"""One-time migration: bills.source_file becomes project-relative.

Named Phase 7 step. Steps, in one transaction after a file backup:
  1. Rewrite bills.source_file under the project root to its relative form.
  2. Recompute audit_runs.input_hash for current-mapping runs only, using
     audit_bills' own computation. Older mapping runs stay byte-identical.
  3. Refuse (and roll back) if any current-mapping result_hash or
     tariff_snapshot_hash would change: the path must not leak into results.
Idempotent: a second run changes nothing.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from src.audit_bills import DEFAULT_MAPPING_PATH, _compute_bill_audit, load_mapping

ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = Path(os.environ.get("TARIFF_DB_PATH") or ROOT / "data" / "tariffs.db")


def _relative_form(value):
    path = Path(value)
    if not path.is_absolute():
        return value
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return value


def migrate(conn, mapping_path=DEFAULT_MAPPING_PATH):
    mapping, mapping_hash = load_mapping(mapping_path)
    current = mapping["mapping_version"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        bills_changed = 0
        for bill_id, source_file in conn.execute(
            "SELECT id, source_file FROM bills"
        ).fetchall():
            new_value = _relative_form(source_file)
            if new_value != source_file:
                conn.execute(
                    "UPDATE bills SET source_file = ? WHERE id = ?",
                    (new_value, bill_id),
                )
                bills_changed += 1

        runs_changed = 0
        runs = conn.execute(
            """
            SELECT id, bill_id, input_hash, result_hash, tariff_snapshot_hash
            FROM audit_runs WHERE mapping_version = ? ORDER BY id
            """,
            (current,),
        ).fetchall()
        for run_id, bill_id, input_hash, result_hash, tariff_hash in runs:
            result = _compute_bill_audit(conn, bill_id, mapping, mapping_hash)
            if (
                result["result_hash"] != result_hash
                or result["report"]["tariff_snapshot_hash"] != tariff_hash
            ):
                raise ValueError(
                    f"Audit run {run_id}: result or tariff hash would change; "
                    "the path leaks into results. Aborting."
                )
            if result["input_hash"] != input_hash:
                conn.execute(
                    "UPDATE audit_runs SET input_hash = ? WHERE id = ?",
                    (result["input_hash"], run_id),
                )
                runs_changed += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "bills_changed": bills_changed,
        "audit_runs_rekeyed": runs_changed,
        "mapping_version": current,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument(
        "--no-backup", action="store_true", help="Skip the file backup (tests only)."
    )
    args = parser.parse_args()
    try:
        if not args.no_backup:
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            backup = args.database.with_name(
                f"{args.database.name}.pre-source-file-migration.{stamp}.bak"
            )
            shutil.copy2(args.database, backup)
            print(f"backup: {backup}")
        with sqlite3.connect(args.database) as conn:
            summary = migrate(conn, args.mapping)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
