import os
import re
import datetime
import threading
import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx
import search_engine
from db_utils import get_connection, is_postgres, q, get_scan_lock_status

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None
    execute_values = None

st.set_page_config(page_title="Job Search OS", page_icon="🎯", layout="wide")

# CRITICAL: st.secrets is not reliably readable from background threads
# (that's exactly how scans run), which was causing search_engine.py to
# silently fall back to a local, table-less SQLite file mid-scan. Copying
# the value into a plain environment variable here — in the main thread, at
# startup — makes it visible to any thread for the rest of the process.
if "DATABASE_URL" not in os.environ and hasattr(st, "secrets") and "DATABASE_URL" in st.secrets:
    os.environ["DATABASE_URL"] = st.secrets["DATABASE_URL"]

if "APP_PASSWORD" not in os.environ and hasattr(st, "secrets") and "APP_PASSWORD" in st.secrets:
    os.environ["APP_PASSWORD"] = st.secrets["APP_PASSWORD"]


def check_password():
    """Blocks access to the entire app until the correct password is
    entered. This app shows real names, phone numbers, and emails from your
    MBA referral list, plus your CV — it must never be left open to anyone
    with the URL. If no APP_PASSWORD is configured at all (e.g. local
    testing), this check is skipped so local development still works."""
    configured_password = os.environ.get("APP_PASSWORD")
    if not configured_password:
        return True

    if st.session_state.get("authenticated", False):
        return True

    st.title("🔒 Job Search OS")
    st.caption("This app contains private contact information. Enter the password to continue.")
    entered = st.text_input("Password", type="password", key="password_attempt")
    if st.button("Enter"):
        if entered == configured_password:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


if not check_password():
    st.stop()


def queue_toast(message, icon="✅"):
    """Queues a lightweight toast notification to show right after the next
    rerun, instead of a full-width success/warning banner that shifts the
    page layout. st.toast() calls made right before st.rerun() get lost
    since the rerun interrupts them, so we stash it in session_state and
    display it at the very top of the next script run instead."""
    if "pending_toasts" not in st.session_state:
        st.session_state.pending_toasts = []
    st.session_state.pending_toasts.append((message, icon))

# ---------------------------------------------------------------------------
# STYLING
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    .stApp { background-color: #0e1117; }
    [data-testid="stHeader"] {
        background: transparent !important;
        box-shadow: none !important;
    }
    [data-testid="stHeader"]::before,
    [data-testid="stHeader"]::after { display: none !important; }
    [data-testid="stDecoration"] {
        display: none !important;
        background-image: none !important;
    }
    [data-testid="stAppViewContainer"] { background-image: none !important; }
    .block-container { padding-top: 1.5rem !important; max-width: 1400px; }

    .badge {
        display: inline-block; padding: 3px 12px; border-radius: 999px;
        font-size: 0.78rem; font-weight: 600; margin-right: 6px;
    }
    .badge-strong { background: rgba(34, 197, 94, 0.15); color: #4ade80; }
    .badge-possible { background: rgba(245, 158, 11, 0.15); color: #fbbf24; }
    .badge-nomatch { background: rgba(148, 163, 184, 0.15); color: #94a3b8; }
    .badge-contact { background: rgba(99, 102, 241, 0.18); color: #a5b4fc; }
    .badge-nocontact { background: rgba(148, 163, 184, 0.10); color: #64748b; }
</style>
""", unsafe_allow_html=True)


def clean(val):
    """Guards against legacy 'None'/'nan' strings saved by older buggy scans."""
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("none", "nan") else s


def match_tier(score):
    if score >= 80:
        return "Strong Match", "badge-strong"
    elif score >= 55:
        return "Possible Match", "badge-possible"
    else:
        return "Not a Match", "badge-nomatch"


def tier_breakdown_str(comp_jobs):
    total = len(comp_jobs)
    if total == 0:
        return "0 jobs"
    strong = len(comp_jobs[comp_jobs["match_score"] >= 80])
    possible = len(comp_jobs[(comp_jobs["match_score"] >= 55) & (comp_jobs["match_score"] < 80)])
    weak = total - strong - possible
    parts = []
    if strong:
        parts.append(f"{strong} Strong")
    if possible:
        parts.append(f"{possible} Possible")
    if weak:
        parts.append(f"{weak} Not a Match")
    return f"{total} jobs (" + ", ".join(parts) + ")" if parts else f"{total} jobs"


def get_setting(key, default=""):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(q("SELECT value FROM settings WHERE key = ?"), (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key, value):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        q("INSERT INTO settings (key, value) VALUES (?, ?) "
          "ON CONFLICT(key) DO UPDATE SET value = excluded.value"),
        (key, value),
    )
    conn.commit()
    conn.close()


def build_outreach_message(user_name, contact_name, company_name, job_title, mba_college):
    name_part = contact_name if contact_name else "there"
    college_line = f" Since we share an MBA connection through {mba_college}," if mba_college else ""
    signer = user_name if user_name else "[Your Name]"
    return (
        f"Hi {name_part},\n\n"
        f"I hope you're doing well!{college_line} I came across the {job_title} opening at "
        f"{company_name} and wanted to reach out directly, since your background there stood out to me.\n\n"
        f"I have relevant experience in project/program delivery and would love to learn more about "
        f"the role and team. If you're open to it, I'd really appreciate any insights you can share, "
        f"or a referral if you think it could be a good fit.\n\n"
        f"Happy to share my CV or connect for a quick call whenever convenient.\n\n"
        f"Thanks so much for your time!\n\n"
        f"Best regards,\n{signer}"
    )


# ---------------------------------------------------------------------------
# NORMALIZATION & MBA REFERRAL IMPORT
# ---------------------------------------------------------------------------
NOISE_RE = re.compile(
    r"\(.*?\)"
    r"|\bstill to join\b|\byet to join\b|\bto join\b|\bjoining soon\b|\bjoining shortly\b"
    r"|\bjoining next month\b|\bcurrently at\b|\bpreviously (?:at|with)\b|\bex[\s\-]+"
    r"|\bformerly (?:at|with)\b",
    re.IGNORECASE
)
PLACEHOLDERS = {"na", "n/a", "none", "self employed", "freelance", "freelancer", "tbd", "-", "unemployed"}

COMPANY_ALIASES = {
    "jpmorgan": "jpmorganchase",
    "jpmorgangroup": "jpmorganchase",
    "ernstyoung": "ey",
    "boozallen": "boozallenhamilton",
    "bcg": "bostonconsultinggroup",
    "mckinsey": "mckinseycompany",
}


def _fast_clean_company(raw):
    text = NOISE_RE.sub("", str(raw or ""))
    text = re.split(r"\s+-\s+|\s*,\s*", text, maxsplit=1)[0]
    return re.sub(r"\s+", " ", text).strip(" -,:;")


def _fast_normalize_company(name):
    cleaned = _fast_clean_company(name).lower()
    norm = re.sub(r"[^a-z0-9]", "", cleaned)
    return COMPANY_ALIASES.get(norm, norm)


def import_mba_excel(file_path):
    """Refreshes contacts from an updated MBA referral Excel file using
    bulk operations and flexible header detection."""
    df = pd.read_excel(file_path)

    col_map = {str(c).strip().lower(): c for c in df.columns}

    def find_col(candidates):
        for candidate in candidates:
            for clean_name, orig in col_map.items():
                if candidate in clean_name:
                    return orig
        return None

    col_company = find_col(["current company", "company", "organization"])
    col_name = find_col(["your full name", "full name", "contact name", "name"])
    col_phone = find_col(["whatsapp", "phone", "mobile", "contact no", "number"])
    col_college = find_col(["mba college", "college", "institute", "b-school"])

    if not col_company:
        return 0, 0

    conn = get_connection()
    cur = conn.cursor()

    # 1. Clear existing contacts
    cur.execute("DELETE FROM contacts")

    # 2. Fetch existing companies map
    cur.execute("SELECT normalized_name, id FROM companies")
    company_map = dict(cur.fetchall())

    new_companies_dict = {}
    parsed_rows = []

    for _, row in df.iterrows():
        raw_company = row.get(col_company, "")
        orig_cleaned = _fast_clean_company(raw_company)
        if not orig_cleaned or orig_cleaned.lower() in PLACEHOLDERS or orig_cleaned.lower() == "nan":
            continue

        norm = _fast_normalize_company(orig_cleaned)
        if norm not in company_map and norm not in new_companies_dict:
            new_companies_dict[norm] = orig_cleaned

        parsed_rows.append((
            norm,
            str(row.get(col_name, "") or "") if col_name else "",
            str(row.get(col_phone, "") or "") if col_phone else "",
            str(row.get(col_college, "") or "") if col_college else ""
        ))

    # 3. Bulk insert new companies
    new_companies_count = len(new_companies_dict)
    if new_companies_dict:
        comp_tuples = [(orig, norm) for norm, orig in new_companies_dict.items()]
        if is_postgres():
            insert_comp_sql = """
                INSERT INTO companies (original_name, normalized_name)
                VALUES %s
                ON CONFLICT (normalized_name) DO NOTHING
                RETURNING normalized_name, id
            """
            returned = execute_values(cur, insert_comp_sql, comp_tuples, fetch=True, page_size=500)
            if returned:
                for norm, c_id in returned:
                    company_map[norm] = c_id
        else:
            # Local SQLite fallback — no execute_values available, so a plain
            # per-row loop instead. Fine for local dev volumes.
            for orig, norm in comp_tuples:
                cur.execute(
                    "INSERT OR IGNORE INTO companies (original_name, normalized_name) VALUES (?, ?)",
                    (orig, norm),
                )
                cur.execute("SELECT id FROM companies WHERE normalized_name = ?", (norm,))
                row = cur.fetchone()
                if row:
                    company_map[norm] = row[0]

    # 4. Bulk insert contacts
    contacts_to_insert = []
    for norm, name, phone, college in parsed_rows:
        c_id = company_map.get(norm)
        if c_id:
            contacts_to_insert.append((c_id, name, phone, "", college))

    total_contacts = len(contacts_to_insert)
    if contacts_to_insert:
        if is_postgres():
            insert_contacts_sql = """
                INSERT INTO contacts (company_id, contact_name, phone, email, mba_college)
                VALUES %s
            """
            execute_values(cur, insert_contacts_sql, contacts_to_insert, page_size=1000)
        else:
            cur.executemany(
                "INSERT INTO contacts (company_id, contact_name, phone, email, mba_college) VALUES (?, ?, ?, ?, ?)",
                contacts_to_insert,
            )

    conn.commit()
    conn.close()
    return new_companies_count, total_contacts


@st.dialog("Replace all contacts from this file?")
def confirm_mba_import_dialog(mba_path):
    st.write("This will completely replace your current contacts list with what's in the uploaded file.")
    st.write("Companies are only ever added to, never deleted — your scan history is safe.")
    st.warning("Double-check you selected the right file. This cannot be undone.")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Cancel", use_container_width=True):
            st.rerun()
    with c2:
        if st.button("Yes, replace contacts", type="primary", use_container_width=True):
            with st.spinner("Updating contacts..."):
                new_companies, total_contacts = import_mba_excel(mba_path)
            queue_toast(f"Done: {new_companies} new companies added, {total_contacts} contacts imported.")
            st.rerun()


# ---------------------------------------------------------------------------
# SESSION STATE
# ---------------------------------------------------------------------------
if "pending_toasts" in st.session_state and st.session_state.pending_toasts:
    for msg, icon in st.session_state.pending_toasts:
        st.toast(msg, icon=icon)
    st.session_state.pending_toasts = []

if "scan_state" not in st.session_state:
    st.session_state.scan_state = {"running": False, "current": 0, "total": 0, "message": ""}
if "scan_was_running" not in st.session_state:
    st.session_state.scan_was_running = False
if "stop_event" not in st.session_state:
    st.session_state.stop_event = threading.Event()
if "comp_page" not in st.session_state:
    st.session_state.comp_page = 1
if "pipeline_cache" not in st.session_state:
    st.session_state.pipeline_cache = None
if "pipeline_contacts_cache" not in st.session_state:
    st.session_state.pipeline_contacts_cache = {}
if "pipeline_username_cache" not in st.session_state:
    st.session_state.pipeline_username_cache = ""
if "pipeline_needs_refresh" not in st.session_state:
    st.session_state.pipeline_needs_refresh = True


def invalidate_pipeline_cache():
    """Call this after anything that actually changes job data (status
    updates, a scan finishing). Pure UI interactions — checkbox, sort,
    pagination — should NOT call this; they just re-filter the cached data
    in memory, which is what makes them instant instead of re-hitting the
    database on every click."""
    st.session_state.pipeline_needs_refresh = True


def start_scan(mode, limit, single_company=None):
    st.session_state.stop_event = threading.Event()
    st.session_state.scan_state.update(
        {"running": True, "current": 0, "total": limit, "message": "Starting scan..."}
    )
    st.session_state.scan_was_running = True
    t = threading.Thread(
        target=search_engine.run_scan,
        kwargs={
            "limit": limit,
            "mode": mode,
            "scan_state": st.session_state.scan_state,
            "stop_event": st.session_state.stop_event,
            "single_company": single_company,
            "owner_label": f"app:{mode}",
        },
        daemon=True,
    )
    add_script_run_ctx(t)
    t.start()


def reset_all_scan_data():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM jobs")
    cur.execute("DELETE FROM scan_records")
    cur.execute("DELETE FROM search_runs")
    cur.execute("DELETE FROM error_log")
    cur.execute("DELETE FROM saved_jobs")
    cur.execute("UPDATE companies SET scan_status = 'NOT_SCANNED', first_scanned_date = NULL, last_scanned_date = NULL")
    conn.commit()
    conn.close()


@st.dialog("Reset all scan data?")
def confirm_reset_dialog():
    st.write("This permanently deletes:")
    st.markdown("- All discovered jobs\n- All scan records and search run history\n- All logged errors\n- All saved jobs")
    st.write("Your **company list**, **MBA contacts**, and **CV versions** will NOT be touched.")
    st.warning("This cannot be undone.")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Cancel", use_container_width=True):
            st.rerun()
    with c2:
        if st.button("Yes, reset everything", type="primary", use_container_width=True):
            reset_all_scan_data()
            st.session_state.comp_page = 1
            st.rerun()


# ---------------------------------------------------------------------------
# SIDEBAR — order: Scan, then Files, then Your Details
# ---------------------------------------------------------------------------
st.sidebar.title("⚙️ Controls")
st.sidebar.markdown("---")

# --- SCAN (top) ---
st.sidebar.subheader("🚀 Scan")
st.sidebar.caption("Companies with an MBA contact are scanned first.")
batch_limit = st.sidebar.slider("Batch limit (companies)", min_value=1, max_value=50, value=10, step=1)

scan_disabled = st.session_state.scan_state["running"]

col_a, col_b = st.sidebar.columns(2)
with col_a:
    if st.button("New Batch", disabled=scan_disabled, use_container_width=True):
        start_scan("next_batch", batch_limit)
        st.rerun()
with col_b:
    if st.button("Retry Errors", disabled=scan_disabled, use_container_width=True):
        start_scan("retry_errors", batch_limit)
        st.rerun()

col_c, col_d = st.sidebar.columns(2)
with col_c:
    if st.button("New Results", disabled=scan_disabled, use_container_width=True):
        start_scan("freshness_check", batch_limit)
        st.rerun()
with col_d:
    if st.button("🗑️ Reset Data", disabled=scan_disabled, use_container_width=True):
        confirm_reset_dialog()

if st.sidebar.button("🔗 Verify Links (30)", disabled=scan_disabled, use_container_width=True):
    with st.spinner("Checking stored job links..."):
        verify_msg = search_engine.verify_job_links(limit=30)
    queue_toast(verify_msg)
    st.rerun()


@st.fragment(run_every=2 if st.session_state.scan_state["running"] else None)
def scan_progress_widget():
    state = st.session_state.scan_state
    if state["running"]:
        pct = state["current"] / state["total"] if state["total"] else 0
        st.progress(min(pct, 1.0))
        st.caption(state["message"])
        if st.button("⏹ Stop Scan", key="stop_scan_btn", use_container_width=True):
            st.session_state.stop_event.set()
    else:
        if st.session_state.scan_was_running:
            st.session_state.scan_was_running = False
            invalidate_pipeline_cache()
            st.rerun()
        if state["message"]:
            st.info(state["message"])


with st.sidebar:
    scan_progress_widget()

with st.sidebar.expander("🔴 Recent Errors"):
    conn = get_connection()
    errors_df = pd.read_sql_query("""
        SELECT e.timestamp, COALESCE(c.original_name, 'Unknown') as company_name,
               e.error_type, e.error_message
        FROM error_log e
        LEFT JOIN companies c ON e.company_id = c.id
        ORDER BY e.timestamp DESC
        LIMIT 20
    """, conn)
    conn.close()

    if errors_df.empty:
        st.caption("No errors logged.")
    else:
        for _, err in errors_df.iterrows():
            st.markdown(f"**{err['company_name']}** — {err['error_type']}")
            st.caption(f"{err['timestamp']}")
            st.caption(clean(err['error_message'])[:200])
            st.markdown("---")

# --- FILES (middle) ---
st.sidebar.markdown("---")
with st.sidebar.expander("📁 Files (CV / Referral List)"):
    file_type = st.selectbox("Manage", ["CV", "Referral File"], label_visibility="collapsed")

    if file_type == "CV":
        conn = get_connection()
        active_cv_row = pd.read_sql_query("SELECT filename, uploaded_date FROM cv_versions WHERE is_active = TRUE", conn)
        conn.close()

        if not active_cv_row.empty:
            st.success(f"Active: {active_cv_row.iloc[0]['filename']}")
            st.caption(f"Uploaded: {active_cv_row.iloc[0]['uploaded_date']}")
        else:
            st.warning("No active CV set.")

        uploaded_cv = st.file_uploader("Upload new CV (PDF)", type=["pdf"])
        if uploaded_cv is not None and st.button("Set as Active CV", use_container_width=True):
            filename = uploaded_cv.name or f"CV_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            file_bytes = uploaded_cv.getvalue()
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("UPDATE cv_versions SET is_active = FALSE")
            cur.execute(
                q("INSERT INTO cv_versions (filename, uploaded_date, is_active, file_content) VALUES (?, ?, TRUE, ?)"),
                (filename, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 psycopg2.Binary(file_bytes) if is_postgres() else file_bytes),
            )
            conn.commit()
            conn.close()
            queue_toast(f"Active CV updated: {filename}")
            st.rerun()

    else:  # Referral File
        st.caption("Upload monthly to refresh contacts. Existing scan history is preserved — only new companies get added.")
        uploaded_mba = st.file_uploader("Upload updated MBA Excel", type=["xlsx"])
        if uploaded_mba is not None and st.button("Update Contacts from Excel", use_container_width=True):
            mba_path = "MBA referrals - Mumbai - List of Members.xlsx"
            with open(mba_path, "wb") as f:
                f.write(uploaded_mba.getbuffer())
            confirm_mba_import_dialog(mba_path)

with st.sidebar.expander("👤 Your Details"):
    user_name_input = st.text_input("Your name (used in outreach messages)", value=get_setting("user_name", ""))
    if st.button("Save Name", use_container_width=True):
        set_setting("user_name", user_name_input)
        invalidate_pipeline_cache()
        st.toast("Saved")

# ---------------------------------------------------------------------------
# MAIN HEADER + METRICS
# ---------------------------------------------------------------------------
st.title("🎯 Job Search OS")


def load_jobs_df():
    conn = get_connection()
    df = pd.read_sql_query("""
        SELECT j.*, COALESCE(c.original_name, 'Unknown Company') as company_name,
               (SELECT COUNT(*) FROM contacts WHERE contacts.company_id = j.company_id) as contact_count
        FROM jobs j
        LEFT JOIN companies c ON j.company_id = c.id
        ORDER BY j.date_discovered DESC, j.match_score DESC
    """, conn)
    conn.close()
    if not df.empty:
        df["has_contact"] = df["contact_count"] > 0
    return df


conn = get_connection()
companies_df = pd.read_sql_query("""
    SELECT c.*,
           (SELECT COUNT(*) FROM contacts WHERE contacts.company_id = c.id) as contact_count
    FROM companies c ORDER BY c.id ASC
""", conn)
conn.close()
jobs_df = load_jobs_df()

if not companies_df.empty:
    companies_df["has_contact"] = companies_df["contact_count"] > 0
    if not jobs_df.empty:
        job_counts = jobs_df.groupby("company_id").size().rename("job_count")
        companies_df = companies_df.merge(job_counts, left_on="id", right_index=True, how="left")
    else:
        companies_df["job_count"] = 0
    companies_df["job_count"] = companies_df["job_count"].fillna(0).astype(int)

conn = get_connection()
error_log_count = pd.read_sql_query("SELECT COUNT(*) as c FROM error_log", conn).iloc[0]["c"]
conn.close()

total_jobs = len(jobs_df)
top_match = int(jobs_df["match_score"].max()) if total_jobs > 0 else 0
scanned_count = len(companies_df[companies_df["scan_status"] == "COMPLETED"])
pending_count = len(companies_df[companies_df["scan_status"] == "NOT_SCANNED"])
error_count = int(error_log_count)
incomplete_count = len(companies_df[companies_df["scan_status"] == "INCOMPLETE"])
actionable_count = len(jobs_df[(jobs_df["match_score"] >= 55) & (jobs_df["has_contact"])]) if total_jobs > 0 else 0

m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
m1.metric("🎯 Ready to Reach Out", actionable_count)
m2.metric("Total Jobs", total_jobs)
m3.metric("Top Match", f"{top_match}%")
m4.metric("Scanned", scanned_count)
m5.metric("Pending", pending_count)
m6.metric("Incomplete", incomplete_count)
m7.metric("Errors", error_count)

# Last Scan status — surfaces runs triggered from anywhere (this app, a
# manual Recheck, or an external scheduler like GitHub Actions), so a
# failure is visible here without needing to check GitHub's own logs.
is_locked, lock_owner, lock_since = get_scan_lock_status()
if is_locked:
    st.info(f"🔄 A scan is currently running (triggered by **{lock_owner}**, started {lock_since}).")
else:
    conn = get_connection()
    last_run = pd.read_sql_query(
        "SELECT start_time, end_time, companies_scanned, relevant_jobs_found, errors FROM search_runs ORDER BY id DESC LIMIT 1",
        conn,
    )
    conn.close()
    if not last_run.empty:
        r = last_run.iloc[0]
        status_note = "✅" if r["errors"] == 0 else f"⚠️ {r['errors']} errors"
        finished = "still running or crashed without finishing" if not r["end_time"] else f"finished {r['end_time']}"
        st.caption(f"Last scan: started {r['start_time']}, {finished} · {r['companies_scanned']} companies, {r['relevant_jobs_found']} jobs found · {status_note}")

st.markdown("---")


def get_contacts_for_companies(company_ids):
    """Fetches contacts for ALL given companies in a single query, instead of
    one query per card. Popovers in Streamlit are not lazy — their contents
    run on every script rerun whether opened or not — so without this,
    every visible card triggers its own blocking database round-trip, which
    is what was causing cards to render slowly and the checkbox toggle to
    show a stale/overlapping mix of results mid-render."""
    ids = sorted(set(int(c) for c in company_ids if c is not None))
    if not ids:
        return {}
    placeholders = ", ".join(["?"] * len(ids))
    conn = get_connection()
    df = pd.read_sql_query(
        q(f"SELECT company_id, contact_name, phone, email, mba_college FROM contacts WHERE company_id IN ({placeholders})"),
        conn, params=ids,
    )
    conn.close()
    return {cid: group.reset_index(drop=True) for cid, group in df.groupby("company_id")}


def render_contact_and_outreach(job_id, company_id, job_title, company_name, contacts_map, user_name, key_prefix="pl"):
    contacts = contacts_map.get(company_id, pd.DataFrame())
    if contacts.empty:
        st.warning("⚠️ No referral contact available in MBA database.")
        return

    for idx, row in contacts.iterrows():
        name, phone, email, college = clean(row["contact_name"]), clean(row["phone"]), clean(row["email"]), clean(row["mba_college"])
        completeness = "Complete" if name and phone else "Partial contact"
        st.markdown(f"**Contact {idx + 1}** — _{completeness}_")
        if name:
            st.write(f"Name: {name}")
        if phone:
            st.write(f"Phone: {phone}")
        if email:
            st.write(f"Email: {email}")
        if college:
            st.write(f"MBA College: {college}")
        if not (name or phone or email):
            st.warning("⚠️ Partial/empty contact record.")
            st.markdown("---")
            continue

        message = build_outreach_message(user_name, name, company_name, job_title, college)
        st.caption("Suggested outreach message (edit before sending):")
        st.code(message, language=None)

        if st.button("✅ Mark as Contacted", key=f"contacted_{key_prefix}_{job_id}_{idx}", use_container_width=True):
            update_job_status(job_id, "Contacted")
            invalidate_pipeline_cache()
            queue_toast("Marked as Contacted.")
            st.rerun()

        st.markdown("---")


def update_job_status(job_row_id, new_status):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(q("UPDATE jobs SET job_status = ? WHERE id = ?"), (new_status, job_row_id))
    conn.commit()
    conn.close()


PIPELINE_COLUMNS = [
    ("📥 To Review", ["New", "Old"]),
    ("📇 Contact to Approach", ["Contact to approach"]),
    ("✉️ Contacted", ["Contacted"]),
    ("🤝 Referral Received", ["Referral received"]),
    ("📝 Applied", ["Applied"]),
    ("🎤 Interview", ["Interview"]),
    ("🏆 Offer", ["Offer"]),
    ("🗑️ Rejected", ["Rejected"]),
]
NEXT_STAGE = {
    "New": "Contact to approach", "Old": "Contact to approach",
    "Contact to approach": "Contacted",
    "Contacted": "Referral received",
    "Referral received": "Applied",
    "Applied": "Interview",
    "Interview": "Offer",
}
PIPELINE_PAGE_SIZE = 9


def render_pipeline_card(job, contacts_map, user_name, show_advance=True):
    tier_label, tier_class = match_tier(job["match_score"])
    contact_class = "badge-contact" if job["has_contact"] else "badge-nocontact"
    contact_label = "Has contact" if job["has_contact"] else "No contact"
    is_rejected = job["job_status"] == "Rejected"

    with st.container(border=True):
        st.markdown(f"**{job['job_title']}**")
        st.caption(job["company_name"])
        st.markdown(
            f'<span class="badge {tier_class}">{tier_label} · {job["match_score"]}%</span>'
            f'<span class="badge {contact_class}">{contact_label}</span>',
            unsafe_allow_html=True,
        )

        if job["job_url"]:
            st.markdown(f"[View Job ↗]({job['job_url']})")

        with st.popover("Contact & Outreach", use_container_width=True):
            render_contact_and_outreach(job["id"], job["company_id"], job["job_title"], job["company_name"], contacts_map, user_name, key_prefix="pl")

        if is_rejected:
            if st.button("↩ Restore to Review", key=f"restore_{job['id']}", use_container_width=True):
                update_job_status(job["id"], "New")
                invalidate_pipeline_cache()
                st.rerun()
        else:
            btn_cols = st.columns(2)
            with btn_cols[0]:
                next_stage = NEXT_STAGE.get(job["job_status"])
                if show_advance and next_stage:
                    if st.button(f"→ {next_stage}", key=f"advance_{job['id']}", use_container_width=True):
                        update_job_status(job["id"], next_stage)
                        invalidate_pipeline_cache()
                        st.rerun()
            with btn_cols[1]:
                if st.button("✕ Reject", key=f"reject_{job['id']}", use_container_width=True):
                    update_job_status(job["id"], "Rejected")
                    invalidate_pipeline_cache()
                    st.rerun()


tab_pipeline, tab_companies = st.tabs(["🎯 Pipeline", "🏢 Company Directory"])

# ---------------------------------------------------------------------------
# PIPELINE
# ---------------------------------------------------------------------------
with tab_pipeline:
    @st.fragment(run_every=5 if st.session_state.scan_state["running"] else None)
    def render_pipeline():
        # Only hits the database when something actually requires fresh data:
        # first load, a live scan tick, or right after a status-changing
        # action flagged invalidate_pipeline_cache(). Pure UI interactions
        # (checkbox, sort, pagination) just re-filter the cached DataFrame in
        # memory — instant, no database round-trip, no stale-render flicker.
        needs_fresh = (
            st.session_state.pipeline_cache is None
            or st.session_state.pipeline_needs_refresh
            or st.session_state.scan_state["running"]
        )
        if needs_fresh:
            fresh_jobs_df = load_jobs_df()
            st.session_state.pipeline_cache = fresh_jobs_df
            # Computed from the FULL job set (not the filtered view below), so
            # toggling the checkbox/sort/pagination never needs to touch these
            # again — they only change when the underlying data actually does.
            all_company_ids = fresh_jobs_df["company_id"].unique().tolist() if not fresh_jobs_df.empty else []
            st.session_state.pipeline_contacts_cache = get_contacts_for_companies(all_company_ids)
            st.session_state.pipeline_username_cache = get_setting("user_name", "")
            st.session_state.pipeline_needs_refresh = False
        jobs_df = st.session_state.pipeline_cache
        contacts_map = st.session_state.pipeline_contacts_cache
        user_name = st.session_state.pipeline_username_cache

        if jobs_df.empty:
            st.info("No jobs yet. Run a scan from the sidebar to get started.")
            return

        f1, f2, f3 = st.columns([2, 1, 3])
        with f1:
            show_all_scores = st.checkbox("Include low-relevance matches (below 55%)", value=False)
        with f2:
            sort_option = st.selectbox(
                "Sort by", ["Newest posting date", "Highest match score", "Company A-Z"]
            )

        base_df = jobs_df if show_all_scores else jobs_df[jobs_df["match_score"] >= 55]
        base_df = base_df[base_df["job_status"] != "Closed/Expired"]

        if base_df.empty:
            st.info("No actionable matches yet. Try lowering the score filter above, or run more scans.")
            return

        if sort_option == "Newest posting date":
            base_df = base_df.sort_values(by="posting_date", ascending=False, na_position="last")
        elif sort_option == "Highest match score":
            base_df = base_df.sort_values(by="match_score", ascending=False)
        else:
            base_df = base_df.sort_values(by="company_name", ascending=True)

        stage_dfs = [base_df[base_df["job_status"].isin(statuses)].copy() for _, statuses in PIPELINE_COLUMNS]

        tab_labels = [f"{label} ({len(df)})" for (label, _), df in zip(PIPELINE_COLUMNS, stage_dfs)]
        stage_tabs = st.tabs(tab_labels)

        for stage_idx, (stage_tab, stage_df) in enumerate(zip(stage_tabs, stage_dfs)):
            with stage_tab:
                if stage_df.empty:
                    st.caption("Nothing here yet.")
                    continue

                page_key = f"pipeline_page_{stage_idx}"
                if page_key not in st.session_state:
                    st.session_state[page_key] = 1

                total_items = len(stage_df)
                total_pages = max(1, (total_items + PIPELINE_PAGE_SIZE - 1) // PIPELINE_PAGE_SIZE)
                st.session_state[page_key] = min(st.session_state[page_key], total_pages)

                if total_pages > 1:
                    spacer, page_col = st.columns([4, 1])
                    with page_col:
                        st.session_state[page_key] = st.number_input(
                            "Page", min_value=1, max_value=total_pages,
                            value=st.session_state[page_key], step=1, key=f"{page_key}_input",
                        )

                start = (st.session_state[page_key] - 1) * PIPELINE_PAGE_SIZE
                page_df = stage_df.iloc[start:start + PIPELINE_PAGE_SIZE]

                card_cols = st.columns(3)
                for idx, (_, job) in enumerate(page_df.iterrows()):
                    with card_cols[idx % 3]:
                        render_pipeline_card(job, contacts_map, user_name)

    render_pipeline()

# ---------------------------------------------------------------------------
# COMPANY DIRECTORY
# ---------------------------------------------------------------------------
with tab_companies:
    @st.fragment
    def render_company_directory(companies_df, jobs_df):
        st.subheader("Company Directory")
        if companies_df.empty:
            st.info("No companies found. Upload your referral Excel file in the sidebar to populate.")
            return

        sc1, sc2 = st.columns([2, 2])
        with sc1:
            search_query = st.text_input("Search company by name", "")
        with sc2:
            sort_option = st.selectbox(
                "Sort by", ["Company Name (A-Z)", "Most Jobs First", "Has Contact First", "Most Recently Scanned"]
            )

        filtered_companies = companies_df.copy()
        if search_query:
            filtered_companies = filtered_companies[
                filtered_companies["original_name"].str.contains(search_query, case=False, na=False)
            ]

        if sort_option == "Most Jobs First":
            filtered_companies = filtered_companies.sort_values(by="job_count", ascending=False)
        elif sort_option == "Has Contact First":
            filtered_companies = filtered_companies.sort_values(by=["has_contact", "job_count"], ascending=[False, False])
        elif sort_option == "Most Recently Scanned":
            filtered_companies = filtered_companies.sort_values(by="last_scanned_date", ascending=False, na_position="last")
        else:
            filtered_companies = filtered_companies.sort_values(by="original_name", ascending=True)

        comp_items_per_page = 15
        comp_total_items = len(filtered_companies)
        comp_total_pages = max(1, (comp_total_items + comp_items_per_page - 1) // comp_items_per_page)
        st.session_state.comp_page = min(st.session_state.comp_page, comp_total_pages)

        cc1, cc2 = st.columns([1, 4])
        with cc1:
            st.session_state.comp_page = st.number_input(
                "Directory page", min_value=1, max_value=comp_total_pages,
                value=st.session_state.comp_page, step=1
            )
        with cc2:
            st.caption(f"{comp_total_items} companies · Page {st.session_state.comp_page} of {comp_total_pages}")

        c_start = (st.session_state.comp_page - 1) * comp_items_per_page
        page_companies = filtered_companies.iloc[c_start:c_start + comp_items_per_page]

        if "selected_company_id" not in st.session_state:
            st.session_state.selected_company_id = None

        # Default to the first company on the current page if nothing (or
        # something no longer on this page) is selected, so the detail
        # panel is never empty on first load.
        visible_ids = page_companies["id"].tolist()
        if st.session_state.selected_company_id not in visible_ids and visible_ids:
            st.session_state.selected_company_id = visible_ids[0]

        list_col, detail_col = st.columns([1.3, 2.2])

        with list_col:
            for _, comp in page_companies.iterrows():
                comp_id, comp_name, status = comp["id"], comp["original_name"], comp["scan_status"]
                status_dot = {"COMPLETED": "🟢", "ERROR": "🔴", "IN_PROGRESS": "🟡", "INCOMPLETE": "🟠"}.get(status, "⚪")
                contact_mark = "📇" if comp["has_contact"] else ""
                is_selected = comp_id == st.session_state.selected_company_id
                label = f"{status_dot} {contact_mark} {comp_name}"
                if st.button(
                    label, key=f"select_company_{comp_id}", use_container_width=True,
                    type="primary" if is_selected else "secondary",
                ):
                    st.session_state.selected_company_id = comp_id
                    st.rerun()

        with detail_col:
            selected_id = st.session_state.selected_company_id
            if selected_id is None:
                st.info("Select a company from the list to see details.")
                return

            comp_row = companies_df[companies_df["id"] == selected_id]
            if comp_row.empty:
                st.info("Select a company from the list to see details.")
                return
            comp = comp_row.iloc[0]
            comp_name, status = comp["original_name"], comp["scan_status"]
            comp_jobs = jobs_df[jobs_df["company_id"] == selected_id] if not jobs_df.empty else pd.DataFrame()
            breakdown = tier_breakdown_str(comp_jobs)

            with st.container(border=True):
                st.markdown(f"### {comp_name}")
                st.markdown(
                    f'<span class="badge {"badge-contact" if comp["has_contact"] else "badge-nocontact"}">'
                    f'{"Has MBA contact" if comp["has_contact"] else "No MBA contact on file"}</span>',
                    unsafe_allow_html=True,
                )
                st.caption(f"Status: {status} · {breakdown}")
                st.caption(f"First scanned: {clean(comp['first_scanned_date']) or '—'} · Last scanned: {clean(comp['last_scanned_date']) or '—'}")

                if st.button("Recheck Now", key=f"recheck_{selected_id}", disabled=st.session_state.scan_state["running"]):
                    with st.spinner(f"Checking career pages + job boards for {comp_name}... this can take 2-3 minutes."):
                        result_msg = search_engine.run_scan(
                            limit=1, mode="recheck", single_company=(selected_id, comp_name),
                            owner_label="app:recheck",
                        )
                    invalidate_pipeline_cache()
                    queue_toast(result_msg)
                    st.rerun()

            if not comp_jobs.empty:
                st.dataframe(
                    comp_jobs[["job_title", "match_score", "job_status", "posting_date", "job_url"]],
                    width="stretch",
                    column_config={"job_url": st.column_config.LinkColumn("Link")},
                    hide_index=True,
                )
            else:
                st.write("No matching job records for this company yet.")

    render_company_directory(companies_df, jobs_df)
