import sqlite3

import pytest

from src.activate_version import activate_version
from src.validate_draft import validate_drafts


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
        CREATE TABLE tariff_components (
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
            billing_class TEXT
        );
        CREATE TABLE tariff_rules (
            id INTEGER PRIMARY KEY,
            tariff_version_id INTEGER NOT NULL,
            rule_type TEXT NOT NULL,
            rule_text TEXT NOT NULL
        );
        """
    )
    return conn


def add_version(conn, version, effective_from, status):
    cursor = conn.execute(
        """
        INSERT INTO tariff_versions(
            state, utility, schedule_code, version, effective_from, status
        ) VALUES ('VA', 'Utility', 'R-1', ?, ?, ?)
        """,
        (version, effective_from, status),
    )
    return cursor.lastrowid


def add_component(
    conn,
    version_id,
    rate=0.5,
    component_type="energy",
    formula=None,
    unit="usd_per_kwh",
    tier_min=None,
    tier_max=None,
    tier_unit=None,
    variant=None,
):
    conn.execute(
        """
        INSERT INTO tariff_components(
            tariff_version_id, component_type, billing_category, rate, formula,
            unit, tier_min, tier_max, tier_unit, variant
        ) VALUES (?, ?, 'residential', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            component_type,
            rate,
            formula,
            unit,
            tier_min,
            tier_max,
            tier_unit,
            variant,
        ),
    )


def add_rule(conn, version_id, rule_type="eligibility", rule_text="Must qualify."):
    conn.execute(
        """
        INSERT INTO tariff_rules(tariff_version_id, rule_type, rule_text)
        VALUES (?, ?, ?)
        """,
        (version_id, rule_type, rule_text),
    )


def test_validate_drafts_passes_for_valid_draft():
    conn = make_connection()
    version_id = add_version(conn, 1, "2025-01-01", "draft")
    add_component(conn, version_id)

    assert validate_drafts(conn) == []


def test_validate_drafts_reports_invalid_rates_duplicates_and_orphans():
    conn = make_connection()
    version_id = add_version(conn, 1, "2025-01-01", "draft")
    conn.execute("UPDATE tariff_versions SET state = '' WHERE id = ?", (version_id,))
    add_component(conn, version_id, rate=-0.5)
    add_component(conn, version_id, rate=-0.5)
    add_component(conn, 999, rate=2)
    add_rule(conn, version_id, "", "   ")
    add_rule(conn, version_id)
    add_rule(conn, version_id)
    add_rule(conn, 999)

    failures = validate_drafts(conn)

    assert any("non-positive rate" in failure for failure in failures)
    assert any("are duplicates" in failure for failure in failures)
    assert any("references missing version 999" in failure for failure in failures)
    assert any("empty state" in failure for failure in failures)
    assert any("Rule 1 has an empty rule_type" in failure for failure in failures)
    assert any("Rule 1 has an empty rule_text" in failure for failure in failures)
    assert any("are duplicates for version" in failure for failure in failures)
    assert any("Rule 4 references missing version 999" in failure for failure in failures)


def test_validate_drafts_requires_exactly_one_rate_or_formula():
    conn = make_connection()
    version_id = add_version(conn, 1, "2025-01-01", "draft")
    add_component(conn, version_id, rate=0.5, formula="x * 2")
    add_component(conn, version_id, rate=None, formula=None)
    add_component(conn, version_id, rate=None, formula="x * 2")

    failures = validate_drafts(conn)

    assert sum("exactly one of rate or formula" in failure for failure in failures) == 2


def test_validate_drafts_allows_zero_rate_for_ordinary_component():
    conn = make_connection()
    version_id = add_version(conn, 1, "2025-01-01", "draft")
    add_component(conn, version_id, rate=0)

    assert validate_drafts(conn) == []


def test_validate_drafts_allows_non_positive_non_ordinary_rates():
    conn = make_connection()
    version_id = add_version(conn, 1, "2025-01-01", "draft")
    add_component(conn, version_id, rate=0, component_type="es_adjustment")
    add_component(conn, version_id, rate=-1, component_type="bill_credit")
    add_component(conn, version_id, rate=0, component_type="payment_reversal")

    assert validate_drafts(conn) == []


def test_validate_drafts_checks_units_and_tier_units():
    conn = make_connection()
    version_id = add_version(conn, 1, "2025-01-01", "draft")
    add_component(conn, version_id, unit="dollars_per_kwh")
    add_component(conn, version_id, tier_min=0)

    failures = validate_drafts(conn)

    assert any("invalid unit" in failure for failure in failures)
    assert any("has tier_min but no tier_unit" in failure for failure in failures)


def test_validate_drafts_duplicate_key_distinguishes_variants():
    conn = make_connection()
    version_id = add_version(conn, 1, "2025-01-01", "draft")
    add_component(conn, version_id, variant="primary")
    add_component(conn, version_id, variant="secondary")

    assert validate_drafts(conn) == []


def test_activation_closes_previous_version_and_activates_draft():
    conn = make_connection()
    old_id = add_version(conn, 1, "2025-01-01", "active")
    new_id = add_version(conn, 2, "2025-04-01", "draft")
    add_component(conn, new_id)
    conn.commit()

    activate_version(conn, new_id)

    assert conn.execute(
        "SELECT effective_to, status FROM tariff_versions WHERE id = ?", (old_id,)
    ).fetchone() == ("2025-04-01", "active")
    assert conn.execute(
        "SELECT status FROM tariff_versions WHERE id = ?", (new_id,)
    ).fetchone() == ("active",)


def test_failed_activation_rolls_back_without_closing_active_version():
    conn = make_connection()
    old_id = add_version(conn, 1, "2025-01-01", "active")
    new_id = add_version(conn, 2, "2025-04-01", "draft")
    add_component(conn, new_id, rate=-0.5)
    conn.commit()

    with pytest.raises(ValueError, match="Draft validation failed"):
        activate_version(conn, new_id)

    assert conn.execute(
        "SELECT effective_to, status FROM tariff_versions WHERE id = ?", (old_id,)
    ).fetchone() == (None, "active")
    assert conn.execute(
        "SELECT status FROM tariff_versions WHERE id = ?", (new_id,)
    ).fetchone() == ("draft",)


def test_activation_validates_only_the_target_draft():
    conn = make_connection()
    target_id = add_version(conn, 1, "2025-01-01", "draft")
    invalid_id = add_version(conn, 2, "2025-02-01", "draft")
    add_component(conn, target_id)
    add_component(conn, invalid_id, rate=-1)
    conn.commit()

    activate_version(conn, target_id)

    assert conn.execute(
        "SELECT status FROM tariff_versions WHERE id = ?", (target_id,)
    ).fetchone() == ("active",)
    assert conn.execute(
        "SELECT status FROM tariff_versions WHERE id = ?", (invalid_id,)
    ).fetchone() == ("draft",)
