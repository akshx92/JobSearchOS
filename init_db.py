import sqlite3
import pandas as pd
import re
import os

EXCEL_FILE = "MBA referrals - Mumbai - List of Members.xlsx"
DB_FILE = "job_search_os.db"


# Extensible list of noise phrases people write into a "Current Company" field
# that aren't actually part of the company name (e.g. "HDFC Still to Join").
# Add more patterns here as you spot them in your referral sheet.
COMPANY_NAME_NOISE_PATTERNS = [
    r"\(.*?\)",              # anything in parentheses, e.g. "(yet to join)"
    r"\bstill to join\b",
    r"\byet to join\b",
    r"\bto join\b",
    r"\bjoining soon\b",
    r"\bjoining shortly\b",
    r"\bjoining next month\b",
    r"\bcurrently at\b",
    r"\bpreviously (at|with)\b",
    r"\bex[\s\-]+",          # "Ex-Google", "Ex Google"
    r"\bformerly (at|with)\b",
]


PLACEHOLDER_COMPANY_VALUES = {"na", "n/a", "none", "self employed", "freelance", "freelancer", "tbd", "-", "unemployed"}


def clean_company_name_text(raw):
    """Strips known noise phrases from a messy referral-sheet company field
    before it's used for normalization or display."""
    text = str(raw)
    for pattern in COMPANY_NAME_NOISE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    # Strip a trailing " - <role>" or " , <role>" fragment some people add,
    # e.g. "HDFC Bank - Assistant Manager" -> "HDFC Bank". Only trims after
    # the first separator, so legitimate multi-word company names are safe.
    text = re.split(r"\s+-\s+|\s*,\s*", text, maxsplit=1)[0]
    return re.sub(r"\s+", " ", text).strip(" -,:;")


def is_placeholder_company(cleaned_name):
    return cleaned_name.strip().lower() in PLACEHOLDER_COMPANY_VALUES


def normalize_company_name(name):
    """Removes spaces, punctuation, and makes lowercase to prevent duplicates.
    Also strips known noise phrases and resolves known aliases (e.g. 'JP
    Morgan' -> 'jpmorganchase') via the shared alias table in
    search_engine.py, since pure formatting cleanup can't tell that two
    differently-worded names are the same company."""
    if pd.isna(name):
        return "unknown"
    name = clean_company_name_text(name).lower()
    cleaned = re.sub(r'[^a-z0-9]', '', name)
    try:
        from search_engine import resolve_company_alias
        cleaned = resolve_company_alias(cleaned)
    except ImportError:
        pass  # alias resolution is a nice-to-have; never block on it
    return cleaned


def setup_database():
    print("1. Connecting to database...")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    print("2. Creating tables for Companies and Contacts...")
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        original_name TEXT,
        normalized_name TEXT UNIQUE,
        scan_status TEXT DEFAULT 'NOT_SCANNED',
        first_scanned_date TEXT,
        last_scanned_date TEXT
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER,
        contact_name TEXT,
        phone TEXT,
        email TEXT,
        mba_college TEXT,
        FOREIGN KEY (company_id) REFERENCES companies (id)
    )
    ''')
    conn.commit()

    print(f"3. Reading Excel file: {EXCEL_FILE}...")
    df = pd.read_excel(EXCEL_FILE)

    col_company = 'Current Company'
    col_name = 'Your full name'
    col_phone = 'Whatsapp no.'
    col_college = 'Your MBA college'

    print("4. Importing data into the database...")
    for index, row in df.iterrows():
        company_original = clean_company_name_text(str(row.get(col_company, 'Unknown')))
        if company_original == 'Unknown' or company_original.strip() == 'nan' or not company_original or is_placeholder_company(company_original):
            continue

        contact_name = str(row.get(col_name, ''))
        phone = str(row.get(col_phone, ''))
        college = str(row.get(col_college, ''))
        email = ""

        company_normalized = normalize_company_name(company_original)

        cursor.execute('''
            INSERT OR IGNORE INTO companies (original_name, normalized_name)
            VALUES (?, ?)
        ''', (company_original, company_normalized))

        cursor.execute('SELECT id FROM companies WHERE normalized_name = ?', (company_normalized,))
        company_id = cursor.fetchone()[0]

        cursor.execute('''
            INSERT INTO contacts (company_id, contact_name, phone, email, mba_college)
            VALUES (?, ?, ?, ?, ?)
        ''', (company_id, contact_name, phone, email, college))

    conn.commit()
    conn.close()
    print("5. Success! Database created and contacts imported.")


if __name__ == "__main__":
    setup_database()
