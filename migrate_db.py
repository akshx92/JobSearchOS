"""
Run this ONCE to upgrade an existing job_search_os.db to the new schema.
It rebuilds the 'jobs' table to add: required_experience_min/max, experience_text,
posting_date_verified, source, link_verified, and a UNIQUE constraint on job_url
(needed so duplicate prevention actually works).

Safe to re-run: it checks whether the migration is already applied and skips if so.
"""

import sqlite3

DB_FILE = "job_search_os.db"


def column_exists(cursor, table, column):
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def migrate():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    print("Checking jobs table schema...")
    needs_rebuild = not column_exists(cursor, "jobs", "required_experience_min")

    if needs_rebuild:
        print("Rebuilding 'jobs' table with new columns + UNIQUE(job_url) constraint...")
        cursor.execute("""
            CREATE TABLE jobs_new (
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
                job_url TEXT UNIQUE,
                source TEXT,
                match_score INTEGER,
                match_rationale TEXT,
                job_status TEXT DEFAULT 'New',
                link_verified INTEGER DEFAULT 0,
                FOREIGN KEY (company_id) REFERENCES companies (id),
                FOREIGN KEY (run_id) REFERENCES search_runs (id)
            )
        """)

        cursor.execute("PRAGMA table_info(jobs)")
        old_cols = [c[1] for c in cursor.fetchall()]
        cursor.execute("SELECT * FROM jobs")
        old_rows = cursor.fetchall()

        seen_urls = set()
        migrated, skipped = 0, 0
        for row in old_rows:
            row_dict = dict(zip(old_cols, row))
            url = row_dict.get("job_url") or ""
            if not url or url in seen_urls:
                skipped += 1
                continue
            seen_urls.add(url)
            cursor.execute("""
                INSERT INTO jobs_new (
                    job_id, company_id, run_id, job_title, location,
                    experience_text, posting_date, date_discovered, job_url,
                    match_score, match_rationale, job_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row_dict.get("job_id"), row_dict.get("company_id"), row_dict.get("run_id"),
                row_dict.get("job_title"), row_dict.get("location"),
                row_dict.get("experience_requirement"), row_dict.get("posting_date"),
                row_dict.get("date_discovered"), url,
                row_dict.get("match_score"), row_dict.get("match_rationale"),
                row_dict.get("job_status"),
            ))
            migrated += 1

        cursor.execute("DROP TABLE jobs")
        cursor.execute("ALTER TABLE jobs_new RENAME TO jobs")
        print(f"Migrated {migrated} job records. Skipped {skipped} duplicate/blank-URL rows.")
        print("NOTE: any rows in 'saved_jobs' referencing old job IDs may now point to the wrong")
        print("row, since IDs were reassigned during rebuild. Given the app is still early-stage,")
        print("this is a one-time acceptable cost. Re-save any jobs you'd saved previously.")
    else:
        print("'jobs' table already has the new schema. Nothing to do.")

    if not column_exists(cursor, "jobs", "last_seen_date"):
        print("Adding 'last_seen_date' column for the Rediscovered feature...")
        cursor.execute("ALTER TABLE jobs ADD COLUMN last_seen_date TEXT")
        cursor.execute("UPDATE jobs SET last_seen_date = date_discovered WHERE last_seen_date IS NULL")
    else:
        print("'last_seen_date' column already present. Nothing to do.")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    print("'settings' table ready.")

    if not column_exists(cursor, "jobs", "last_verified_date"):
        print("Adding 'last_verified_date' column for link verification...")
        cursor.execute("ALTER TABLE jobs ADD COLUMN last_verified_date TEXT")
    else:
        print("'last_verified_date' column already present. Nothing to do.")

    if not column_exists(cursor, "cv_versions", "file_content"):
        print("Adding 'file_content' column so CVs are stored in the database, not local disk...")
        cursor.execute("ALTER TABLE cv_versions ADD COLUMN file_content BLOB")
    else:
        print("'file_content' column already present. Nothing to do.")

    conn.commit()
    conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
