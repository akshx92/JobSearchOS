import sqlite3

DB_FILE = "job_search_os.db"


def setup_tracking_tables():
    print("1. Connecting to database...")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    print("2. Creating CV Management and Tracking tables...")

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS cv_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        uploaded_date TEXT NOT NULL,
        is_active BOOLEAN DEFAULT 0,
        file_content BLOB
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS search_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_time TEXT NOT NULL,
        end_time TEXT,
        cv_version_id INTEGER,
        companies_scanned INTEGER DEFAULT 0,
        relevant_jobs_found INTEGER DEFAULT 0,
        errors INTEGER DEFAULT 0,
        FOREIGN KEY (cv_version_id) REFERENCES cv_versions (id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS scan_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER,
        run_id INTEGER,
        scan_date TEXT NOT NULL,
        status TEXT DEFAULT 'IN_PROGRESS',
        cv_version_id INTEGER,
        error_reason TEXT,
        FOREIGN KEY (company_id) REFERENCES companies (id),
        FOREIGN KEY (run_id) REFERENCES search_runs (id),
        FOREIGN KEY (cv_version_id) REFERENCES cv_versions (id)
    )
    ''')

    # NOTE: job_url has a UNIQUE constraint so duplicate prevention actually works.
    # required_experience_min/max are populated by the AI evaluator and used to
    # enforce the hard experience filter (roles requiring 12+ yrs min are excluded).
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT,
        company_id INTEGER,
        run_id INTEGER,
        job_title TEXT NOT NULL,
        location TEXT,
        required_experience_min INTEGER,
        required_experience_max INTEGER,
        experience_text TEXT,
        posting_date TEXT,
        posting_date_verified INTEGER DEFAULT 0,
        date_discovered TEXT NOT NULL,
        last_seen_date TEXT,
        job_url TEXT UNIQUE,
        source TEXT,
        match_score INTEGER,
        match_rationale TEXT,
        job_status TEXT DEFAULT 'New',
        link_verified INTEGER DEFAULT 0,
        last_verified_date TEXT,
        FOREIGN KEY (company_id) REFERENCES companies (id),
        FOREIGN KEY (run_id) REFERENCES search_runs (id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS saved_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER UNIQUE,
        saved_date TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES jobs (id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS error_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER,
        run_id INTEGER,
        timestamp TEXT NOT NULL,
        error_type TEXT,
        error_message TEXT,
        is_resolved BOOLEAN DEFAULT 0,
        FOREIGN KEY (company_id) REFERENCES companies (id),
        FOREIGN KEY (run_id) REFERENCES search_runs (id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''')

    conn.commit()
    conn.close()
    print("3. Success! All tracking, job, and CV tables are set up.")


if __name__ == "__main__":
    setup_tracking_tables()
