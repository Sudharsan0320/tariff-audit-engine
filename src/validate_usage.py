from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3

try:
    from src import db
except ImportError:
    import db


ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = db.default_target(ROOT / "data" / "tariffs.db")
REQUIRED_FIELDS = (
    "account_number",
    "account_name",
    "customer_class",
    "service_address",
    "current_rate_raw",
    "schedule_code",
    "turn_on_date",
    "billing_status",
)


def validate_account_profile(profile, conn=None):
    """Return validation failures for one extracted account profile."""
    failures = []
    for field in REQUIRED_FIELDS:
        value = profile.get(field)
        if not isinstance(value, str) or not value.strip():
            failures.append(f"Missing {field}.")

    account_number = profile.get("account_number")
    if isinstance(account_number, str) and account_number.strip():
        if not account_number.isdigit():
            failures.append(f"Account number is not numeric: {account_number!r}.")

    turn_on_date = profile.get("turn_on_date")
    parsed_date = None
    if isinstance(turn_on_date, str) and turn_on_date.strip():
        try:
            parsed_date = datetime.strptime(turn_on_date, "%Y-%m-%d").date()
        except ValueError:
            failures.append(f"Turn on date is not ISO: {turn_on_date!r}.")
        else:
            if parsed_date > date.today():
                failures.append(f"Turn on date is in the future: {turn_on_date}.")

    billing_status = profile.get("billing_status")
    if isinstance(billing_status, str) and billing_status.strip():
        if billing_status not in {"Active", "Inactive"}:
            failures.append(f"Unknown billing status: {billing_status!r}.")

    schedule_code = profile.get("schedule_code")
    if conn is not None and isinstance(schedule_code, str) and schedule_code.strip():
        matches = conn.execute(
            """
            SELECT COUNT(*)
            FROM tariff_versions
            WHERE schedule_code = ? AND status = 'active'
            """,
            (schedule_code,),
        ).fetchone()[0]
        if matches == 0:
            failures.append(
                f"No active tariff version for schedule_code {schedule_code}."
            )

    return failures


CENT = Decimal("0.01")
BASIC_CHARGE_LABELS = ("Basic Cust. Charges", "Basic Customer Chg")
NEGATIVE_CHARGE_KEYWORDS = ("credit", "payment")


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError("date is missing or not an ISO date")


def _as_decimal(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("value is missing or not numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("value is not numeric") from None
    if not number.is_finite():
        raise ValueError("value is not finite")
    return number


def _bill_context(account_number, bill):
    return (
        f"Account {account_number!r} bill "
        f"{bill.get('bill_from')!r} to {bill.get('bill_to')!r}"
    )


def _tariff_windows(conn, schedule_code):
    return conn.execute(
        """
        SELECT effective_from, effective_to
        FROM tariff_versions
        WHERE schedule_code = ? AND status IN ('active', 'superseded')
        """,
        (schedule_code,),
    ).fetchall()


def _window_covers_period(windows, bill_from, bill_to):
    for effective_from, effective_to in windows:
        try:
            start = _as_date(effective_from)
            end = _as_date(effective_to) if effective_to else None
        except ValueError:
            continue
        if start <= bill_from and (end is None or bill_to <= end):
            return True
    return False


def _account_matches_database(conn, account):
    row = conn.execute(
        """
        SELECT customer_name, service_address, meter_number, customer_class,
               voltage, delivery_phase, turn_on_date, current_rate_raw
        FROM accounts WHERE account_number = ?
        """,
        (account.get("account_number"),),
    ).fetchone()
    if row is None:
        return False
    expected = (
        account.get("account_name"),
        account.get("service_address"),
        account.get("meter_number"),
        account.get("customer_class"),
        account.get("voltage"),
        account.get("delivery_phase"),
        account.get("turn_on_date"),
        account.get("current_rate_raw"),
    )
    return tuple(row) == expected


def _bill_matches_database(conn, bill, resolution):
    row = conn.execute(
        """
        SELECT id, billing_days, billed_rate_raw, schedule_code,
             total_consumption_kwh, demand_kw, total_billed, source_file,
             history_year_status
        FROM bills
        WHERE account_number = ? AND bill_from = ? AND bill_to = ?
        """,
        (bill.get("account_number"), bill.get("bill_from"), bill.get("bill_to")),
    ).fetchone()
    if row is None:
        return False
    bill_id, days, rate, schedule, usage, demand, total, source_file, year_status = row
    try:
        numeric_matches = (
            Decimal(str(usage)) == _as_decimal(bill.get("total_consumption_kwh"))
            and (
                demand is None
                and bill.get("demand_kw") is None
                or Decimal(str(demand)) == _as_decimal(bill.get("demand_kw"))
            )
            and (
                total is None
                and bill.get("total_billed") is None
                or Decimal(str(total)) == _as_decimal(bill.get("total_billed"))
            )
        )
    except (ValueError, InvalidOperation):
        return False
    if not (
        days == bill.get("billing_days")
        and rate == bill.get("billed_rate_raw")
        and schedule == bill.get("schedule_code")
        and source_file == bill.get("source_file")
        and year_status == bill.get("history_year_status", "complete")
        and numeric_matches
    ):
        return False

    expected_charges = [
        (charge.get("line_label"), _as_decimal(charge.get("amount")))
        for charge in bill.get("bill_charges", [])
        if not (
            resolution == "exclude_basic_customer_chg"
            and charge.get("line_label") == BASIC_CHARGE_LABELS[1]
        )
    ]
    stored_charges = conn.execute(
        "SELECT line_label, amount FROM bill_charges WHERE bill_id = ? ORDER BY id",
        (bill_id,),
    ).fetchall()
    try:
        actual_charges = [(label, Decimal(str(amount))) for label, amount in stored_charges]
    except InvalidOperation:
        return False
    return actual_charges == expected_charges


def _determinants_for_bill(conn, bill):
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'bill_determinants'"
    ).fetchone()
    expected = bill.get("bill_determinants", [])
    if not table_exists:
        return (not expected, False, "bill_determinants table is missing")

    row = conn.execute(
        """
        SELECT id FROM bills
        WHERE account_number = ? AND bill_from = ? AND bill_to = ?
        """,
        (bill.get("account_number"), bill.get("bill_from"), bill.get("bill_to")),
    ).fetchone()
    if row is None:
        return False, False, "stored bill is missing"

    stored = conn.execute(
        """
        SELECT label, determinant_type, value
        FROM bill_determinants WHERE bill_id = ?
        """,
        (row[0],),
    ).fetchall()
    try:
        stored_by_label = {
            label: (determinant_type, _as_decimal(value))
            for label, determinant_type, value in stored
        }
    except ValueError:
        return False, False, "stored determinant value is invalid"
    expected_by_label = {}
    for determinant in expected:
        label = determinant.get("label")
        determinant_type = determinant.get("determinant_type")
        if not isinstance(label, str) or not label.strip():
            return False, False, "expected determinant label is invalid"
        if not isinstance(determinant_type, str) or not determinant_type.strip():
            return False, False, f"expected determinant {label!r} has no type"
        try:
            value = _as_decimal(determinant.get("value"))
        except ValueError:
            return False, False, f"invalid expected determinant {label!r}"
        if label in expected_by_label:
            return False, False, f"duplicate expected determinant {label!r}"
        expected_by_label[label] = (determinant_type, value)

    if any(label not in expected_by_label for label in stored_by_label):
        return False, False, "stored determinants do not match the source draft"
    for label, (determinant_type, value) in stored_by_label.items():
        if expected_by_label[label] != (determinant_type, value):
            return False, False, f"stored determinant {label!r} conflicts with source"
    return True, len(stored_by_label) < len(expected_by_label), None


def _charge_sum(bill):
    total = Decimal(0)
    for charge in bill.get("bill_charges", []):
        total += _as_decimal(charge.get("amount"))
    return total


def _basic_charge_resolution(bills):
    paired = [
        bill
        for bill in bills
        if all(
            sum(charge.get("line_label") == label for charge in bill.get("bill_charges", []))
            for label in BASIC_CHARGE_LABELS
        )
        and bill.get("total_billed") is not None
    ]
    if not paired:
        return "no_duplicate_pair"

    both_match = True
    one_match = True
    for bill in bills:
        if bill.get("total_billed") is None:
            continue
        total = _as_decimal(bill["total_billed"])
        amount_sum = _charge_sum(bill)
        if abs(amount_sum - total) > CENT:
            both_match = False
        omitted = sum(
            (
                _as_decimal(charge["amount"])
                for charge in bill.get("bill_charges", [])
                if charge.get("line_label") == BASIC_CHARGE_LABELS[1]
            ),
            Decimal(0),
        )
        if abs(amount_sum - omitted - total) > CENT:
            one_match = False

    if both_match:
        return "retain_both"
    if one_match:
        return "exclude_basic_customer_chg"
    return "unresolved"


def validate_usage_draft(draft, conn):
    """Validate a parsed EAP draft without mutating it or the database."""
    failures = []
    warnings = []
    information = []
    account = draft.get("account") or {}
    account_number = account.get("account_number")
    bills = draft.get("bills") or []
    if not bills:
        failures.append(f"Account {account_number!r}: no monthly bills were extracted.")
    for field in (
        "account_name",
        "service_address",
        "meter_number",
        "customer_class",
        "voltage",
        "delivery_phase",
        "turn_on_date",
        "current_rate_raw",
    ):
        if not isinstance(account.get(field), str) or not account[field].strip():
            failures.append(f"Account {account_number!r}: missing profile field {field}.")
    parsed_periods = []
    seen_periods = set()
    existing_periods = set()
    schedule_windows = {}

    if not isinstance(account_number, str) or not account_number.strip():
        failures.append("Account is missing a non-empty account_number.")

    for bill in bills:
        context = _bill_context(account_number, bill)
        for field in (
            "account_number",
            "bill_from",
            "bill_to",
            "billing_days",
            "billed_rate_raw",
            "total_consumption_kwh",
        ):
            value = bill.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                failures.append(f"{context}: missing required field {field}.")

        if bill.get("account_number") != account_number:
            failures.append(f"{context}: bill account does not match the profile.")

        bill_from = bill_to = None
        try:
            bill_from = _as_date(bill.get("bill_from"))
            bill_to = _as_date(bill.get("bill_to"))
        except ValueError as error:
            failures.append(f"{context}: invalid period date ({error}).")

        if bill_from is not None and bill_to is not None:
            if bill_from >= bill_to:
                failures.append(f"{context}: bill_from must be before bill_to.")
            try:
                billing_days_value = _as_decimal(bill.get("billing_days"))
                if billing_days_value != billing_days_value.to_integral_value():
                    raise ValueError("value must be an integer")
                billing_days = int(billing_days_value)
            except ValueError:
                billing_days = None
                failures.append(f"{context}: billing_days must be an integer.")
            if billing_days is not None and billing_days != (bill_to - bill_from).days + 1:
                failures.append(
                    f"{context}: billing_days {billing_days} does not match the "
                    f"inclusive period length {(bill_to - bill_from).days + 1}."
                )
            period_key = (account_number, bill_from, bill_to)
            if period_key in seen_periods:
                failures.append(f"{context}: duplicate billing period in draft.")
            seen_periods.add(period_key)
            parsed_periods.append((bill_from, bill_to, bill))
            exists = conn.execute(
                """
                SELECT 1 FROM bills
                WHERE account_number = ? AND bill_from = ? AND bill_to = ?
                LIMIT 1
                """,
                (account_number, bill_from.isoformat(), bill_to.isoformat()),
            ).fetchone()
            if exists:
                existing_periods.add(period_key)

        for field in ("total_consumption_kwh", "demand_kw"):
            value = bill.get(field)
            if value is None and field == "demand_kw":
                continue
            try:
                number = _as_decimal(value)
            except ValueError as error:
                if field == "total_consumption_kwh" or value is not None:
                    failures.append(f"{context}: {field} {error}.")
                continue
            if number < 0:
                failures.append(f"{context}: {field} must be non-negative.")

        if bill.get("total_billed") is not None:
            try:
                total_billed = _as_decimal(bill["total_billed"])
            except ValueError as error:
                failures.append(f"{context}: total_billed {error}.")
            else:
                if total_billed < 0:
                    failures.append(f"{context}: total_billed must be non-negative.")

        raw_rate = bill.get("billed_rate_raw")
        try:
            normalized = normalize_schedule_for_validation(raw_rate)
        except ValueError:
            normalized = None
            failures.append(f"{context}: billed rate {raw_rate!r} cannot be normalized.")
        if normalized is not None:
            matches = conn.execute(
                "SELECT 1 FROM tariff_versions WHERE schedule_code = ? LIMIT 1",
                (normalized,),
            ).fetchone()
            if not matches:
                failures.append(f"{context}: unknown schedule_code {normalized!r}.")
            if bill.get("schedule_code") != normalized:
                failures.append(
                    f"{context}: normalized schedule_code {bill.get('schedule_code')!r} "
                    f"does not match billed rate {raw_rate!r} ({normalized!r})."
                )
            if bill_from is not None and bill_to is not None:
                if normalized not in schedule_windows:
                    schedule_windows[normalized] = _tariff_windows(conn, normalized)
                if not _window_covers_period(
                    schedule_windows[normalized], bill_from, bill_to
                ):
                    warnings.append(
                        f"{context}: audit not yet possible for this period."
                    )

        for charge_index, charge in enumerate(bill.get("bill_charges", []), start=1):
            label = charge.get("line_label")
            if not isinstance(label, str) or not label.strip():
                failures.append(f"{context}: charge line {charge_index} has no printed label.")
            try:
                amount = _as_decimal(charge.get("amount"))
            except ValueError as error:
                failures.append(
                    f"{context}: charge line {charge_index} ({label!r}) {error}."
                )
                continue
            if amount < 0:
                information.append(
                    f"{context}: negative as-printed charge {amount} on line {label!r}."
                )

        seen_determinants = set()
        for determinant_index, determinant in enumerate(
            bill.get("bill_determinants", []), start=1
        ):
            label = determinant.get("label")
            determinant_type = determinant.get("determinant_type")
            if not isinstance(label, str) or not label.strip():
                failures.append(
                    f"{context}: determinant {determinant_index} has no printed label."
                )
            if not isinstance(determinant_type, str) or not determinant_type.strip():
                failures.append(
                    f"{context}: determinant {determinant_index} has no normalized type."
                )
            if isinstance(label, str):
                if label in seen_determinants:
                    failures.append(
                        f"{context}: duplicate determinant label {label!r}."
                    )
                seen_determinants.add(label)
            try:
                determinant_value = _as_decimal(determinant.get("value"))
            except ValueError as error:
                failures.append(
                    f"{context}: determinant {label!r} {error}."
                )
            else:
                if determinant_value < 0:
                    failures.append(
                        f"{context}: determinant {label!r} must be non-negative."
                    )

    parsed_periods.sort(key=lambda item: item[0])
    for previous, current in zip(parsed_periods, parsed_periods[1:]):
        if current[0] <= previous[1]:
            failures.append(
                f"{_bill_context(account_number, current[2])}: period overlaps "
                f"the preceding bill ending {previous[1].isoformat()}."
            )

    resolution = "unresolved"
    try:
        resolution = _basic_charge_resolution(bills)
        for bill in bills:
            context = _bill_context(account_number, bill)
            subtotal = bill.get("bill_summary_subtotal")
            total_value = bill.get("total_billed")
            if subtotal is None:
                failures.append(f"{context}: V7a printed subtotal is missing.")
            if total_value is None:
                failures.append(f"{context}: V7a printed total is missing.")
            if subtotal is None or total_value is None:
                continue
            subtotal = _as_decimal(subtotal)
            total = _as_decimal(total_value)
            amount_sum = _charge_sum(bill)
            if resolution == "exclude_basic_customer_chg":
                amount_sum -= sum(
                    (
                        _as_decimal(charge["amount"])
                        for charge in bill.get("bill_charges", [])
                        if charge.get("line_label") == BASIC_CHARGE_LABELS[1]
                    ),
                    Decimal(0),
                )
            if abs(amount_sum - subtotal) > CENT:
                failures.append(
                    f"{context}: V7a detail lines sum to {amount_sum}, but printed "
                    f"subtotal is {subtotal} (tolerance {CENT})."
                )
            if abs(subtotal - total) > CENT:
                failures.append(
                    f"{context}: V7a printed subtotal {subtotal} differs from "
                    f"printed total {total} (tolerance {CENT})."
                )
    except ValueError as error:
        failures.append(f"Charge reconciliation could not be calculated: {error}.")
        resolution = "unresolved"

    if resolution == "exclude_basic_customer_chg":
        warnings.append(
            "V6 finding: excluding the printed 'Basic Customer Chg' line is the "
            "only basic-charge interpretation that reconciles across months."
        )
    elif resolution == "unresolved":
        failures.append(
            "V6: neither retaining both basic-charge lines nor excluding "
            "'Basic Customer Chg' reconciles across all printed monthly totals."
        )

    idempotent = False
    determinants_enrichment = False
    if existing_periods:
        account_matches = _account_matches_database(conn, account)
        stored_bill_count = conn.execute(
            "SELECT COUNT(*) FROM bills WHERE account_number = ?",
            (account_number,),
        ).fetchone()[0]
        all_periods_exist = (
            len(existing_periods) == len(seen_periods) == stored_bill_count
            and bool(seen_periods)
        )
        try:
            all_bills_match = all(
                _bill_matches_database(conn, bill, resolution) for bill in bills
            )
        except (TypeError, ValueError, InvalidOperation):
            all_bills_match = False
        determinants_match = True
        determinant_enrichment_needed = False
        determinant_mismatch = None
        if account_matches and all_periods_exist and all_bills_match:
            for bill in bills:
                matches, needs_enrichment, mismatch = _determinants_for_bill(
                    conn, bill
                )
                if not matches:
                    determinants_match = False
                    determinant_mismatch = mismatch
                    break
                determinant_enrichment_needed |= needs_enrichment
        if (
            account_matches
            and all_periods_exist
            and all_bills_match
            and determinants_match
            and not failures
        ):
            idempotent = True
            determinants_enrichment = determinant_enrichment_needed
            if determinants_enrichment:
                warnings.append(
                    "Exact account, bill, and charge rows already exist; "
                    "only missing source determinants will be added."
                )
            else:
                warnings.append(
                    "Exact source and billing rows already exist; ingestion is an "
                    "explicit idempotent no-op."
                )
        else:
            if determinant_mismatch:
                failures.append(
                    f"Account {account_number!r}: {determinant_mismatch}."
                )
            for bill in bills:
                period_key = None
                try:
                    period_key = (
                        account_number,
                        _as_date(bill.get("bill_from")),
                        _as_date(bill.get("bill_to")),
                    )
                except ValueError:
                    pass
                if period_key in existing_periods:
                    failures.append(
                        f"{_bill_context(account_number, bill)}: billing period "
                        "already exists in the database and is not an exact full-draft match."
                    )
            if not account_matches and conn.execute(
                "SELECT 1 FROM accounts WHERE account_number = ?",
                (account_number,),
            ).fetchone():
                failures.append(
                    f"Account {account_number!r} already exists with different profile data."
                )

    summaries = {
        summary.get("year"): summary
        for summary in draft.get("annual_summaries", [])
    }
    summary_years = [summary.get("year") for summary in draft.get("annual_summaries", [])]
    if len(summary_years) != len(set(summary_years)):
        failures.append("Annual summary contains duplicate years.")
    years = sorted(
        {bill.get("history_year") for bill in bills}
        | set(summaries)
    )
    annual_variances = []
    for year in years:
        year_bills = [bill for bill in bills if bill.get("history_year") == year]
        witness = summaries.get(year)
        if witness is None:
            information.append(f"V7b year {year}: annual summary witness is missing.")
            continue
        try:
            detail_billed = sum(
                (_as_decimal(bill.get("total_billed")) for bill in year_bills),
                Decimal(0),
            )
            annual_billed = _as_decimal(witness.get("total_billed"))
            detail_usage = sum(
                (
                    _as_decimal(bill.get("total_consumption_kwh"))
                    for bill in year_bills
                ),
                Decimal(0),
            )
            annual_usage = _as_decimal(witness.get("total_usage_kwh"))
        except ValueError as error:
            information.append(
                f"V7b year {year}: annual variance unavailable due to invalid "
                f"numeric data ({error})."
            )
            continue
        statuses = {bill.get("history_year_status", "complete") for bill in year_bills}
        if len(statuses) > 1:
            history_status = "inconsistent"
        elif statuses:
            history_status = next(iter(statuses))
        else:
            history_status = "no_detail"
        annual_variance = {
            "year": year,
            "month_count": len(year_bills),
            "history_year_status": history_status,
            "detail_billed": detail_billed,
            "annual_billed": annual_billed,
            "billed_delta": detail_billed - annual_billed,
            "detail_usage_kwh": detail_usage,
            "annual_usage_kwh": annual_usage,
            "usage_delta_kwh": detail_usage - annual_usage,
        }
        annual_variances.append(annual_variance)
        information.append(
            f"V7b year {year} ({history_status}, {len(year_bills)} detail months): "
            f"billed delta {annual_variance['billed_delta']:+}, usage delta "
            f"{annual_variance['usage_delta_kwh']:+} kWh."
        )

    return {
        "failures": failures,
        "warnings": warnings,
        "information": information,
        "annual_variances": annual_variances,
        "basic_charge_resolution": resolution,
        "idempotent": idempotent,
        "determinants_enrichment": determinants_enrichment,
    }


def normalize_schedule_for_validation(raw_rate):
    if not isinstance(raw_rate, str) or not raw_rate.strip():
        raise ValueError("rate is empty")
    if __package__:
        from .normalize_schedule import normalize_schedule_code
    else:
        from normalize_schedule import normalize_schedule_code
    return normalize_schedule_code(raw_rate)


def main():
    parser_error = "No usage_accounts drafts to validate."
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM usage_accounts
            WHERE status IN ('draft', 'quarantine')
            ORDER BY id
            """
        ).fetchall()
        if not rows:
            print(parser_error)
            return 1

        any_failures = False
        for row in rows:
            profile = dict(row)
            failures = validate_account_profile(profile, conn)
            if failures:
                any_failures = True
                print(f"FAIL account {profile['account_number']}")
                for failure in failures:
                    print(f"- {failure}")
            else:
                print(f"PASS account {profile['account_number']}")

    return 1 if any_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
