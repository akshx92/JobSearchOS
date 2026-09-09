-- Job Search OS — Supabase/PostgreSQL schema
-- Safe to run multiple times: uses IF NOT EXISTS everywhere, and ADD COLUMN
-- IF NOT EXISTS for any column that might be missing from an earlier version
-- of this schema. Run this in the Supabase SQL editor to make sure your live
-- database actually has every column the app expects.

CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    original_name TEXT,
    normalized_name TEXT UNIQUE,
    scan_status TEXT DEFAULT 'NOT_SCANNED',
    first_scanned_date TEXT,
    last_scanned_date TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id),
    contact_name TEXT,
    phone TEXT,
    email TEXT,
    mba_college TEXT
);

CREATE TABLE IF NOT EXISTS cv_versions (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL,
    uploaded_date TEXT NOT NULL,
    is_active BOOLEAN DEFAULT FALSE,
    file_content BYTEA
);

CREATE TABLE IF NOT EXISTS search_runs (
    id SERIAL PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT,
    cv_version_id INTEGER REFERENCES cv_versions(id),
    companies_scanned INTEGER DEFAULT 0,
    relevant_jobs_found INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_records (
    id SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id),
    run_id INTEGER REFERENCES search_runs(id),
    scan_date TEXT NOT NULL,
    status TEXT DEFAULT 'IN_PROGRESS',
    cv_version_id INTEGER REFERENCES cv_versions(id),
    error_reason TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id SERIAL PRIMARY KEY,
    job_id TEXT,
    company_id INTEGER REFERENCES companies(id),
    run_id INTEGER REFERENCES search_runs(id),
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
    last_verified_date TEXT
);

CREATE TABLE IF NOT EXISTS saved_jobs (
    id SERIAL PRIMARY KEY,
    job_id INTEGER UNIQUE REFERENCES jobs(id),
    saved_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS error_log (
    id SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id),
    run_id INTEGER REFERENCES search_runs(id),
    timestamp TEXT NOT NULL,
    error_type TEXT,
    error_message TEXT,
    is_resolved BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Column additions for any table that may have been created from an earlier
-- schema version, before these fields existed.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS last_seen_date TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS last_verified_date TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS required_experience_min INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS required_experience_max INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS experience_text TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS posting_date_verified INTEGER DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS link_verified INTEGER DEFAULT 0;
ALTER TABLE cv_versions ADD COLUMN IF NOT EXISTS file_content BYTEA;

-- Back-fill last_seen_date for any existing rows where it's still null, so
-- older jobs don't look artificially "never seen."
UPDATE jobs SET last_seen_date = date_discovered WHERE last_seen_date IS NULL;
