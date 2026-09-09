"""
Standalone scan runner for GitHub Actions (or any scheduler/cron).
Runs the exact same search_engine.run_scan() logic the Streamlit app uses —
no Streamlit dependency here at all, so it can run headless.

Usage:
    python run_scan_job.py --mode auto --max-minutes 330
    python run_scan_job.py --mode next_batch --limit 20
    python run_scan_job.py --mode retry_errors --limit 10

--mode auto (the default, and what the scheduled workflow uses):
    Checks whether any companies are still NOT_SCANNED. If yes, runs
    next_batch requesting ALL of them (bounded safely by --max-minutes,
    not by count) so the backlog clears as fast as possible without risking
    a hard timeout kill mid-run. Once the backlog is fully cleared, it
    automatically switches to freshness_check ("New Results") instead of
    doing nothing — checking previously-scanned companies for newly posted
    jobs, which is exactly what a daily run should do once there's no more
    initial backlog left.

Required environment variables (set as GitHub Actions secrets):
    DATABASE_URL   - Supabase/Postgres connection string
    GEMINI_API_KEY - Google Gemini API key
"""

import argparse
import sys
import search_engine
from db_utils import get_connection


def count_pending_companies():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM companies WHERE scan_status = 'NOT_SCANNED'")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def resolve_auto_mode():
    pending = count_pending_companies()
    if pending > 0:
        print(f"{pending} companies still pending — running next_batch to clear backlog.")
        return "next_batch", pending
    print("No pending companies left — running freshness_check for newly posted jobs instead.")
    return "freshness_check", 10000  # effectively "all scanned companies"; time limit protects this


def main():
    parser = argparse.ArgumentParser(description="Run a Job Search OS scan headlessly.")
    parser.add_argument(
        "--mode", default="auto",
        choices=["auto", "next_batch", "retry_errors", "new_run", "freshness_check"],
        help="'auto' clears the pending backlog first, then switches to freshness_check once empty.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max companies to process (ignored in auto mode).")
    parser.add_argument("--max-minutes", type=int, default=330,
                         help="Stop gracefully after this many minutes, regardless of progress. "
                              "Keep this comfortably under your CI job's own timeout.")
    args = parser.parse_args()

    if args.mode == "auto":
        mode, limit = resolve_auto_mode()
    else:
        mode = args.mode
        limit = args.limit if args.limit is not None else 20

    print(f"Starting scan: mode={mode}, limit={limit}, max_minutes={args.max_minutes}")
    result = search_engine.run_scan(
        limit=limit, mode=mode, owner_label="github_actions",
        max_duration_minutes=args.max_minutes,
    )
    print(result)

    if result.startswith("Scan crashed") or result.startswith("Another scan is already in progress") or result.startswith("ERROR"):
        sys.exit(1)


if __name__ == "__main__":
    main()
