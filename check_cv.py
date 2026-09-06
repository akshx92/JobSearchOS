import sqlite3

conn = sqlite3.connect("job_search_os.db")
cursor = conn.cursor()

# Check the columns inside cv_versions
cursor.execute("PRAGMA table_info(cv_versions);")
columns = cursor.fetchall()

print("--- Columns in cv_versions table ---")
for col in columns:
    print(col)

conn.close()