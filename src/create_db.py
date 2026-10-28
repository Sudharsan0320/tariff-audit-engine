import sqlite3  # import = bring in a toolbox; sqlite3 = Python's built-in toolbox for talking to SQLite databases

conn = sqlite3.connect("data/tariffs.db")  # connect() opens the database file; conn = the connection object (your live link to the file). If the file doesn't exist, it gets created.
cur = conn.cursor()  # cursor() makes a cursor; cur = your messenger that carries SQL commands to the database and brings results back

# --- tariff_versions: one row per tariff version (a dated set of rates) ---
# The SQL below builds it. Column by column:
#   id INTEGER PRIMARY KEY = whole-number column; the unique id for each row, auto-assigned
#   state/utility/schedule_code/version = TEXT or INTEGER columns; NOT NULL = must always have a value
#   effective_from DATE NOT NULL = the date this version starts applying
#   effective_to DATE (no NOT NULL) = the date it stops; allowed to stay empty (NULL) for the current version
#   status TEXT NOT NULL = 'draft' or 'active', the pipeline stage
# CREATE TABLE makes the table; IF NOT EXISTS = skip if it's already there, so re-running is safe
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
""")  # """ closes the SQL string; ) closes the execute( call

# --- tariff_components: one row per individual rate, linked to its version ---
# Column by column:
#   id INTEGER PRIMARY KEY = auto-assigned unique row id
#   tariff_version_id INTEGER NOT NULL = links each component to its version row
#   component_type TEXT NOT NULL = what kind of charge, e.g. 'supply_cents_per_kwh'
#   billing_category TEXT = which schedule it applies to, e.g. 'Schedule 5'
#   rate REAL NOT NULL = REAL means decimal number; the actual rate, e.g. 0.5729
#   tier_min_kwh / tier_max_kwh = where a tier starts/ends (empty when the rate isn't tiered)
#   season TEXT = e.g. 'summer' (empty when the rate isn't seasonal)
#   FOREIGN KEY ... REFERENCES tariff_versions(id) = rule: every tariff_version_id must match a real id over there; no orphan rows
cur.execute("""
CREATE TABLE IF NOT EXISTS tariff_components (
    id INTEGER PRIMARY KEY,
    tariff_version_id INTEGER NOT NULL,
    component_type TEXT NOT NULL,
    billing_category TEXT,
    rate REAL,
    formula TEXT,
    unit TEXT,
    tier_min REAL,
    tier_max REAL,
    tier_unit TEXT,
    season TEXT,
    variant TEXT,
    FOREIGN KEY (tariff_version_id) REFERENCES tariff_versions(id)
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS tariff_rules (
    id INTEGER PRIMARY KEY,
    tariff_version_id INTEGER NOT NULL,
    rule_type TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    FOREIGN KEY (tariff_version_id) REFERENCES tariff_versions(id)
);
""")

conn.commit()  # commit() SAVES all changes into the file; without it, everything above is thrown away when you close
conn.close()  # close() ends the connection, freeing it up
print("Database created: data/tariffs.db")  # print() shows text on screen; confirms the script finished
