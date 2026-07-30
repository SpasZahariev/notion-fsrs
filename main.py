#!/usr/bin/env python3
"""
notion-fsrs: Query LeetCode problems from Notion, rank by FSRS priority, and set daily review items.

On each run it reads "Last Reviewed" + "Score" from the Problems DB, auto-syncs any
new reviews into the Reviews DB, then uses full FSRS to determine what to study today.

Usage:
    uv run python main.py              # Default: select top 3, update Status to "To Do"
    uv run python main.py --dry-run    # Print without updating Notion
    uv run python main.py -n 5         # Select top 5 instead of default 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from fsrs import Card, Rating, ReviewLog, Scheduler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROBLEMS_DB_ID = "bc4026a6449247e48b5445758bcdad5f"
REVIEWS_DB_ID = "3ad974f8b66a80eca299e6c937c3a6bc"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PAGE_SIZE = 100
CACHE_FILE = "cache.json"

SCORE_TO_RATING: dict[str, Rating] = {
    "FAILED": Rating.Again,
    "1": Rating.Hard,
    "2": Rating.Good,
    "3": Rating.Easy,
    "4": Rating.Easy,
    "5": Rating.Easy,
}

_fsrs = Scheduler(desired_retention=0.9)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Problem:
    """A single LeetCode problem from the Notion database."""
    page_id: str
    title: str
    status: str
    score_label: str              # e.g. "FAILED", "1" .. "5"
    last_reviewed: date | None    # from "Last Reviewed" property
    link_text: str


@dataclass(order=True)
class RankedCard:
    """A problem with computed FSRS state and review priority."""
    priority_score: float         # Higher = more urgent
    card: Card = field(compare=False)
    problem: Problem = field(compare=False)
    review_count: int = field(compare=False, default=0)


# ---------------------------------------------------------------------------
# Notion API helpers
# ---------------------------------------------------------------------------

def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fetch_all_pages(api_key: str, db_id: str, timeout: float = 30.0) -> list[dict[str, Any]]:
    """Query a Notion database and return all page results (handles pagination)."""
    url = f"{NOTION_API_BASE}/databases/{db_id}/query"
    headers = _headers(api_key)
    all_results: list[dict[str, Any]] = []

    cursor: str | None = None
    with httpx.Client(headers=headers, timeout=timeout) as client:
        while True:
            body: dict[str, Any] = {"page_size": PAGE_SIZE}
            if cursor:
                body["start_cursor"] = cursor

            resp = client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()

            all_results.extend(data.get("results", []))
            if not data.get("has_more", False):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break

    return all_results


def update_page_status(api_key: str, page_id: str, status: str) -> None:
    """Update a page's Status property."""
    url = f"{NOTION_API_BASE}/pages/{page_id}"
    payload = {"properties": {"Status": {"select": {"name": status}}}}

    with httpx.Client(headers=_headers(api_key), timeout=30.0) as client:
        resp = client.patch(url, json=payload)
        resp.raise_for_status()


def create_review_entry(
    api_key: str,
    problem_id: str,
    score_label: str,
    review_date: date,
) -> None:
    """Create a new review entry in the Reviews DB."""
    today_str = review_date.isoformat()
    payload = {
        "parent": {"database_id": REVIEWS_DB_ID},
        "properties": {
            "Title": {
                "title": [{"text": {"content": f"Review - {today_str}"}}]
            },
            "Leetcode": {"relation": [{"id": problem_id}]},
            "Date": {"date": {"start": today_str}},
            "Select": {"select": {"name": score_label}},
        },
    }

    with httpx.Client(headers=_headers(api_key), timeout=30.0) as client:
        resp = client.post(f"{NOTION_API_BASE}/pages", json=payload)
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _get_title(props: dict[str, Any]) -> str:
    """Extract plain text title from Notion properties."""
    for prop_name in ("Projects", "Title"):
        prop = props.get(prop_name)
        if isinstance(prop, dict):
            items = prop.get("title", [])
            if items and isinstance(items, list):
                return "".join(
                    item.get("plain_text", "")
                    for item in items
                    if isinstance(item, dict)
                ).strip()
    return ""


def _get_select_name(props: dict[str, Any], prop_name: str) -> str:
    """Extract the name of a select property."""
    obj = props.get(prop_name)
    if isinstance(obj, dict):
        sel = obj.get("select")
        if isinstance(sel, dict):
            return sel.get("name", "")
    return ""


def _get_rich_text(props: dict[str, Any], prop_name: str) -> str:
    """Extract plain text from a rich_text property."""
    obj = props.get(prop_name)
    if isinstance(obj, dict):
        items = obj.get("rich_text", [])
        if isinstance(items, list):
            return "".join(
                item.get("plain_text", "")
                for item in items
                if isinstance(item, dict)
            ).strip()
    return ""


def _get_date(props: dict[str, Any], prop_name: str) -> date | None:
    """Extract a date from a date property."""
    obj = props.get(prop_name)
    if isinstance(obj, dict):
        d = obj.get("date")
        if isinstance(d, dict):
            raw = d.get("start")
            if raw:
                try:
                    return datetime.strptime(raw, "%Y-%m-%d").date()
                except ValueError:
                    pass
    return None


def parse_problems(raw_pages: list[dict[str, Any]]) -> list[Problem]:
    """Convert raw Notion results into Problem objects."""
    problems = []
    for entry in raw_pages:
        props = entry.get("properties", {})

        score_obj = props.get("Score", {})
        score_label = (
            score_obj.get("select", {}).get("name", "")
            if isinstance(score_obj, dict)
            else ""
        )

        last_reviewed_obj = props.get("Last Reviewed", {})
        last_reviewed_str = ""
        if isinstance(last_reviewed_obj, dict):
            d = last_reviewed_obj.get("date")
            if isinstance(d, dict):
                last_reviewed_str = d.get("start", "")

        last_reviewed: date | None = None
        if last_reviewed_str:
            try:
                last_reviewed = datetime.strptime(last_reviewed_str, "%Y-%m-%d").date()
            except ValueError:
                pass

        problems.append(Problem(
            page_id=entry.get("id", ""),
            title=_get_title(props),
            status=_get_select_name(props, "Status"),
            score_label=score_label,
            last_reviewed=last_reviewed,
            link_text=_get_rich_text(props, "Link"),
        ))

    return problems


# ---------------------------------------------------------------------------
# Local cache: avoid re-fetching reviews on every run
# ---------------------------------------------------------------------------

CacheEntry = dict[str, str]  # {"date": "YYYY-MM-DD", "score": "FAILED|1..5"}
CacheData = dict[str, CacheEntry]  # problem_id -> entry


def load_cache() -> tuple[CacheData, dict[str, list[ReviewLog]]]:
    """
    Load cached review data from disk.

    Returns (cache_entries, reviews_by_problem) where cache_entries maps
    problem_id -> {date, score} and reviews_by_problem is the parsed ReviewLog map.
    """
    if not os.path.exists(CACHE_FILE):
        return {}, {}

    try:
        with open(CACHE_FILE) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}, {}

    cache: CacheData = {}
    reviews_by_problem: dict[str, list[ReviewLog]] = {}

    for pid, entry in raw.items():
        if not isinstance(entry, dict) or "date" not in entry:
            continue
        cache[pid] = entry
        rating = SCORE_TO_RATING.get(entry.get("score", ""))
        if not rating:
            continue
        try:
            review_dt = datetime.strptime(entry["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        reviews_by_problem.setdefault(pid, []).append(
            ReviewLog(
                card_id=hash(pid) & 0xFFFFFFFF,
                rating=rating,
                review_datetime=review_dt,
                review_duration=None,
            )
        )

    for pid in reviews_by_problem:
        reviews_by_problem[pid].sort(key=lambda r: r.review_datetime)

    return cache, reviews_by_problem


def save_cache(cache: CacheData) -> None:
    """Persist cache to disk."""
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


# ---------------------------------------------------------------------------
# Review sync: auto-create missing entries from problems
# ---------------------------------------------------------------------------

def sync_reviews(
    api_key: str,
    problems: list[Problem],
    cache: CacheData,
    reviews_by_problem: dict[str, list[ReviewLog]],
) -> int:
    """
    Compare each problem's Last Reviewed + Score against the local cache.
    For any new or changed reviews, create a Notion entry and update cache + logs.

    Returns the number of new entries created.
    """
    created = 0
    for p in problems:
        if not p.last_reviewed or not p.score_label:
            continue

        date_str = p.last_reviewed.isoformat()
        cached = cache.get(p.page_id)

        # Only create if the Last Reviewed date has changed (or no prior cache entry)
        cached_date = cached.get("date") if isinstance(cached, dict) else None
        if cached_date == date_str:
            continue  # already synced

        try:
            create_review_entry(api_key, p.page_id, p.score_label, p.last_reviewed)
        except httpx.HTTPStatusError as e:
            print(f"Warning: Failed to sync review for '{p.title}': {e}", file=sys.stderr)
            continue

        # Update cache and in-memory review logs
        cache[p.page_id] = {"date": date_str, "score": p.score_label}
        created += 1

        rating = SCORE_TO_RATING.get(p.score_label)
        if rating:
            review_dt = datetime(
                p.last_reviewed.year, p.last_reviewed.month,
                p.last_reviewed.day, tzinfo=timezone.utc,
            )
            reviews_by_problem.setdefault(p.page_id, []).append(
                ReviewLog(
                    card_id=hash(p.page_id) & 0xFFFFFFFF,
                    rating=rating,
                    review_datetime=review_dt,
                    review_duration=None,
                )
            )

    return created


# ---------------------------------------------------------------------------
# FSRS ranking logic
# ---------------------------------------------------------------------------

def build_card(problem_id: str, review_logs: list[ReviewLog]) -> Card:
    """Build FSRS card state from review history."""
    card = Card(card_id=hash(problem_id) & 0xFFFFFFFF)
    if review_logs:
        card = _fsrs.reschedule_card(card, review_logs)
    return card


def rank_problems(
    problems: list[Problem],
    reviews_by_problem: dict[str, list[ReviewLog]],
    now: datetime | None = None,
) -> list[RankedCard]:
    """
    Rank problems by review urgency using full FSRS.

    Priority formula:
    - Overdue cards (due <= now): high priority, scaled by days overdue + forgetfulness
    - New cards (no reviews): low baseline priority
    """
    if now is None:
        now = datetime.now(timezone.utc)

    ranked: list[RankedCard] = []

    for p in problems:
        logs = reviews_by_problem.get(p.page_id, [])
        card = build_card(p.page_id, logs)
        retrievability = _fsrs.get_card_retrievability(card, now)

        if not logs:
            priority = 0.5
        else:
            if card.due and card.due <= now:
                days_overdue = (now - card.due).days
                priority = round(
                    0.5 + min(days_overdue * 0.1, 2.0) + (1.0 - retrievability), 6
                )
            else:
                priority = round(retrievability - 0.5, 6)

        ranked.append(RankedCard(
            priority_score=priority,
            card=card,
            problem=p,
            review_count=len(logs),
        ))

    ranked.sort(key=lambda c: c.priority_score, reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _state_label(card: Card) -> str:
    from fsrs import State
    names = {
        State.Learning: "Learning",
        State.Review: "Review",
        State.Relearning: "Relearn",
    }
    return names.get(card.state, f"Unknown({card.state})")


def format_card(card: RankedCard, index: int) -> str:
    """Format a single ranked card for stdout output."""
    p = card.problem
    c = card.card
    d_str = f"{c.difficulty:.2f}" if c.difficulty is not None else "N/A"
    s_str = f"{c.stability:.1f}d" if c.stability is not None else "N/A"

    lines = [
        f"  {index}. {p.title}",
        f"     State: {_state_label(c)} | D: {d_str} | S: {s_str} | Reviews: {card.review_count}",
    ]

    if c.due:
        lines.append(f"     Due: {c.due.strftime('%Y-%m-%d')}")

    if p.link_text:
        lines.append(f"     Link: {p.link_text}")

    return "\n".join(lines)


def print_results(cards: list[RankedCard]) -> None:
    """Print ranked cards to stdout."""
    if not cards:
        print("No review items found.")
        return

    print(f"\nTop {len(cards)} review items (most urgent first):\n")
    for i, card in enumerate(cards, 1):
        print(format_card(card, i))
        print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rank LeetCode problems by FSRS review priority and set daily To Do items."
        )
    )
    parser.add_argument(
        "-n", type=int, default=3,
        help="Number of items to select (default: 3)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print ranked items without updating Notion Status or syncing reviews",
    )
    return parser.parse_args(argv)


def _seed_cache_from_notion(
    api_key: str,
    problems: list[Problem],
) -> tuple[CacheData, dict[str, list[ReviewLog]]]:
    """
    First-run seed: fetch all reviews from Notion and build the local cache.

    Returns (cache_data, reviews_by_problem).
    """
    raw_reviews = fetch_all_pages(api_key, REVIEWS_DB_ID)
    problem_map = {p.page_id: p for p in problems}

    cache: CacheData = {}
    reviews_by_problem: dict[str, list[ReviewLog]] = {}

    for entry in raw_reviews:
        props = entry.get("properties", {})

        # Linked problem via 'Leetcode' relation
        leetcode_rel = props.get("Leetcode", {})
        if not isinstance(leetcode_rel, dict):
            continue
        relations = leetcode_rel.get("relation", [])

        # Review date
        date_obj = props.get("Date", {})
        date_str = ""
        if isinstance(date_obj, dict):
            d = date_obj.get("date")
            if isinstance(d, dict):
                date_str = d.get("start", "")

        if not date_str:
            continue

        try:
            review_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        # Score -> rating
        select_obj = props.get("Select", {})
        score_label = ""
        if isinstance(select_obj, dict):
            sel = select_obj.get("select")
            if isinstance(sel, dict):
                score_label = sel.get("name", "")

        if not score_label or score_label not in SCORE_TO_RATING:
            continue

        rating = SCORE_TO_RATING[score_label]

        for rel in relations:
            if not isinstance(rel, dict):
                continue
            pid = rel.get("id", "")
            if pid not in problem_map:
                continue

            # Keep latest review per problem in cache
            if pid not in cache or date_str > cache[pid].get("date", ""):
                cache[pid] = {"date": date_str, "score": score_label}

            reviews_by_problem.setdefault(pid, []).append(
                ReviewLog(
                    card_id=hash(pid) & 0xFFFFFFFF,
                    rating=rating,
                    review_datetime=review_dt,
                    review_duration=None,
                )
            )

    for pid in reviews_by_problem:
        reviews_by_problem[pid].sort(key=lambda r: r.review_datetime)

    return cache, reviews_by_problem


def main(argv: list[str] | None = None) -> int:
    """Main entry point. Returns exit code."""
    args = parse_args(argv)

    load_dotenv()
    api_key = os.getenv("NOTION_API_KEY")
    if not api_key:
        print(
            "Error: NOTION_API_KEY not set. Add it to .env or export it.",
            file=sys.stderr,
        )
        return 1

    # Step 1: Load local cache (avoids re-fetching reviews DB)
    cache, reviews_by_problem = load_cache()

    # If no cache exists, seed from Notion's Reviews DB once
    if not cache:
        try:
            raw_problems = fetch_all_pages(api_key, PROBLEMS_DB_ID)
        except httpx.HTTPStatusError as e:
            print(f"Error querying Notion: {e}", file=sys.stderr)
            return 1

        problems = parse_problems(raw_problems)
        cache, reviews_by_problem = _seed_cache_from_notion(api_key, problems)
        save_cache(cache)
    else:
        # Normal run: just fetch problems (reviews come from cache + delta sync)
        try:
            raw_problems = fetch_all_pages(api_key, PROBLEMS_DB_ID)
        except httpx.HTTPStatusError as e:
            print(f"Error querying Notion: {e}", file=sys.stderr)
            return 1

        problems = parse_problems(raw_problems)

    # Step 2: Auto-sync new reviews (unless dry-run) - only creates deltas
    if not args.dry_run:
        new_count = sync_reviews(api_key, problems, cache, reviews_by_problem)
        if new_count:
            print(f"Synced {new_count} new review(s) from Problems DB.\n")
            save_cache(cache)

    # Step 3: Filter out already-To-Do cards
    candidates = [p for p in problems if p.status != "To Do"]

    if not candidates:
        print("No candidate problems (all To Do or DB empty).")
        return 0

    ranked = rank_problems(candidates, reviews_by_problem)
    top_n = ranked[: args.n]

    # Step 4: Print results
    print_results(top_n)

    # Step 5: Update Status to "To Do" (unless dry-run)
    if args.dry_run:
        print("[DRY RUN] Skipping Notion updates.\n")
    else:
        for card in top_n:
            try:
                update_page_status(api_key, card.problem.page_id, "To Do")
                print(f"Updated '{card.problem.title}' -> To Do")
            except httpx.HTTPStatusError as e:
                print(
                    f"Warning: Failed to update '{card.problem.title}': {e}",
                    file=sys.stderr,
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
