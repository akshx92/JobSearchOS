"""
Shared database connection logic for both app.py and search_engine.py.
Both files MUST use this instead of opening their own connections, or they
end up talking to two different databases — which is exactly what caused
the scan failures after the Supabase migration.
"""

import os
import sqlite3

try:
    import psycopg2
except ImportError:
    psycopg2 = None

DB_FILE = "job_search_os.db"


def get_db_url():
    """Reads DATABASE_URL from the environment first — this works reliably
    from background threads, unlike st.secrets, which can silently fail to
    resolve outside Streamlit's main script thread. app.py is responsible for
    copying st.secrets into os.environ once at startup (see app.py's top)."""
    env_val = os.getenv("DATABASE_URL")
    if env_val:
        print("[db_utils] DATABASE_URL found in os.environ.")
        return env_val
    try:
        import streamlit as st
        if hasattr(st, "secrets") and "DATABASE_URL" in st.secrets:
            print("[db_utils] DATABASE_URL found in st.secrets (env bridge did not run/apply).")
            return st.secrets["DATABASE_URL"]
    except Exception as e:
        print(f"[db_utils] st.secrets access raised: {e}")
    print("[db_utils] DATABASE_URL NOT FOUND anywhere - will fall back to local SQLite.")
    return None


def is_postgres():
    return bool(get_db_url()) and psycopg2 is not None


def get_connection():
    db_url = get_db_url()
    if db_url and psycopg2:
        print("[db_utils] Connecting to Postgres.")
        return psycopg2.connect(db_url)
    print("[db_utils] WARNING: Falling back to local SQLite file. "
          "If you expected Postgres, DATABASE_URL was not resolved — see log lines above.")
    return sqlite3.connect(DB_FILE, timeout=15)


def q(sql):
    """Converts SQLite-style '?' placeholders to Postgres-style '%s' when
    running against Postgres. Write every query using '?' and pass it
    through this function — works on both backends without duplicating
    every query string."""
    return sql.replace("?", "%s") if is_postgres() else sql


def insert_and_get_id(cursor, sql, params):
    """Runs an INSERT and returns the new row's id, whichever backend is
    active. psycopg2 has no cursor.lastrowid, so Postgres needs RETURNING id
    explicitly; SQLite doesn't support RETURNING in older versions, so it
    uses lastrowid instead. `sql` should NOT include a trailing semicolon."""
    if is_postgres():
        cursor.execute(q(sql) + " RETURNING id", params)
        return cursor.fetchone()[0]
    else:
        cursor.execute(q(sql), params)
        return cursor.lastrowid


def insert_ignore_duplicate(cursor, table, columns, values, conflict_column):
    """Runs an insert that silently skips a row that would violate a UNIQUE
    constraint (e.g. duplicate job_url), returning True if a row was
    actually inserted, False if it was skipped as a duplicate. Abstracts the
    'INSERT OR IGNORE' (SQLite) vs 'ON CONFLICT DO NOTHING' (Postgres)
    syntax difference."""
    col_list = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    if is_postgres():
        sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT ({conflict_column}) DO NOTHING"
    else:
        sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
    cursor.execute(q(sql), values)
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# SCAN LOCK — prevents two scans running at once (e.g. a scheduled GitHub
# Actions run overlapping with a manual "Recheck Now" in the app), which
# could otherwise cause duplicate AI evaluations or wasted API calls.
# Stored as a plain row in the settings table — no new table needed.
# ---------------------------------------------------------------------------
import datetime as _datetime

SCAN_LOCK_KEY = "scan_lock"
SCAN_LOCK_STALE_MINUTES = 20  # if a lock is older than this, assume the process crashed and allow a new scan


def try_acquire_scan_lock(owner_label="unknown"):
    """Returns True if the lock was acquired, False if another scan is
    already running (and its lock isn't stale)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(q("SELECT value FROM settings WHERE key = ?"), (SCAN_LOCK_KEY,))
    row = cursor.fetchone()

    if row and row[0]:
        try:
            _, ts_str = row[0].split("|", 1)
            held_since = _datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            age_minutes = (_datetime.datetime.now() - held_since).total_seconds() / 60
            if age_minutes < SCAN_LOCK_STALE_MINUTES:
                conn.close()
                return False  # genuinely locked, not stale
        except Exception:
            pass  # malformed lock value — treat as stale and overwrite below

    now_str = _datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lock_value = f"{owner_label}|{now_str}"
    cursor.execute(
        q("INSERT INTO settings (key, value) VALUES (?, ?) "
          "ON CONFLICT(key) DO UPDATE SET value = excluded.value"),
        (SCAN_LOCK_KEY, lock_value),
    )
    conn.commit()
    conn.close()
    return True


def release_scan_lock():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(q("DELETE FROM settings WHERE key = ?"), (SCAN_LOCK_KEY,))
    conn.commit()
    conn.close()


def get_scan_lock_status():
    """Returns (is_locked, owner_label, held_since_str) for display purposes."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(q("SELECT value FROM settings WHERE key = ?"), (SCAN_LOCK_KEY,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        return False, None, None
    try:
        owner, ts_str = row[0].split("|", 1)
        return True, owner, ts_str
    except Exception:
        return True, "unknown", None
