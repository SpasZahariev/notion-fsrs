# notion-fsrs

Rank LeetCode problems from Notion using FSRS (Free Spaced Repetition Scheduler) and set daily "To Do" review items.

## Setup

1. Add `NOTION_API_KEY` to `.env`
2. Ensure both DBs exist:
   - **Problems DB** (`bc4026a6...`) with properties: `Score`, `Last Reviewed` (date), `Status`, `Leetcode Reviews` (relation)
   - **Reviews DB** (`3ad974f8...`) with properties: `Date` (date), `Leetcode` (relation to Problems), `Select` (score options: FAILED, 1-5)

## Usage

```bash
uv run python main.py           # Select top 3, mark as "To Do" in Notion
uv run python main.py --dry-run # Preview only
uv run python main.py -n 5      # Select top 5
```

## Workflow

1. Solve problems on LeetCode
2. Update `Score` and `Last Reviewed` date on problem pages in Notion
3. Run the script - it auto-syncs new reviews, ranks by FSRS urgency, marks top N as "To Do"

## How it works

- First run fetches all reviews from Notion and builds a local `cache.json`
- Subsequent runs only fetch the Problems DB (~1s), diff against cache, and create delta reviews
- Uses `fsrs.Scheduler` with real per-review history to compute card Difficulty, Stability, and recall probability
- Priority = days overdue + forgetfulness; cards with no reviews get low baseline priority
