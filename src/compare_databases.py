"""Content comparison of two pipeline databases (Phase 7d acceptance).

Equality means equal content, not equal bookkeeping. The documented exclusions:
  1. timestamp columns (names ending in `_at`);
  2. surrogate ids of tariff_components and audit_runs/audit_lines (a rebuild
     legitimately numbers rows differently). References to a component are
     compared by the component's content instead of its id;
  3. audit_runs/audit_lines of superseded mapping versions, and the legacy
     usage_accounts table (history a pipeline cannot regenerate);
  4. hashes that embed surrogate component ids (tariff_snapshot_hash,
     input_hash, result_hash and the matching keys inside report_json).
     This id dependence is a known Phase 8 item.
Every other table is compared on all non-timestamp columns, ordered by key.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_TABLES = {"usage_accounts"}
ID_DEPENDENT_KEYS = {"tariff_component_id", "tariff_snapshot_hash", "component_id"}
COMPONENT_CONTENT = (
    "tariff_version_id, component_type, billing_category, rate, formula, unit, "
    "tier_min, tier_max, tier_unit, season, variant, billing_class"
)
PLAIN_ORDER_SKIP = {"audit_runs", "audit_lines", "tariff_components"}


def _connect(path):
    return sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)


def _digest(rows):
    digests = [
        hashlib.sha256(json.dumps(r, default=str, separators=(",", ":")).encode()).hexdigest()
        for r in rows
    ]
    return {"rows": len(digests), "hash": hashlib.sha256("".join(digests).encode()).hexdigest()}


def _scrub(value):
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in sorted(value.items()) if k not in ID_DEPENDENT_KEYS}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _plain_table(conn, table):
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    columns = [r[1] for r in info if not r[1].endswith("_at")]
    keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]] or columns
    select = ", ".join(f'"{c}"' for c in columns)
    order = ", ".join(f'"{c}"' for c in keys)
    return list(conn.execute(f'SELECT {select} FROM "{table}" ORDER BY {order}'))


def canonical_content(path, current_mapping):
    conn = _connect(path)
    try:
        tables = [
            n
            for (n,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        content = {}
        for table in tables:
            if table in EXCLUDED_TABLES or table in PLAIN_ORDER_SKIP:
                continue
            content[table] = _digest(_plain_table(conn, table))

        rows = conn.execute(
            f"SELECT {COMPONENT_CONTENT} FROM tariff_components "
            f"ORDER BY {COMPONENT_CONTENT}"
        ).fetchall()
        content["tariff_components (ids ignored)"] = _digest(rows)

        runs = []
        for bill_id, mapping, report in conn.execute(
            "SELECT bill_id, mapping_version, report_json FROM audit_runs "
            "WHERE mapping_version = ? ORDER BY bill_id",
            (current_mapping,),
        ):
            runs.append((bill_id, mapping, json.dumps(_scrub(json.loads(report)), sort_keys=True)))
        content["audit_runs (current mapping, ids/hashes ignored)"] = _digest(runs)

        lines = conn.execute(
            f"""
            SELECT r.bill_id, l.bill_charge_id, l.line_label, l.disposition, l.verdict,
                   l.billed_amount, l.expected_amount, l.delta, l.detail,
                   (SELECT json_array({', '.join('c.' + c.strip() for c in COMPONENT_CONTENT.split(','))})
                    FROM tariff_components c WHERE c.id = l.tariff_component_id)
            FROM audit_lines l JOIN audit_runs r ON r.id = l.audit_run_id
            WHERE r.mapping_version = ?
            ORDER BY r.bill_id, l.bill_charge_id, l.line_label
            """,
            (current_mapping,),
        ).fetchall()
        content["audit_lines (current mapping, component by content)"] = _digest(lines)
        return content
    finally:
        conn.close()


def compare(left, right, current_mapping):
    a = canonical_content(left, current_mapping)
    b = canonical_content(right, current_mapping)
    report = {}
    for name in sorted(set(a) | set(b)):
        report[name] = {
            "left": a.get(name),
            "right": b.get(name),
            "equal": a.get(name) == b.get(name),
        }
    overall = lambda c: hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()
    return {
        "equal": all(v["equal"] for v in report.values()),
        "left_hash": overall(a),
        "right_hash": overall(b),
        "tables": report,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--mapping", type=Path, default=ROOT / "docs" / "bill-label-mapping.json")
    args = parser.parse_args()
    current = json.loads(args.mapping.read_text())["mapping_version"]
    result = compare(args.left, args.right, current)
    for name, entry in result["tables"].items():
        rows = (entry["left"] or {}).get("rows"), (entry["right"] or {}).get("rows")
        print(f"{'EQUAL' if entry['equal'] else 'DIFF ':5} {name}  rows {rows[0]} / {rows[1]}")
    print(f"left  content hash: {result['left_hash']}")
    print(f"right content hash: {result['right_hash']}")
    print("RESULT:", "EQUAL" if result["equal"] else "DIFFERENT")
    return 0 if result["equal"] else 1


if __name__ == "__main__":
    sys.exit(main())
