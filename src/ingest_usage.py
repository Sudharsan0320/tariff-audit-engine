import argparse
import sqlite3

if __package__:
    from .parse_account_profile import extract_account_profile
    from .validate_usage import DATABASE_PATH, validate_account_profile
else:
    from parse_account_profile import extract_account_profile
    from validate_usage import DATABASE_PATH, validate_account_profile


ACCOUNT_COLUMNS = (
    "source_path",
    "account_number",
    "account_name",
    "customer_class",
    "mailing_address",
    "service_address",
    "current_rate_raw",
    "schedule_code",
    "turn_on_date",
    "meter_numbers",
    "voltage",
    "delivery_phase",
    "minimum_demand",
    "facility_charge",
    "billing_status",
    "district_office",
    "status",
    "quarantine_reason",
)


def persist_account_profile(conn, profile):
    """Validate an extracted profile and store or quarantine it."""
    failures = validate_account_profile(profile, conn)
    row = {key: profile.get(key) for key in ACCOUNT_COLUMNS if key not in {
        "status",
        "quarantine_reason",
    }}
    if failures:
        row["status"] = "quarantine"
        row["quarantine_reason"] = "; ".join(failures)
    else:
        row["status"] = "stored"
        row["quarantine_reason"] = None

    placeholders = ", ".join("?" for _ in ACCOUNT_COLUMNS)
    assignments = ", ".join(
        f"{column}=excluded.{column}"
        for column in ACCOUNT_COLUMNS
        if column not in {"source_path", "account_number"}
    )
    conn.execute(
        f"""
        INSERT INTO usage_accounts ({", ".join(ACCOUNT_COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT(source_path, account_number) DO UPDATE SET {assignments}
        """,
        tuple(row[column] for column in ACCOUNT_COLUMNS),
    )
    return row["status"], row["quarantine_reason"]


def ingest_account_profile(conn, pdf_path):
    profile = extract_account_profile(pdf_path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        status, reason = persist_account_profile(conn, profile)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return profile, status, reason


def main():
    parser = argparse.ArgumentParser(
        description="Extract, validate, and store or quarantine an EAP profile."
    )
    parser.add_argument("pdf_path", type=str, help="Path to the EAP PDF")
    args = parser.parse_args()

    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            profile, status, reason = ingest_account_profile(conn, args.pdf_path)
        except (sqlite3.Error, ValueError, FileNotFoundError) as error:
            print(f"FAIL: {error}")
            return 1

    print(
        f"{status.upper()}: account {profile['account_number']} "
        f"schedule {profile['current_rate_raw']} -> {profile['schedule_code']}"
    )
    if reason:
        print(reason)
    return 0 if status == "stored" else 1


if __name__ == "__main__":
    raise SystemExit(main())
