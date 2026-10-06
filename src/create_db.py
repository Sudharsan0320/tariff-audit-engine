import sqlite3

conn = sqlite3.connect("data/tariffs.db") # this opens a connection to our database file.
cur = conn.cursor() # this is like a cursor, we use this to point and do the work.

cur.execute("""
CREATE TABLE IF NOT EXISTS tariff_versions (
    id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    utility TEXT NOT NULL,
    schedule_code TEXT NOT NULL,
    version INTEGER NOT NULL,
    effective_from DATE NOT NULL,
    effective_to DATE,
    status TEXT NOT NULL
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS tariff_components (
    id INTEGER PRIMARY KEY,
    tariff_version_id INTEGER NOT NULL,
    component_type TEXT NOT NULL,
    billing_category TEXT,
    rate REAL NOT NULL,
    tier_min_kwh REAL,
    tier_max_kwh REAL,
    season TEXT,
    FOREIGN KEY (tariff_version_id) REFERENCES tariff_versions(id)
);
""")

conn.commit()
conn.close()
print("Database created: data/tariffs.db")
