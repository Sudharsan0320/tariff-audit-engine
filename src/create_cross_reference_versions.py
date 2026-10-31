"""Create draft version rows for schedules the document references but gives no sheet.

The schedules are declared as data in docs/tariff-rules-inventory.json
(`cross_reference_schedules`). Idempotent: a version that exists in any status
is left alone.
"""

import json
import sys
from pathlib import Path

try:
    from src import db
except ImportError:
    import db

ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = db.default_target(ROOT / "data" / "tariffs.db")
INVENTORY_PATH = ROOT / "docs" / "tariff-rules-inventory.json"
STATE = "VA"
UTILITY = "Virginia Electric and Power Company"
VERSION = 12


def create_cross_reference_versions(conn, inventory):
    declared = inventory.get("cross_reference_schedules")
    if not isinstance(declared, dict):
        raise ValueError("Inventory must define cross_reference_schedules.")
    effective_from = inventory["effective_from"]
    created = []
    skipped = []
    with conn:
        for code in declared:
            existing = conn.execute(
                """
                SELECT id, status FROM tariff_versions
                WHERE state = ? AND utility = ? AND schedule_code = ?
                  AND version = ? AND effective_from = ?
                """,
                (STATE, UTILITY, code, VERSION, effective_from),
            ).fetchone()
            if existing is not None:
                skipped.append((code, existing[0], existing[1]))
                continue
            cursor = conn.execute(
                """
                INSERT INTO tariff_versions(
                    state, utility, schedule_code, version, effective_from, status
                ) VALUES (?, ?, ?, ?, ?, 'draft')
                """,
                (STATE, UTILITY, code, VERSION, effective_from),
            )
            created.append((code, cursor.lastrowid))
    return created, skipped


def main():
    inventory = json.loads(INVENTORY_PATH.read_text())
    conn = db.connect(DATABASE_PATH)
    try:
        created, skipped = create_cross_reference_versions(conn, inventory)
    finally:
        conn.close()
    for code, version_id in created:
        print(f"created draft cross-reference version {code} (id {version_id})")
    for code, version_id, status in skipped:
        print(f"already {status}, skipped: {code} (id {version_id})")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
