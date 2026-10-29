import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

if __package__:
    from .parse_usage import parse_usage_pdf, summarize_draft
    from .validate_usage import (
        DATABASE_PATH,
        _account_matches_database,
        validate_usage_draft,
    )
else:
    from parse_usage import parse_usage_pdf, summarize_draft
    from validate_usage import (
        DATABASE_PATH,
        _account_matches_database,
        validate_usage_draft,
    )


ACCOUNT_COLUMNS = (
    "account_number",
    "customer_name",
    "service_address",
    "meter_number",
    "customer_class",
    "voltage",
    "delivery_phase",
    "turn_on_date",
    "current_rate_raw",
)


def _database_snapshot(conn):
    snapshot = {}
    for table in ("accounts", "bills", "bill_charges"):
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        primary_key = [
            row[1]
            for row in sorted(
                (row for row in conn.execute(f"PRAGMA table_info({table})") if row[5]),
                key=lambda row: row[5],
            )
        ]
        order = ", ".join(primary_key or columns)
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}"
        ).fetchall()
        snapshot[table] = rows
    payload = json.dumps(snapshot, ensure_ascii=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def snapshot_hash(conn):
    """Return a stable hash of all rows in the three U1 usage tables."""
    return _database_snapshot(conn)


def _account_row(account):
    return (
        account.get("account_number"),
        account.get("account_name"),
        account.get("service_address"),
        account.get("meter_number"),
        account.get("customer_class"),
        account.get("voltage"),
        account.get("delivery_phase"),
        account.get("turn_on_date"),
        account.get("current_rate_raw"),
    )


def _amount_for_sql(value):
    return float(Decimal(str(value)))


def load_usage_draft(conn, draft):
    """Validate and transactionally load one draft, or return without writes."""
    conn.execute("PRAGMA foreign_keys = ON")
    before_hash = snapshot_hash(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        validation = validate_usage_draft(draft, conn)
        if validation["failures"]:
            conn.rollback()
            return "rejected", validation, before_hash
        if validation["idempotent"]:
            conn.commit()
            return "no-op", validation, snapshot_hash(conn)

        account = draft["account"]
        existing_account = conn.execute(
            "SELECT 1 FROM accounts WHERE account_number = ?",
            (account["account_number"],),
        ).fetchone()
        if existing_account:
            if not _account_matches_database(conn, account):
                raise ValueError(
                    f"Account {account['account_number']!r} exists with different profile data."
                )
        else:
            conn.execute(
                f"INSERT INTO accounts ({', '.join(ACCOUNT_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in ACCOUNT_COLUMNS)})",
                _account_row(account),
            )

        for bill in draft["bills"]:
            cursor = conn.execute(
                """
                INSERT INTO bills(
                    account_number, bill_from, bill_to, billing_days,
                    billed_rate_raw, schedule_code, total_consumption_kwh,
                    demand_kw, total_billed, source_file, history_year_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account["account_number"],
                    bill["bill_from"],
                    bill["bill_to"],
                    bill["billing_days"],
                    bill["billed_rate_raw"],
                    bill["schedule_code"],
                    float(Decimal(str(bill["total_consumption_kwh"]))),
                    None
                    if bill.get("demand_kw") is None
                    else float(Decimal(str(bill["demand_kw"]))),
                    None
                    if bill.get("total_billed") is None
                    else _amount_for_sql(bill["total_billed"]),
                    bill["source_file"],
                    bill.get("history_year_status", "complete"),
                ),
            )
            for charge in bill.get("bill_charges", []):
                if (
                    validation["basic_charge_resolution"]
                    == "exclude_basic_customer_chg"
                    and charge.get("line_label") == "Basic Customer Chg"
                ):
                    continue
                conn.execute(
                    """
                    INSERT INTO bill_charges(bill_id, line_label, amount)
                    VALUES (?, ?, ?)
                    """,
                    (
                        cursor.lastrowid,
                        charge["line_label"],
                        _amount_for_sql(charge["amount"]),
                    ),
                )
        conn.commit()
        return "loaded", validation, snapshot_hash(conn)
    except Exception:
        conn.rollback()
        raise


def ingest_usage_pdf(conn, pdf_path):
    draft = parse_usage_pdf(pdf_path)
    status, validation, final_hash = load_usage_draft(conn, draft)
    return draft, status, validation, final_hash


def _print_result(draft, status, validation):
    account_number = draft.get("account", {}).get("account_number")
    if validation["failures"]:
        print(f"FAIL account {account_number}")
        for failure in validation["failures"]:
            print(f"- {failure}")
    else:
        print(f"PASS account {account_number}: {status}")
    for warning in validation["warnings"]:
        print(f"WARNING: {warning}")
    for information in validation.get("information", []):
        print(f"INFO: {information}")
    print(json.dumps(validation.get("annual_variances", []), indent=2, default=str))
    print(json.dumps(summarize_draft(draft), indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(
        description="Parse, validate, and transactionally load a Dominion EAP history."
    )
    parser.add_argument("pdf_path", type=Path, help="Path to a native EAP PDF")
    args = parser.parse_args()
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            draft, status, validation, _ = ingest_usage_pdf(conn, args.pdf_path)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    _print_result(draft, status, validation)
    return 1 if validation["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())