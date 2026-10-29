import sqlite3

from src.ingest_usage import persist_account_profile
from src.parse_account_profile import parse_profile_text
from src.validate_usage import validate_account_profile
from test_parse_account_profile import PROFILE_TEXT


def make_connection():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tariff_versions (
            id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            utility TEXT NOT NULL,
            schedule_code TEXT NOT NULL,
            version INTEGER NOT NULL,
            effective_from DATE NOT NULL,
            effective_to DATE,
            status TEXT NOT NULL
        );
        CREATE TABLE usage_accounts (
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
        """
    )
    return conn


def sample_profile(**overrides):
    profile = parse_profile_text(PROFILE_TEXT, "008980201225")
    profile["source_path"] = "data/raw/bills/example-eap.pdf"
    profile.update(overrides)
    return profile


def add_active_schedule(conn, schedule_code="154"):
    conn.execute(
        """
        INSERT INTO tariff_versions(
            state, utility, schedule_code, version, effective_from, status
        ) VALUES ('VA', 'Virginia Electric and Power Company', ?, 12, '2025-07-01', 'active')
        """,
        (schedule_code,),
    )


def test_missing_active_tariff_is_a_validation_failure():
    profile = sample_profile()
    conn = make_connection()

    failures = validate_account_profile(profile, conn)

    assert "No active tariff version for schedule_code 154." in failures


def test_stores_valid_profile_and_is_idempotent():
    conn = make_connection()
    add_active_schedule(conn)
    profile = sample_profile()

    first_status, first_reason = persist_account_profile(conn, profile)
    second_status, second_reason = persist_account_profile(conn, profile)
    count = conn.execute("SELECT COUNT(*) FROM usage_accounts").fetchone()[0]
    row = conn.execute(
        "SELECT status, schedule_code, current_rate_raw FROM usage_accounts"
    ).fetchone()

    assert first_status == second_status == "stored"
    assert first_reason is None and second_reason is None
    assert count == 1
    assert row == ("stored", "154", "VE-154")


def test_quarantines_when_schedule_is_unknown():
    conn = make_connection()
    profile = sample_profile()

    status, reason = persist_account_profile(conn, profile)
    row = conn.execute(
        "SELECT status, quarantine_reason FROM usage_accounts"
    ).fetchone()

    assert status == "quarantine"
    assert "No active tariff version" in reason
    assert row[0] == "quarantine"


def test_reingest_promotes_quarantine_to_stored_after_tariff_exists():
    conn = make_connection()
    profile = sample_profile()
    persist_account_profile(conn, profile)
    add_active_schedule(conn)

    status, reason = persist_account_profile(conn, profile)
    row = conn.execute(
        "SELECT status, quarantine_reason FROM usage_accounts"
    ).fetchone()

    assert status == "stored"
    assert reason is None
    assert row == ("stored", None)
