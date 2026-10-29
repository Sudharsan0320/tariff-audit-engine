import json
import sqlite3
from collections import Counter

import pytest

from src.load_rules import INVENTORY_PATH, load_rules, validate_inventory


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
        CREATE TABLE tariff_rules (
            id INTEGER PRIMARY KEY,
            tariff_version_id INTEGER NOT NULL,
            rule_type TEXT NOT NULL,
            rule_text TEXT NOT NULL
        );
        """
    )
    return conn


def read_inventory():
    with INVENTORY_PATH.open(encoding="utf-8") as file:
        return json.load(file)


def add_versions(conn, inventory, status="draft"):
    for code in inventory["expected_rule_counts"]:
        conn.execute(
            """
            INSERT INTO tariff_versions(
                state, utility, schedule_code, version, effective_from, status
            ) VALUES ('VA', 'Virginia Electric and Power Company', ?, 12, '2025-07-01', ?)
            """,
            (code, status),
        )
    conn.commit()


def test_load_rules_asserts_counts_and_is_idempotent():
    conn = make_connection()
    inventory = read_inventory()
    add_versions(conn, inventory)

    expected_counts = load_rules(conn, inventory)

    assert sum(expected_counts.values()) == 112
    assert conn.execute("SELECT COUNT(*) FROM tariff_rules").fetchone()[0] == 112
    assert conn.execute(
        """
        SELECT COUNT(*)
        FROM tariff_rules AS r
        JOIN tariff_versions AS v ON v.id = r.tariff_version_id
        WHERE v.schedule_code = 'C'
          AND r.rule_text LIKE '%[garbled — verify]%'
        """
    ).fetchone()[0] == 1

    before = Counter(
        conn.execute(
            """
            SELECT v.schedule_code, r.rule_type, r.rule_text
            FROM tariff_rules AS r
            JOIN tariff_versions AS v ON v.id = r.tariff_version_id
            """
        ).fetchall()
    )
    load_rules(conn, inventory)
    after = Counter(
        conn.execute(
            """
            SELECT v.schedule_code, r.rule_type, r.rule_text
            FROM tariff_rules AS r
            JOIN tariff_versions AS v ON v.id = r.tariff_version_id
            """
        ).fetchall()
    )

    assert after == before


def test_load_rules_rejects_inventory_count_mismatch_before_writing():
    conn = make_connection()
    inventory = read_inventory()
    add_versions(conn, inventory)
    inventory["inventory_rule_counts"]["100"] += 1

    with pytest.raises(ValueError, match="inventory has"):
        validate_inventory(inventory)

    assert conn.execute("SELECT COUNT(*) FROM tariff_rules").fetchone()[0] == 0


def test_load_rules_rejects_missing_version_without_partial_writes():
    conn = make_connection()
    inventory = read_inventory()
    add_versions(conn, inventory)
    conn.execute("DELETE FROM tariff_versions WHERE schedule_code = 'RIDER A'")
    conn.commit()

    with pytest.raises(ValueError, match="Expected exactly the 40"):
        load_rules(conn, inventory)

    assert conn.execute("SELECT COUNT(*) FROM tariff_rules").fetchone()[0] == 0
