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
        return env_val
    try:
        import streamlit as st
        if hasattr(st, "secrets") and "DATABASE_URL" in st.secrets:
            return st.secrets["DATABASE_URL"]
    except Exception:
        pass
    return None


def is_postgres():
    return bool(get_db_url()) and psycopg2 is not None


def get_connection():
    db_url = get_db_url()
    if db_url and psycopg2:
        return psycopg2.connect(db_url)
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
