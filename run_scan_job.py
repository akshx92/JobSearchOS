"""
Standalone scan runner for GitHub Actions (or any scheduler/cron).
Runs the exact same search_engine.run_scan() logic the Streamlit app uses —
no Streamlit dependency here at all, so it can run headless.

Usage:
    python run_scan_job.py --mode next_batch --limit 20
    python run_scan_job.py --mode retry_errors --limit 10
    python run_scan_job.py --mode freshness_check --limit 15

Required environment variables (set as GitHub Actions secrets):
    DATABASE_URL   - Supabase/Postgres connection string
    GEMINI_API_KEY - Google Gemini API key
"""

import argparse
import sys
import search_engine


def main():
    parser = argparse.ArgumentParser(description="Run a Job Search OS scan headlessly.")
    parser.add_argument(
        "--mode", default="next_batch",
        choices=["next_batch", "retry_errors", "new_run", "freshness_check"],
        help="Which scan mode to run.",
    )
    parser.add_argument("--limit", type=int, default=20, help="Max companies to process this run.")
    args = parser.parse_args()

    print(f"Starting scan: mode={args.mode}, limit={args.limit}")
    result = search_engine.run_scan(limit=args.limit, mode=args.mode, owner_label="github_actions")
    print(result)

    # Non-zero exit code on a crash or lock conflict, so the GitHub Actions
    # run itself shows as failed/skipped rather than silently "succeeding"
    # while nothing actually happened.
    if result.startswith("Scan crashed") or result.startswith("Another scan is already in progress") or result.startswith("ERROR"):
        sys.exit(1)


if __name__ == "__main__":
    main()
