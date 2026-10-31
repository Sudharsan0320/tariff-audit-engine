import sqlite3  # import = bring in a toolbox; sqlite3 = Python's built-in toolbox for talking to SQLite databases

import os
try:
    from src import db
except ImportError:
    import db
DB_TARGET = db.default_target("data/tariffs.db")  # TARIFF_DB_URL / TARIFF_DB_PATH override the location (pipeline use)
conn = db.connect(DB_TARGET)  # connect() opens the database; conn = the connection object (your live link). SQLite files are created if missing.
cur = conn.cursor()  # cursor() makes a cursor; cur = your messenger that carries SQL commands to the database and brings results back

# --- tariff_versions: one row per tariff version (a dated set of rates) ---
# The SQL below builds it. Column by column:
#   id INTEGER PRIMARY KEY = whole-number column; the unique id for each row, auto-assigned
#   state/utility/schedule_code/version = TEXT or INTEGER columns; NOT NULL = must always have a value
#   effective_from DATE NOT NULL = the date this version starts applying
#   effective_to DATE (no NOT NULL) = the date it stops; allowed to stay empty (NULL) for the current version
#   status TEXT NOT NULL = 'draft', 'active' (current) or 'superseded' (replaced by a later
#   activation; still resolved by its effective range), the pipeline stage
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
    billing_class TEXT,
    source_page INTEGER,
    source_cell TEXT,
    FOREIGN KEY (tariff_version_id) REFERENCES tariff_versions(id)
);
""")

# Databases built before B1 lack the evidence columns; add them in place.
component_columns = {
    row[1] for row in cur.execute("PRAGMA table_info(tariff_components)").fetchall()
}
for column, column_type in (("source_page", "INTEGER"), ("source_cell", "TEXT")):
    if column not in component_columns:
        cur.execute(f"ALTER TABLE tariff_components ADD COLUMN {column} {column_type}")

cur.execute("""
CREATE TABLE IF NOT EXISTS tariff_rules (
    id INTEGER PRIMARY KEY,
    tariff_version_id INTEGER NOT NULL,
    rule_type TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    FOREIGN KEY (tariff_version_id) REFERENCES tariff_versions(id)
);
""")

# --- usage_accounts: one row per extracted Dominion EAP account profile ---
# status is 'stored' after validation or 'quarantine' when checks fail.
# source_path + account_number is the idempotency key: re-running the same PDF
# updates that row instead of inserting a duplicate.
cur.execute("""
CREATE TABLE IF NOT EXISTS usage_accounts (
    id INTEGER PRIMARY KEY,
    source_path TEXT NOT NULL,
    account_number TEXT NOT NULL,
    account_name TEXT,
    customer_class TEXT,
    mailing_address TEXT,
    service_address TEXT,
    current_rate_raw TEXT,
    schedule_code TEXT,
    turn_on_date DATE,
    meter_numbers TEXT,
    voltage TEXT,
    delivery_phase TEXT,
    minimum_demand TEXT,
    facility_charge TEXT,
    billing_status TEXT,
    district_office TEXT,
    status TEXT NOT NULL,
    quarantine_reason TEXT,
    UNIQUE(source_path, account_number)
);
""")

# --- accounts: one row per account in a validated usage history ---
cur.execute("""
CREATE TABLE IF NOT EXISTS accounts (
    account_number TEXT PRIMARY KEY,
    customer_name TEXT,
    service_address TEXT,
    meter_number TEXT,
    customer_class TEXT,
    voltage TEXT,
    delivery_phase TEXT,
    turn_on_date DATE,
    current_rate_raw TEXT
);
""")

# --- bills: one row per account and billing period ---
cur.execute("""
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY,
    account_number TEXT NOT NULL,
    bill_from DATE NOT NULL,
    bill_to DATE NOT NULL,
    billing_days INTEGER,
    billed_rate_raw TEXT,
    schedule_code TEXT,
    total_consumption_kwh REAL,
    demand_kw REAL,
    total_billed NUMERIC,
    source_file TEXT NOT NULL,
    history_year_status TEXT NOT NULL DEFAULT 'complete',
    FOREIGN KEY (account_number) REFERENCES accounts(account_number),
    UNIQUE (account_number, bill_from, bill_to)
);
""")

bill_columns = {
    row[1] for row in cur.execute("PRAGMA table_info(bills)").fetchall()
}
if "history_year_status" not in bill_columns:
    cur.execute(
        "ALTER TABLE bills ADD COLUMN history_year_status "
        "TEXT NOT NULL DEFAULT 'complete'"
    )

# --- bill_charges: one row per printed charge line, retaining its raw label ---
cur.execute("""
CREATE TABLE IF NOT EXISTS bill_charges (
    id INTEGER PRIMARY KEY,
    bill_id INTEGER NOT NULL,
    line_label TEXT NOT NULL,
    amount NUMERIC NOT NULL,
    FOREIGN KEY (bill_id) REFERENCES bills(id)
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS bill_determinants (
    id INTEGER PRIMARY KEY,
    bill_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    determinant_type TEXT NOT NULL,
    value NUMERIC NOT NULL,
    FOREIGN KEY (bill_id) REFERENCES bills(id),
    UNIQUE (bill_id, label)
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS audit_runs (
    id INTEGER PRIMARY KEY,
    bill_id INTEGER NOT NULL,
    mapping_version TEXT NOT NULL,
    tariff_snapshot_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (bill_id) REFERENCES bills(id),
    UNIQUE (bill_id, input_hash)
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS audit_lines (
    id INTEGER PRIMARY KEY,
    audit_run_id INTEGER NOT NULL,
    bill_charge_id INTEGER,
    line_label TEXT NOT NULL,
    disposition TEXT NOT NULL,
    verdict TEXT NOT NULL,
    tariff_component_id INTEGER,
    billed_amount TEXT,
    expected_amount TEXT,
    delta TEXT,
    detail TEXT,
    FOREIGN KEY (audit_run_id) REFERENCES audit_runs(id),
    FOREIGN KEY (bill_charge_id) REFERENCES bill_charges(id),
    FOREIGN KEY (tariff_component_id) REFERENCES tariff_components(id)
);
""")

# --- schedule_review_decisions: human review of repriced candidate schedules ---
# Additive: CREATE TABLE IF NOT EXISTS upgrades existing databases in place. Only the
# human decision is stored; candidate totals are always recomputed on read.
cur.execute("""
CREATE TABLE IF NOT EXISTS schedule_review_decisions (
    id INTEGER PRIMARY KEY,
    bill_id INTEGER NOT NULL,
    schedule_code TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('works', 'doesnt_work')),
    reviewer TEXT NOT NULL,
    note TEXT,
    decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
    FOREIGN KEY (bill_id) REFERENCES bills(id),
    UNIQUE (bill_id, schedule_code)
);
""")

# --- parse_review_items: parse results a human must look at (B9) ---
# Additive: CREATE TABLE IF NOT EXISTS upgrades existing databases in place. A resolution
# records that a human looked; it never edits the component or rule it points at.
cur.execute("""
CREATE TABLE IF NOT EXISTS parse_review_items (
    id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    schedule_code TEXT,
    field TEXT NOT NULL,
    stored_value TEXT,
    signal TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    reviewer TEXT,
    resolution_note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    UNIQUE (source_kind, source_ref, field, signal)
);
""")

# --- sites: the portfolio registry, one row per tracked account (B14) ---
# Additive: CREATE TABLE IF NOT EXISTS upgrades existing databases in place.
cur.execute("""
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    account_number TEXT NOT NULL UNIQUE,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_number) REFERENCES accounts(account_number)
);
""")

conn.commit()  # commit() SAVES all changes into the file; without it, everything above is thrown away when you close
conn.close()  # close() ends the connection, freeing it up
print(f"Database created: {db.describe(DB_TARGET)}")  # print() shows text on screen; confirms the script finished
