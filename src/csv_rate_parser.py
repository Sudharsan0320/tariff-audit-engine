"""B15 second adapter: a CSV rate sheet that writes a draft version.

It exists to show the parser contract is not PDF-shaped. Columns: schedule_code, version,
effective_from, component_type, unit, rate, source_page, source_cell. State and utility
are parameters (the sheet does not carry them). Everything is validated before anything
is written; a refused sheet writes nothing. Versions are always written as `draft`.
"""

import argparse
import csv
from collections import Counter, OrderedDict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys

try:
    from src import db
except ImportError:
    import db

ROOT = Path(__file__).resolve().parent.parent
COLUMNS = ("schedule_code", "version", "effective_from", "component_type", "unit", "rate",
           "source_page", "source_cell")


def _read_sheet(source):
    try:
        with open(source, "r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError("Not a rate sheet; missing columns: " + ", ".join(missing))
            rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError(f"Unreadable rate sheet: {error}") from error
    if not rows:
        raise ValueError("The rate sheet has no data rows.")
    return rows


def _clean(rows):
    """Validate every row; return {(code, version, effective_from): [component tuples]}."""
    groups = OrderedDict()
    for number, row in enumerate(rows, start=2):
        where = f"row {number}"
        code = (row["schedule_code"] or "").strip()
        ctype = (row["component_type"] or "").strip()
        unit = (row["unit"] or "").strip()
        cell = (row["source_cell"] or "").strip()
        if not (code and ctype and unit):
            raise ValueError(f"{where}: schedule_code, component_type and unit are required.")
        try:
            version = int(row["version"])
            effective_from = date.fromisoformat((row["effective_from"] or "").strip()).isoformat()
            rate = Decimal((row["rate"] or "").strip())
            page = int(row["source_page"])
        except (ValueError, InvalidOperation) as error:
            raise ValueError(f"{where}: invalid version, effective_from, rate or source_page.") from error
        if not rate.is_finite():
            raise ValueError(f"{where}: rate must be a finite number.")
        if page < 1 or not cell:
            raise ValueError(f"{where}: a citation (source_page >= 1 and source_cell) is required.")
        groups.setdefault((code, version, effective_from), []).append(
            (ctype, unit, float(rate), page, cell))
    return groups


def parse_csv(source, database_path, *, state, utility):
    """Store the sheet as draft versions. Returns [(version_id, components_inserted)]."""
    if not str(state or "").strip() or not str(utility or "").strip():
        raise ValueError("state and utility are required.")
    groups = _clean(_read_sheet(source))
    results = []
    with db.connect(database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for (code, version, effective_from), components in groups.items():
                existing = conn.execute(
                    "SELECT id FROM tariff_versions WHERE state = ? AND utility = ? "
                    "AND schedule_code = ? AND version = ? AND effective_from = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (state, utility, code, version, effective_from)).fetchone()
                if existing is not None:
                    stored = conn.execute(
                        "SELECT component_type, unit, rate, source_page, source_cell "
                        "FROM tariff_components WHERE tariff_version_id = ?",
                        (existing[0],)).fetchall()
                    key = lambda r: (r[0], r[1], Decimal(str(r[2])), int(r[3]), r[4])
                    if Counter(map(key, stored)) != Counter(map(key, components)):
                        raise ValueError(
                            f"Version {existing[0]} of schedule {code} already exists with "
                            "different components; refusing to overwrite.")
                    results.append((existing[0], 0))
                    continue
                version_id = conn.execute(
                    "INSERT INTO tariff_versions(state, utility, schedule_code, version, "
                    "effective_from, status) VALUES (?, ?, ?, ?, ?, 'draft')",
                    (state, utility, code, version, effective_from)).lastrowid
                for ctype, unit, rate, page, cell in components:
                    conn.execute(
                        "INSERT INTO tariff_components(tariff_version_id, component_type, "
                        "billing_category, rate, formula, unit, source_page, source_cell) "
                        "VALUES (?, ?, NULL, ?, NULL, ?, ?, ?)",
                        (version_id, ctype, rate, unit, page, cell))
                results.append((version_id, len(components)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Load a CSV rate sheet as draft tariff versions.")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--utility", required=True)
    args = parser.parse_args(argv)
    try:
        results = parse_csv(args.csv, db.default_target(ROOT / "data" / "tariffs.db"),
                            state=args.state, utility=args.utility)
    except (OSError, ValueError, db.Error) as error:
        print(f"FAIL: {error}")
        return 1
    for version_id, count in results:
        print(f"Version {version_id}: {count} components inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
