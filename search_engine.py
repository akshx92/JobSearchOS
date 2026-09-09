import datetime
import time
import re
import logging
from jobdrop import scrape_jobs
import pandas as pd
from ai_evaluator import evaluate_job_match, load_cv_from_bytes
from db_utils import get_connection, q, insert_and_get_id, is_postgres, try_acquire_scan_lock, release_scan_lock, get_scan_lock_status

TARGET_ROLES = (
    "Project Manager OR Program Manager OR Programme Manager OR "
    "Technical Project Manager OR Technical Program Manager OR Scrum Master OR "
    "Delivery Manager OR PMO OR Transformation"
)
TARGET_LOCATION = "Mumbai, Maharashtra, India"
ALLOWED_LOCATION_KEYWORDS = ["mumbai", "thane", "navi mumbai", "mmr"]
FRESHNESS_DAYS = 7
MAX_EXPERIENCE_MIN = 11  # roles requiring 12+ years minimum are excluded per spec tolerance rule
EVAL_DELAY_SECONDS = 10  # starting pause before each AI call; adjusts automatically during a run
MAX_EVAL_DELAY_SECONDS = 30

# Cheap first-pass filter, run before company-match and before any AI call.
# Kills irrelevant roles (e.g. "Area Sales Manager", "Credit Executive") that
# a broad company-name search still pulls in, so we don't waste rate-limited
# AI calls evaluating jobs that were never going to be relevant.
TITLE_KEYWORDS = [
    "project manager", "programme manager", "program manager",
    "technical project manager", "technical program manager",
    "scrum master", "agile", "delivery manager", "delivery lead",
    "pmo", "transformation", "project & change", "digital project manager",
    "technology program manager", "technology project manager",
]

# Tier 1: the actual ATS platforms behind most large companies' real career pages.
# Tier 2: LinkedIn.
# Tier 3: general aggregators.
# Tier 1 sites discover postings via Google search under the hood. Firing all 5
# of these per company, across many companies in a batch, gets flagged as bot
# traffic and Google starts returning CAPTCHA walls ("/sorry/") — at which point
# these sources silently return nothing for the rest of the run. So: only use
# them for single-company "Recheck" lookups, one at a time, with real spacing.
TIER1_SITES = ["greenhouse", "lever", "workday", "ashby", "icims"]
TIER1_DELAY_SECONDS = 20  # pause between each Tier 1 site during a recheck

# Tier 2/3 sites don't depend on Google search and are safe for bulk batch scans.
TIER2_SITES = ["linkedin"]
TIER3_SITES = ["indeed", "naukri", "hiring_cafe"]
BULK_SAFE_SITES = TIER2_SITES + TIER3_SITES

SITE_TIER = {}
for _s in TIER1_SITES:
    SITE_TIER[_s] = 1
for _s in TIER2_SITES:
    SITE_TIER[_s] = 2
for _s in TIER3_SITES:
    SITE_TIER[_s] = 3


def normalize_name(name):
    if not name:
        return "unknown"
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _normalize_title(title):
    if not title:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(title).lower())


# Extensible alias table: normalized variant -> canonical normalized form.
# Add entries here as you spot companies that should match but currently
# don't, since pure string-cleanup normalization can't infer that "JP Morgan"
# and "JPMorgan Chase" are the same entity — that requires knowing the fact.
COMPANY_ALIASES = {
    "jpmorgan": "jpmorganchase",
    "jpmorgangroup": "jpmorganchase",
    "ernstyoung": "ey",
    "boozallen": "boozallenhamilton",
    "bcg": "bostonconsultinggroup",
    "mckinsey": "mckinseycompany",
}


def resolve_company_alias(normalized_name):
    return COMPANY_ALIASES.get(normalized_name, normalized_name)


def _log_error(cursor, company_id, run_id, error_type, message):
    cursor.execute(
        q("""INSERT INTO error_log (company_id, run_id, timestamp, error_type, error_message)
           VALUES (?, ?, ?, ?, ?)"""),
        (company_id, run_id, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         error_type, str(message)[:500]),
    )


def _location_ok(location_str, description=""):
    if location_str:
        loc = location_str.lower()
        if any(kw in loc for kw in ALLOWED_LOCATION_KEYWORDS):
            return True
    # Fallback: JD explicitly states Mumbai eligibility even if the
    # structured location field says otherwise (spec Section 4).
    if description:
        desc = description.lower()
        mentions_mumbai = any(kw in desc for kw in ALLOWED_LOCATION_KEYWORDS)
        mentions_eligible = any(p in desc for p in ["open to", "eligible", "candidates based", "candidates from"])
        if mentions_mumbai and mentions_eligible:
            return True
    return False


MIN_EXPERIENCE_FLOOR = 5  # JDs explicitly capped below this are a real underqualification mismatch


def _stated_max_experience_below_floor(text):
    if not text:
        return False
    match = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})\s*years", text, re.IGNORECASE)
    if match:
        try:
            stated_max = int(match.group(2))
            return stated_max < MIN_EXPERIENCE_FLOOR
        except (ValueError, IndexError):
            return False
    return False


def _title_relevant(title):
    if not title:
        return False
    t = title.lower()
    return any(kw in t for kw in TITLE_KEYWORDS)


def _parse_posting_date(job):
    raw = job.get("date_posted")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, False
    try:
        parsed = pd.to_datetime(raw)
        if pd.isna(parsed):
            return None, False
        return parsed.strftime("%Y-%m-%d"), True
    except Exception:
        return None, False


def _is_fresh(posting_date_str, verified):
    if not verified or not posting_date_str:
        return None
    try:
        posted = datetime.datetime.strptime(posting_date_str, "%Y-%m-%d")
        return (datetime.datetime.now() - posted).days <= FRESHNESS_DAYS
    except Exception:
        return None


# Deterministic pre-filter: catches JDs that explicitly state a minimum
# experience requirement well beyond our threshold, without needing an AI
# call to figure that out. This never touches genuinely ambiguous cases —
# it only intercepts postings that would have failed the hard experience
# filter anyway, so it costs nothing in match quality.
_EXPERIENCE_PATTERNS = [
    re.compile(r"(\d{1,2})\s*\+\s*years", re.IGNORECASE),
    re.compile(r"minimum\s*(?:of\s*)?(\d{1,2})\s*years", re.IGNORECASE),
    re.compile(r"at least\s*(\d{1,2})\s*years", re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*-\s*(\d{1,2})\s*years", re.IGNORECASE),
]


def _stated_min_experience_exceeds_threshold(text):
    if not text:
        return False
    for pattern in _EXPERIENCE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                stated_min = int(match.group(1))
                if stated_min > MAX_EXPERIENCE_MIN:
                    return True
            except (ValueError, IndexError):
                continue
    return False


def _get_companies(cursor, mode, limit):
    # Companies with at least one MBA contact are scanned first — a match at a
    # contact-having company is directly actionable; one with no contact isn't.
    contact_priority = "(SELECT COUNT(*) FROM contacts WHERE contacts.company_id = companies.id) DESC"

    if mode == "retry_errors":
        cursor.execute(
            q(f"""SELECT id, original_name FROM companies
                WHERE scan_status IN ('ERROR','INCOMPLETE')
                ORDER BY {contact_priority} LIMIT ?"""),
            (limit,),
        )
    elif mode == "new_run":
        cursor.execute(
            q(f"SELECT id, original_name FROM companies ORDER BY {contact_priority} LIMIT ?"),
            (limit,),
        )
    elif mode == "freshness_check":
        cursor.execute(
            q(f"""SELECT id, original_name FROM companies
                WHERE last_scanned_date IS NOT NULL
                ORDER BY {contact_priority} LIMIT ?"""),
            (limit,),
        )
    else:  # next_batch
        cursor.execute(
            q(f"""SELECT id, original_name FROM companies
                WHERE scan_status = 'NOT_SCANNED'
                ORDER BY {contact_priority} LIMIT ?"""),
            (limit,),
        )
    return cursor.fetchall()


class _SourceErrorCapture(logging.Handler):
    """Captures ERROR-level log records emitted during a scrape call.
    jobdrop logs per-source failures (e.g. 'hit Google /sorry/') via the
    standard logging module rather than raising exceptions, so this is the
    only way to detect a partial failure that returns an empty-but-not-crashed
    result."""
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.captured = []

    def emit(self, record):
        self.captured.append(self.format(record))


def _scrape_with_error_capture(**kwargs):
    handler = _SourceErrorCapture()
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        df = scrape_jobs(**kwargs)
    finally:
        root_logger.removeHandler(handler)
    return df, handler.captured


def _fetch_jobs_for_company(comp_name, mode, scan_state=None):
    """Returns (combined_dataframe, source_errors list). Bulk scan modes only
    use sources that don't depend on Google search (avoids CAPTCHA blocks).
    The 5 ATS/career-page sources are only queried during a single-company
    'recheck', one at a time with spacing."""
    frames = []
    all_source_errors = []

    safe_df, safe_errors = _scrape_with_error_capture(
        site_name=BULK_SAFE_SITES,
        search_term=f"{comp_name} {TARGET_ROLES}",
        location=TARGET_LOCATION,
        results_wanted=3,
        country_indeed="India",
    )
    all_source_errors.extend(safe_errors)
    if isinstance(safe_df, pd.DataFrame) and not safe_df.empty:
        frames.append(safe_df)

    if mode == "recheck":
        for site in TIER1_SITES:
            if scan_state is not None:
                scan_state["message"] = f"Checking {site} career page for {comp_name}..."
            try:
                site_df, site_errors = _scrape_with_error_capture(
                    site_name=[site],
                    search_term=f"{comp_name} {TARGET_ROLES}",
                    location=TARGET_LOCATION,
                    results_wanted=3,
                )
                all_source_errors.extend(site_errors)
                if isinstance(site_df, pd.DataFrame) and not site_df.empty:
                    frames.append(site_df)
            except Exception as e:
                all_source_errors.append(f"{site}: {e}")  # a blocked/failed ATS source just contributes nothing this time
            time.sleep(TIER1_DELAY_SECONDS)

    combined = pd.DataFrame() if not frames else pd.concat(frames, ignore_index=True)
    return combined, all_source_errors


def verify_job_links(limit=30):
    """Checks stored job URLs and marks verifiably dead ones as Closed/Expired,
    per spec Section 12. Prioritizes links never checked before, then the
    oldest-checked ones. Safe to run often — each link is wrapped individually
    so one bad request can't derail the batch."""
    import requests

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        q("""SELECT id, job_url FROM jobs
           WHERE job_status NOT IN ('Rejected', 'Closed/Expired') AND job_url IS NOT NULL AND job_url != ''
           ORDER BY last_verified_date IS NOT NULL, last_verified_date ASC
           LIMIT ?"""),
        (limit,),
    )
    rows = cursor.fetchall()

    checked, expired, errors = 0, 0, 0
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; JobSearchOS/1.0)"}

    for job_id, job_url in rows:
        try:
            resp = requests.get(job_url, headers=headers, timeout=10, allow_redirects=True)
            checked += 1
            if resp.status_code in (404, 410):
                cursor.execute(
                    q("UPDATE jobs SET job_status = 'Closed/Expired', link_verified = 0, last_verified_date = ? WHERE id = ?"),
                    (now, job_id),
                )
                expired += 1
            else:
                cursor.execute(
                    q("UPDATE jobs SET link_verified = 1, last_verified_date = ? WHERE id = ?"),
                    (now, job_id),
                )
            conn.commit()
        except Exception:
            # Network hiccup or the site blocking automated requests — don't
            # mark it as expired on an inconclusive result, just leave it and
            # try again next time.
            errors += 1
        time.sleep(1)

    conn.close()
    return f"Checked {checked} links: {expired} now marked Closed/Expired, {errors} couldn't be verified this time."


def run_scan(limit=20, mode="next_batch", scan_state=None, stop_event=None, single_company=None, owner_label="app"):
    """Public entry point — wraps the real scan logic in a safety net so any
    unexpected exception (e.g. a locked database file) still resets
    scan_state, instead of leaving the progress bar and Stop button frozen
    forever with no error shown. Also enforces a scan lock so two scans
    (e.g. a scheduled GitHub Actions run and a manual 'Recheck Now') can
    never run at the same time."""
    if not try_acquire_scan_lock(owner_label=owner_label):
        is_locked, owner, held_since = get_scan_lock_status()
        msg = f"Another scan is already in progress (started by '{owner}' at {held_since}). Try again shortly."
        if scan_state is not None:
            scan_state["running"] = False
            scan_state["message"] = msg
        return msg

    try:
        return _run_scan_impl(
            limit=limit, mode=mode, scan_state=scan_state,
            stop_event=stop_event, single_company=single_company,
        )
    except Exception as e:
        if scan_state is not None:
            scan_state["running"] = False
            scan_state["message"] = f"Scan crashed unexpectedly: {e}"
        return f"Scan crashed unexpectedly: {e}"
    finally:
        release_scan_lock()


def _run_scan_impl(limit=20, mode="next_batch", scan_state=None, stop_event=None, single_company=None):
    """
    mode: 'next_batch' | 'retry_errors' | 'new_run' | 'freshness_check' | 'recheck'
    single_company: (company_id, company_name) tuple — required when mode == 'recheck'
    scan_state: a plain dict (NOT a Streamlit widget) used to report progress safely
                from a background thread. Keys: running, current, total, message.
    stop_event: a threading.Event() the UI can set to request a graceful stop.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("UPDATE jobs SET job_status = 'Old' WHERE job_status = 'New'")
    conn.commit()

    cursor.execute("SELECT id, filename, file_content FROM cv_versions WHERE is_active = TRUE")
    active_cv = cursor.fetchone()
    if not active_cv:
        conn.close()
        msg = "ERROR: No Active CV found. Please upload a CV first."
        if scan_state is not None:
            scan_state["running"] = False
            scan_state["message"] = msg
        return msg
    cv_id, cv_filename, cv_bytes = active_cv

    cv_text = load_cv_from_bytes(bytes(cv_bytes) if cv_bytes is not None else None, cv_filename)
    if not cv_text:
        conn.close()
        msg = f"ERROR: Active CV '{cv_filename}' could not be read from the database (empty or corrupted). Please re-upload it. Scan aborted before wasting AI calls."
        if scan_state is not None:
            scan_state["running"] = False
            scan_state["message"] = msg
        return msg

    if mode == "recheck" and single_company:
        companies_to_scan = [single_company]
    else:
        companies_to_scan = _get_companies(cursor, mode, limit)

    if not companies_to_scan:
        conn.close()
        msg = "No companies matched this scan mode."
        if scan_state is not None:
            scan_state["running"] = False
            scan_state["message"] = msg
        return msg

    start_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_id = insert_and_get_id(
        cursor,
        "INSERT INTO search_runs (start_time, cv_version_id, companies_scanned) VALUES (?, ?, 0)",
        (start_time, cv_id),
    )
    conn.commit()

    companies_completed = 0
    jobs_found = 0
    errors_logged = 0
    skipped_location = 0
    skipped_stale = 0
    skipped_experience = 0
    skipped_underqualified = 0
    skipped_duplicate = 0
    total_companies = len(companies_to_scan)
    adaptive_delay = EVAL_DELAY_SECONDS

    for i, (comp_id, comp_name) in enumerate(companies_to_scan):
        if stop_event and stop_event.is_set():
            if scan_state is not None:
                scan_state["message"] = "Scan stopped by user."
            break

        scan_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if scan_state is not None:
            scan_state["current"] = i + 1
            scan_state["total"] = total_companies
            scan_state["message"] = f"Scanning ({i + 1}/{total_companies}): {comp_name}"

        cursor.execute(
            q("""INSERT INTO scan_records (company_id, run_id, scan_date, status, cv_version_id)
               VALUES (?, ?, ?, 'IN_PROGRESS', ?)"""),
            (comp_id, run_id, scan_date, cv_id),
        )
        conn.commit()

        try:
            jobs_df, source_errors = _fetch_jobs_for_company(comp_name, mode, scan_state=scan_state)
            for src_err in source_errors:
                _log_error(cursor, comp_id, run_id, "SOURCE_ERROR", src_err)
                errors_logged += 1

            if isinstance(jobs_df, pd.DataFrame) and not jobs_df.empty:
                for _, job in jobs_df.iterrows():
                    job_title = str(job.get("title", "Unknown Role"))
                    job_url = str(job.get("job_url", "") or "")
                    location = str(job.get("location", "") or "")
                    company_found = str(job.get("company", comp_name) or job.get("company_name", comp_name) or "")
                    job_description = str(job.get("description", "") or "")
                    external_job_id = str(job.get("id", "") or job.get("job_id", "") or "")
                    site_name = str(job.get("site", "unknown") or "unknown")
                    tier = SITE_TIER.get(site_name, 3)
                    source_label = f"{site_name} (Tier {tier})"

                    if not job_url:
                        continue

                    # Cheapest filter first: is this even a PM/Delivery/Scrum-type
                    # role? Kills irrelevant noise before spending effort on it.
                    if not _title_relevant(job_title):
                        continue

                    # Company identity check: compare full normalized names,
                    # not just the first few characters.
                    n_found, n_comp = normalize_name(company_found), normalize_name(comp_name)
                    if n_comp not in n_found and n_found not in n_comp:
                        continue

                    # Hard filter: location must be Mumbai/MMR/Thane/Navi Mumbai,
                    # OR the JD text explicitly says it's open to Mumbai candidates
                    if not _location_ok(location, job_description):
                        skipped_location += 1
                        continue

                    # Hard filter: freshness (only excludes when verifiably stale)
                    posting_date, verified = _parse_posting_date(job)
                    fresh = _is_fresh(posting_date, verified)
                    if fresh is False:
                        skipped_stale += 1
                        continue

                    # #1b: Same real job posted on a different source (e.g. the
                    # same posting on LinkedIn and Naukri gets different URLs).
                    # Match by company + normalized title instead of exact URL.
                    # Merges into the existing row's source list rather than
                    # creating a second card for what's really one opportunity.
                    target_title = _normalize_title(job_title)
                    cross_source_match_id = None
                    matched_source = None
                    cursor.execute(
                        q("SELECT id, job_title, source, job_status FROM jobs WHERE company_id = ? AND job_status NOT IN ('Rejected', 'Closed/Expired')"),
                        (comp_id,),
                    )
                    for existing_id, existing_title, existing_source, existing_status in cursor.fetchall():
                        if _normalize_title(existing_title) == target_title:
                            cross_source_match_id = existing_id
                            matched_source = existing_source
                            break

                    if cross_source_match_id:
                        merged_source = matched_source or ""
                        if source_label not in merged_source:
                            merged_source = f"{merged_source}, {source_label}" if merged_source else source_label
                        cursor.execute(
                            q("""UPDATE jobs SET last_seen_date = ?, source = ?,
                               job_status = CASE WHEN job_status = 'Old' THEN 'New' ELSE job_status END
                               WHERE id = ?"""),
                            (scan_date, merged_source, cross_source_match_id),
                        )
                        conn.commit()
                        skipped_duplicate += 1
                        continue

                    # #1: Already have this exact posting? Don't re-evaluate it —
                    # just refresh last_seen_date and revive it from Old to New
                    # if needed. Same result as before, zero wasted AI calls.
                    cursor.execute(q("SELECT id, job_status FROM jobs WHERE job_url = ?"), (job_url,))
                    existing_job = cursor.fetchone()
                    if existing_job:
                        existing_id, existing_status = existing_job
                        cursor.execute(
                            q("""UPDATE jobs SET last_seen_date = ?,
                               job_status = CASE WHEN job_status = 'Old' THEN 'New' ELSE job_status END
                               WHERE id = ?"""),
                            (scan_date, existing_id),
                        )
                        conn.commit()
                        skipped_duplicate += 1
                        continue

                    # #2: Does the JD itself explicitly state a minimum experience
                    # well past our threshold? Catch this deterministically before
                    # spending an AI call on something that's going to be a hard
                    # exclude either way.
                    if _stated_min_experience_exceeds_threshold(job_description):
                        skipped_experience += 1
                        continue

                    if _stated_max_experience_below_floor(job_description):
                        skipped_underqualified += 1
                        continue

                    # AI evaluation (CV loaded once, passed in — not reloaded per job).
                    # A short pause before each call keeps us under the free-tier
                    # requests-per-minute cap instead of relying only on retries
                    # after the limit is already hit.
                    if scan_state is not None:
                        scan_state["message"] = f"{comp_name}: evaluating \"{job_title[:40]}\" (pausing {adaptive_delay}s to respect API limits)..."
                    time.sleep(adaptive_delay)
                    result = evaluate_job_match(job_description, cv_text)
                    if result.get("error"):
                        _log_error(cursor, comp_id, run_id, "AI_EVALUATION_ERROR", result["error"])
                        errors_logged += 1
                        if "RATE_LIMIT" in result["error"] or "SERVER_UNAVAILABLE" in result["error"]:
                            adaptive_delay = min(adaptive_delay + 10, MAX_EVAL_DELAY_SECONDS)
                    else:
                        adaptive_delay = max(EVAL_DELAY_SECONDS, adaptive_delay - 2)

                    if result.get("error"):
                        # Don't save a fake "Not a Match" for a job that never
                        # actually got evaluated. Skipping the insert entirely
                        # means it's not in the jobs table yet, so it won't be
                        # caught by the "already known" dedup check next scan —
                        # it'll get a genuine fresh evaluation attempt instead
                        # of being permanently stuck at a bogus 0% score.
                        continue

                    # Hard filter: experience — never overridden by a high match score
                    exp_min = result.get("required_experience_min")
                    if isinstance(exp_min, (int, float)) and exp_min > MAX_EXPERIENCE_MIN:
                        skipped_experience += 1
                        continue

                    rationale = result.get("rationale", "")
                    if not verified:
                        rationale = "Posting date could not be verified. " + rationale

                    exp_max = result.get("required_experience_max")
                    experience_text = f"{exp_min}-{exp_max} yrs" if exp_min else "Not specified"

                    job_id_value = external_job_id if external_job_id else f"URLHASH-{abs(hash(job_url)) % (10 ** 10)}"

                    if is_postgres():
                        insert_sql = """INSERT INTO jobs (
                               job_id, company_id, run_id, job_title, location,
                               required_experience_min, required_experience_max, experience_text,
                               posting_date, posting_date_verified, date_discovered, last_seen_date,
                               job_url, source, match_score, match_rationale, job_status
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT (job_url) DO NOTHING"""
                    else:
                        insert_sql = """INSERT OR IGNORE INTO jobs (
                               job_id, company_id, run_id, job_title, location,
                               required_experience_min, required_experience_max, experience_text,
                               posting_date, posting_date_verified, date_discovered, last_seen_date,
                               job_url, source, match_score, match_rationale, job_status
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

                    cursor.execute(
                        q(insert_sql),
                        (
                            job_id_value, comp_id, run_id, job_title, location,
                            exp_min, exp_max, experience_text,
                            posting_date, 1 if verified else 0, scan_date, scan_date,
                            job_url, source_label, result.get("match_score", 0), rationale, "New",
                        ),
                    )
                    if cursor.rowcount > 0:
                        jobs_found += 1
                    else:
                        # Already existed (same job_url) — this is a rediscovery, not a new job.
                        # Update last_seen_date and revive it from Old back to New, but leave
                        # manually-set pipeline statuses (Applied, Interview, etc.) untouched.
                        cursor.execute(
                            q("""UPDATE jobs SET last_seen_date = ?,
                               job_status = CASE WHEN job_status = 'Old' THEN 'New' ELSE job_status END
                               WHERE job_url = ?"""),
                            (scan_date, job_url),
                        )
                    conn.commit()

            final_status = "INCOMPLETE" if source_errors else "COMPLETED"
            cursor.execute(
                q("UPDATE scan_records SET status = ? WHERE company_id = ? AND run_id = ?"),
                (final_status, comp_id, run_id),
            )
            cursor.execute(
                q("""UPDATE companies SET scan_status = ?,
                   first_scanned_date = COALESCE(first_scanned_date, ?),
                   last_scanned_date = ? WHERE id = ?"""),
                (final_status, scan_date, scan_date, comp_id),
            )
            conn.commit()
            companies_completed += 1
            time.sleep(5)  # slightly longer pause now that 8 sites are hit per company

        except Exception as e:
            cursor.execute(
                q("UPDATE scan_records SET status = 'ERROR', error_reason = ? WHERE company_id = ? AND run_id = ?"),
                (str(e), comp_id, run_id),
            )
            cursor.execute(q("UPDATE companies SET scan_status = 'ERROR' WHERE id = ?"), (comp_id,))
            _log_error(cursor, comp_id, run_id, "SCRAPING_ERROR", str(e))
            conn.commit()
            errors_logged += 1

    end_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        q("""UPDATE search_runs SET end_time = ?, companies_scanned = ?,
           relevant_jobs_found = ?, errors = ? WHERE id = ?"""),
        (end_time, companies_completed, jobs_found, errors_logged, run_id),
    )
    conn.commit()
    conn.close()

    summary = (
        f"Scan complete: {companies_completed} companies processed, "
        f"{jobs_found} jobs saved. Skipped — location: {skipped_location}, stale: {skipped_stale}, "
        f"over-experienced: {skipped_experience}, underqualified: {skipped_underqualified}, "
        f"already known: {skipped_duplicate}. {errors_logged} errors logged."
    )

    if scan_state is not None:
        scan_state["running"] = False
        scan_state["message"] = summary

    return summary
