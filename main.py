#!/usr/bin/env python3
"""
notion-fsrs: Query LeetCode problems from Notion, rank by FSRS priority, and set daily review items.

Usage:
    uv run python main.py              # Default: select top 3, update Status to "To Do"
    uv run python main.py --dry-run    # Print without updating Notion
    uv run python main.py -n 5         # Select top 5 instead of default 3
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from fsrs import Rating

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATABASE_ID = "bc4026a6449247e48b5445758bcdad5f"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PAGE_SIZE = 100

# Map captain's Notion scores to FSRS quality ratings.
SCORE_TO_RATING: dict[str, Rating] = {
    "FAILED": Rating.Again,   # 0 -> Again
    "1": Rating.Hard,         # 1 -> Hard
    "2": Rating.Good,         # 2 -> Good
    "3": Rating.Easy,         # 3,4,5 -> Easy
    "4": Rating.Easy,
    "5": Rating.Easy,
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class NotionPage:
    """A single problem page from the Notion database."""
    page_id: str
    title: str
    score_label: str          # e.g. "FAILED", "1", "2", "3", "4", "5"
    status: str               # e.g. "To Do", "spaced_repetition", ...
    times_done: int | None
    last_edited: datetime     # from last_edited_time property
    link_text: str            # LeetCode URL (may be empty)

    @property
    def fsrs_rating(self) -> Rating | None:
        """Convert Notion score to FSRS rating."""
        return SCORE_TO_RATING.get(self.score_label)


@dataclass(order=True)
class RankedCard:
    """A card with computed priority for sorting."""
    priority_score: float          # Higher = more urgent review needed
    page: NotionPage = field(compare=False)
    days_since_review: int | None = field(compare=False, default=None)


# ---------------------------------------------------------------------------
# Notion API helpers
# ---------------------------------------------------------------------------

def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fetch_all_pages(api_key: str, timeout: float = 30.0) -> list[dict[str, Any]]:
    """Query the Notion database and return all page results (handles pagination)."""
    url = f"{NOTION_API_BASE}/databases/{DATABASE_ID}/query"
    headers = _headers(api_key)
    all_results: list[dict[str, Any]] = []

    cursor: str | None = None
    with httpx.Client(headers=headers, timeout=timeout) as client:
        while True:
            body: dict[str, Any] = {
                "page_size": PAGE_SIZE,
            }
            if cursor:
                body["start_cursor"] = cursor

            resp = client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()

            all_results.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            next_cursor = data.get("next_cursor")

            if not has_more or not next_cursor:
                break
            cursor = next_cursor

    return all_results


def update_page_status(api_key: str, page_id: str, status: str = "To Do") -> None:
    """Update a page's Status property via Notion Update Page API."""
    url = f"{NOTION_API_BASE}/pages/{page_id}"
    headers = _headers(api_key)

    payload: dict[str, Any] = {
        "properties": {
            "Status": {
                "select": {"name": status}
            }
        }
    }

    with httpx.Client(headers=headers, timeout=30.0) as client:
        resp = client.patch(url, json=payload)
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_pages(raw_pages: list[dict[str, Any]]) -> list[NotionPage]:
    """Convert raw Notion API results into typed NotionPage objects."""
    pages: list[NotionPage] = []
    for entry in raw_pages:
        props = entry.get("properties", {})

        # Title from Projects property (id="title", type="title")
        title_prop = props.get("Projects", {})
        title_list: list[Any] = []
        if isinstance(title_prop, dict):
            title_list = title_prop.get("title", [])
        title = ""
        if isinstance(title_list, list):
            title = "".join(item.get("plain_text", "") for item in title_list if isinstance(item, dict))

        # Score (select)
        score_obj = props.get("Score", {})
        score_label = score_obj.get("select", {}).get("name", "") if isinstance(score_obj, dict) else ""

        # Status (select)
        status_obj = props.get("Status", {})
        status = status_obj.get("select", {}).get("name", "") if isinstance(status_obj, dict) else ""

        # Times done (number)
        times_done_prop = props.get("Times done", {})
        times_done: int | None = None
        if isinstance(times_done_prop, dict):
            raw_num = times_done_prop.get("number")
            if raw_num is not None:
                times_done = int(raw_num)

        # Last edit to page (last_edited_time) - value is a string timestamp
        last_edit_prop = props.get("Last edit to page", {})
        last_edit_str = ""
        if isinstance(last_edit_prop, dict):
            raw_val = last_edit_prop.get("last_edited_time")
            if isinstance(raw_val, str):
                last_edit_str = raw_val
            elif isinstance(raw_val, dict):
                last_edit_str = raw_val.get("time", "")

        last_edited = datetime.fromisoformat(last_edit_str.replace("Z", "+00:00")) if last_edit_str else datetime.now(timezone.utc)

        # Link (rich_text)
        link_obj = props.get("Link", {})
        link_text = ""
        if isinstance(link_obj, dict):
            rich_text_list = link_obj.get("rich_text", [])
            if isinstance(rich_text_list, list):
                link_text = "".join(item.get("plain_text", "") for item in rich_text_list if isinstance(item, dict))

        pages.append(NotionPage(
            page_id=entry.get("id", ""),
            title=title.strip(),
            score_label=score_label,
            status=status,
            times_done=times_done,
            last_edited=last_edited,
            link_text=link_text.strip(),
        ))

    return pages


# ---------------------------------------------------------------------------
# FSRS ranking logic
# ---------------------------------------------------------------------------

def compute_priority(page: NotionPage, now: datetime) -> RankedCard:
    """
    Compute review priority using a simplified FSRS model.

    Since we only have aggregate data (Times done, current Score), not per-review history,
    we use these proxies to estimate urgency:

    - Cards not reviewed in >14 days are high priority
    - Lower Score = higher urgency (FAILED worst, 5 best)
    - Higher Times done with low Score = repeat offender (higher priority)
    """
    # Days since last review
    delta = now - page.last_edited
    days_since = max(delta.days, 0)

    # Base priority from time decay: exponential increase after 14-day threshold
    time_threshold = 14
    time_factor = max(0, (days_since - time_threshold)) ** 2 / 100.0

    # Score urgency: map to numeric where FAILED=0 is worst
    score_value_map = {"FAILED": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
    score_val = score_value_map.get(page.score_label, 3)
    # Invert: lower score = higher urgency (5 - val gives 5 for FAILED, 0 for score 5)
    score_factor = (5 - score_val) * 2.0

    # Repeat offender factor: more attempts with low score = more urgent
    repeat_factor = 0.0
    if page.times_done is not None and page.times_done > 0:
        repeat_factor = min(page.times_done * 0.5, 5.0)

    # Combine factors
    priority = time_factor + score_factor + repeat_factor

    return RankedCard(
        priority_score=round(priority, 4),
        page=page,
        days_since_review=days_since if days_since > 0 else None,
    )


def rank_cards(pages: list[NotionPage], now: datetime | None = None) -> list[RankedCard]:
    """Rank cards by review priority (highest first)."""
    if now is None:
        now = datetime.now(timezone.utc)

    ranked = [compute_priority(page, now) for page in pages]
    ranked.sort(key=lambda c: c.priority_score, reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def format_card(card: RankedCard, index: int) -> str:
    """Format a single ranked card for stdout output."""
    p = card.page
    days_str = f"{card.days_since_review}d" if card.days_since_review else "never"
    times_str = f"{p.times_done}" if p.times_done is not None else "N/A"
    lines = [
        f"  {index}. {p.title}",
        f"     Score: {p.score_label} | Times done: {times_str} | Last review: {days_str} ago",
        f"     Priority: {card.priority_score}",
    ]
    if p.link_text:
        lines.append(f"     Link: {p.link_text}")
    return "\n".join(lines)


def print_results(cards: list[RankedCard]) -> None:
    """Print ranked cards to stdout."""
    if not cards:
        print("No review items found.")
        return

    print(f"\nTop {len(cards)} review items:\n")
    for i, card in enumerate(cards, 1):
        print(format_card(card, i))
        print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank LeetCode problems by FSRS review priority and set daily To Do items."
    )
    parser.add_argument(
        "-n", type=int, default=3,
        help="Number of items to select (default: 3)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print ranked items without updating Notion Status",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Main entry point. Returns exit code."""
    args = parse_args(argv)

    # Load environment variables from .env file
    load_dotenv()
    api_key = os.getenv("NOTION_API_KEY")
    if not api_key:
        print("Error: NOTION_API_KEY not set. Add it to .env or export it.", file=sys.stderr)
        return 1

    # Step 1: Query all pages from DB
    try:
        raw_pages = fetch_all_pages(api_key)
    except httpx.HTTPStatusError as e:
        print(f"Error querying Notion database: {e}", file=sys.stderr)
        return 1

    if not raw_pages:
        print("No pages found in database.", file=sys.stderr)
        return 0

    # Step 2: Parse and filter out "To Do" cards
    all_pages = parse_pages(raw_pages)
    candidate_pages = [p for p in all_pages if p.status != "To Do"]

    if not candidate_pages:
        print("No candidate pages (all already To Do or DB empty).")
        return 0

    # Step 3: Apply FSRS ranking
    ranked = rank_cards(candidate_pages)
    top_n = ranked[:args.n]

    # Step 4: Print results
    print_results(top_n)

    # Step 5: Update Status to "To Do" (unless dry-run)
    if args.dry_run:
        print("[DRY RUN] Skipping Notion updates.\n")
    else:
        for card in top_n:
            try:
                update_page_status(api_key, card.page.page_id)
                print(f"Updated '{card.page.title}' -> To Do")
            except httpx.HTTPStatusError as e:
                print(f"Warning: Failed to update '{card.page.title}': {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
