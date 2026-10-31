"""Pipeline stage: enrich stored bills with their parsed determinants.

Parses each EAP, upserts missing rows into bill_determinants for bills that are
already stored, and proves that accounts, bills, and bill_charges are untouched.
Idempotent: a second run inserts nothing. It never creates bills; a bill that
is not stored is an error (ingest runs first).
"""

import argparse
import sys
from pathlib import Path

try:
    from src import db
except ImportError:
    import db

if __package__:
    from .ingest_usage_history import (
        _insert_determinants,
        _usage_table_hashes,
        DATABASE_PATH,
    )
    from .parse_usage import parse_usage_pdf
else:
    from ingest_usage_history import (
        _insert_determinants,
        _usage_table_hashes,
        DATABASE_PATH,
    )
    from parse_usage import parse_usage_pdf


def enrich_determinants(conn, draft):
    """Insert missing determinants for stored bills; return the number inserted."""
    conn.execute("PRAGMA foreign_keys = ON")
    before = _usage_table_hashes(conn)
    inserted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for bill in draft["bills"]:
            row = conn.execute(
                """
                SELECT id FROM bills
                WHERE account_number = ? AND bill_from = ? AND bill_to = ?
                """,
                (bill["account_number"], bill["bill_from"], bill["bill_to"]),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"Bill {bill['bill_from']}..{bill['bill_to']} is not stored; "
                    "ingest the usage history before enriching."
                )
            stored = {
                label
                for (label,) in conn.execute(
                    "SELECT label FROM bill_determinants WHERE bill_id = ?", (row[0],)
                )
            }
            for determinant in bill.get("bill_determinants", []):
                if determinant["label"] not in stored:
                    _insert_determinants(
                        conn, row[0], {"bill_determinants": [determinant]}
                    )
                    inserted += 1
        if _usage_table_hashes(conn) != before:
            raise ValueError(
                "Determinant enrichment changed existing account, bill, or charge rows."
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return inserted


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf_path", type=Path, help="Path to a native EAP PDF")
    args = parser.parse_args()
    try:
        draft = parse_usage_pdf(args.pdf_path)
        expected = sum(len(bill.get("bill_determinants", [])) for bill in draft["bills"])
        with db.connect(DATABASE_PATH) as conn:
            inserted = enrich_determinants(conn, draft)
            stored = conn.execute(
                """
                SELECT COUNT(*) FROM bill_determinants d JOIN bills b ON b.id = d.bill_id
                WHERE b.source_file = ?
                """,
                (draft["source_file"],),
            ).fetchone()[0]
        if stored != expected:
            raise ValueError(
                f"Determinants incomplete: {stored} stored, {expected} parsed."
            )
    except (OSError, db.Error, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    print(f"PASS: {inserted} determinants inserted; {stored} stored, {expected} parsed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
