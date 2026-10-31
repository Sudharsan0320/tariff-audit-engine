import argparse
from collections import Counter
import json
from pathlib import Path

try:
    from src import db
except ImportError:
    import db


ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = db.default_target(ROOT / "data" / "tariffs.db")
INVENTORY_PATH = ROOT / "docs" / "tariff-rules-inventory.json"
EFFECTIVE_FROM = "2025-07-01"
VERSION = 12


def validate_inventory(inventory):
    """Check inventory shape and both source and final per-version counts."""
    inventory_counts = inventory.get("inventory_rule_counts")
    supplement_counts = inventory.get("supplement_rule_counts")
    expected_counts = inventory.get("expected_rule_counts")
    rules = inventory.get("rules")
    if not all(
        isinstance(value, dict)
        for value in (inventory_counts, supplement_counts, expected_counts)
    ) or not isinstance(rules, list):
        raise ValueError("Inventory must contain rule-count maps and a rules list.")

    codes = set(expected_counts)
    if (
        len(codes) != 40
        or set(inventory_counts) != codes
        or set(supplement_counts) != codes
    ):
        raise ValueError("Inventory must define exactly 40 Amendment 12 versions.")
    if any(
        not isinstance(count, int) or count < 0
        for counts in (inventory_counts, supplement_counts, expected_counts)
        for count in counts.values()
    ):
        raise ValueError("Inventory rule counts must be non-negative integers.")

    actual_inventory = Counter()
    actual_supplements = Counter()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule {index} must be an object.")
        code = rule.get("schedule_code")
        kind = rule.get("rule_type")
        text = rule.get("rule_text")
        origin = rule.get("origin")
        if code not in codes:
            raise ValueError(f"Rule {index} has an unexpected version code: {code!r}.")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(f"Rule {index} has an empty rule_type.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Rule {index} has an empty rule_text.")
        if origin == "inventory":
            actual_inventory[code] += 1
        elif origin == "plan_supplement":
            actual_supplements[code] += 1
        else:
            raise ValueError(f"Rule {index} has an unknown origin: {origin!r}.")

    for code in codes:
        if actual_inventory[code] != inventory_counts[code]:
            raise ValueError(
                f"{code}: inventory has {actual_inventory[code]} source rules; "
                f"expected {inventory_counts[code]}."
            )
        if actual_supplements[code] != supplement_counts[code]:
            raise ValueError(
                f"{code}: inventory has {actual_supplements[code]} supplements; "
                f"expected {supplement_counts[code]}."
            )
        if inventory_counts[code] + supplement_counts[code] != expected_counts[code]:
            raise ValueError(f"{code}: final rule count does not match its sources.")

    if sum(inventory_counts.values()) != 101:
        raise ValueError("The supplied RULES inventory must contain 101 bullets.")
    return expected_counts


def load_rules(conn, inventory):
    """Replace draft rules from the reviewed inventory, transactionally and idempotently."""
    expected_counts = validate_inventory(inventory)
    conn.execute("BEGIN IMMEDIATE")
    try:
        versions = conn.execute(
            """
            SELECT id, schedule_code, status
            FROM tariff_versions
            WHERE version = ? AND effective_from = ?
            ORDER BY schedule_code
            """,
            (VERSION, EFFECTIVE_FROM),
        ).fetchall()
        codes = [row[1] for row in versions]
        if len(versions) != 40 or set(codes) != set(expected_counts):
            raise ValueError(
                "Expected exactly the 40 inventory versions at version 12, "
                f"effective {EFFECTIVE_FROM}; found {len(versions)}."
            )
        if len(set(codes)) != len(codes):
            raise ValueError("Found duplicate version-12 schedule codes.")

        version_ids = {code: version_id for version_id, code, _ in versions}
        target_ids = tuple(version_ids.values())
        placeholders = ",".join("?" for _ in target_ids)
        existing = conn.execute(
            f"""
            SELECT v.schedule_code, r.rule_type, r.rule_text
            FROM tariff_rules AS r
            JOIN tariff_versions AS v ON v.id = r.tariff_version_id
            WHERE r.tariff_version_id IN ({placeholders})
            """,
            target_ids,
        ).fetchall()
        expected_rows = [
            (rule["schedule_code"], rule["rule_type"], rule["rule_text"])
            for rule in inventory["rules"]
        ]
        if Counter(existing) == Counter(expected_rows):
            conn.commit()
            return expected_counts

        if any(status != "draft" for _, _, status in versions):
            raise ValueError(
                "Cannot replace rules after any version-12 version is active; "
                "existing rules do not match the reviewed inventory."
            )

        conn.execute(
            f"DELETE FROM tariff_rules WHERE tariff_version_id IN ({placeholders})",
            target_ids,
        )
        conn.executemany(
            """
            INSERT INTO tariff_rules(tariff_version_id, rule_type, rule_text)
            VALUES (?, ?, ?)
            """,
            [
                (
                    version_ids[rule["schedule_code"]],
                    rule["rule_type"],
                    rule["rule_text"],
                )
                for rule in inventory["rules"]
            ],
        )

        actual_counts = dict.fromkeys(expected_counts, 0)
        for code, count in conn.execute(
            f"""
            SELECT v.schedule_code, COUNT(r.id)
            FROM tariff_versions AS v
            LEFT JOIN tariff_rules AS r ON r.tariff_version_id = v.id
            WHERE v.id IN ({placeholders})
            GROUP BY v.schedule_code
            """,
            target_ids,
        ):
            actual_counts[code] = count
        if actual_counts != expected_counts:
            raise ValueError(
                f"Loaded rule counts do not match inventory: {actual_counts}"
            )
        conn.commit()
        return actual_counts
    except Exception:
        conn.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Load reviewed Amendment 12 rules into tariff_rules."
    )
    parser.add_argument(
        "--database",
        type=lambda value: value if db.is_postgres(value) else Path(value),
        default=DATABASE_PATH,
        help="SQLite database path or postgresql:// URL",
    )
    parser.add_argument(
        "--inventory", type=Path, default=INVENTORY_PATH, help="Reviewed JSON inventory"
    )
    args = parser.parse_args()

    with args.inventory.open(encoding="utf-8") as file:
        inventory = json.load(file)
    with db.connect(args.database) as conn:
        counts = load_rules(conn, inventory)

    print(
        f"PASS: loaded {sum(counts.values())} rules across "
        f"{len(counts)} version-12 versions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
