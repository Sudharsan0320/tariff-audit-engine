"""Reprice a bill under every base schedule: a full computation or one reason.

Computation only. The engine's evaluation core is reused, but never called raw:
every candidate first passes the selection layer below, so the engine only sees
components whose class, variant and determinants are resolved from stored data.
"""

import argparse
from decimal import Decimal
import re
import sys
from pathlib import Path

try:
    from src import db
except ImportError:
    import db

if __package__:
    from .audit_bills import (
        DEFAULT_MAPPING_PATH,
        _components_for_version,
        _fetch_bill,
        _rules_for_version,
        load_mapping,
    )
    from .billing_engine import (
        _season_for_month,
        evaluate_schedule_components,
        resolve_period_version,
        round_money,
    )
else:
    from audit_bills import (
        DEFAULT_MAPPING_PATH,
        _components_for_version,
        _fetch_bill,
        _rules_for_version,
        load_mapping,
    )
    from billing_engine import (
        _season_for_month,
        evaluate_schedule_components,
        resolve_period_version,
        round_money,
    )


ROOT = Path(__file__).resolve().parents[1]

CROSS_REFERENCE_ONLY = "cross_reference_only"
NO_COVERING_VERSION = "no_covering_version"
MISSING_DETERMINANT = "missing_determinant"
NEEDS_ELECTION_DATA = "needs_election_data"
NEEDS_CONTRACTED_AMOUNT = "needs_contracted_amount"
REASONS = (
    CROSS_REFERENCE_ONLY,
    NO_COVERING_VERSION,
    MISSING_DETERMINANT,
    NEEDS_ELECTION_DATA,
    NEEDS_CONTRACTED_AMOUNT,
)

FIXED_MONTHLY_UNITS = {"usd_per_month", "usd_per_unit_month"}
VOLTAGE_VARIANTS = {"primary", "secondary"}
METER_VARIANTS = {"metered", "unmetered"}
USAGE_VARIANT_DETERMINANTS = {"on_peak": "on_peak_usage", "off_peak": "off_peak_usage"}
CONTRACT_AMOUNT_RULES = (
    ("contract_demand", "contract demand from the customer's agreement"),
    ("payment_basis", "contracted capacity level (CL)"),
)
ELECTION_RULE_TYPES = {"contribution_options": "customer election of a contribution option"}


class RepricingError(RuntimeError):
    """An unexpected failure: a bug or a data defect, never a candidate reason."""


class _Blocked(Exception):
    def __init__(self, reason, blocker):
        super().__init__(f"{reason}: {blocker}")
        self.reason = reason
        self.blocker = blocker


def _row_dicts(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _date(value):
    from datetime import date, datetime

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _fixed_text(rules, rule_type):
    return next((r["rule_text"] for r in rules if r["rule_type"] == rule_type), None)


def _history(conn, bill):
    cursor = conn.execute(
        """
        SELECT id, bill_from, bill_to, total_consumption_kwh
        FROM bills WHERE account_number = ? AND bill_to <= ?
        ORDER BY bill_to DESC, id DESC
        """,
        (bill["account_number"], bill["bill_to"]),
    )
    return [
        {
            "id": row["id"],
            "bill_from": _date(row["bill_from"]),
            "bill_to": _date(row["bill_to"]),
            "kwh": None
            if row["total_consumption_kwh"] is None
            else Decimal(str(row["total_consumption_kwh"])),
        }
        for row in _row_dicts(cursor)
    ]


def _usage_window(history, bill, months_back):
    """Current bill plus the previous contiguous bills, newest first."""
    needed = months_back + 1
    window = []
    for entry in history:
        if not window:
            if entry["id"] != bill["id"]:
                continue
        else:
            gap = (window[-1]["bill_from"] - entry["bill_to"]).days
            if gap not in (0, 1) or entry["kwh"] is None:
                break
        if entry["kwh"] is None:
            break
        window.append(entry)
        if len(window) == needed:
            break
    return window, len(window) == needed


def _parse_threshold(text, label):
    months = re.search(r"previous\s+(\d+)\s+(?:consecutive\s+)?billing months", text, re.I)
    if months is None:
        raise _Blocked(MISSING_DETERMINANT, f"parsable look-back window in the stored {label} rule")
    return int(months.group(1))


def _number_before(text, phrase):
    match = re.search(rf"(\d[\d,]*(?:\.\d+)?)\s*kWh {phrase}", text, re.I)
    return None if match is None else Decimal(match.group(1).replace(",", ""))


def _select_class(components, rules, bill, history):
    classes = {c["billing_class"] for c in components if c["billing_class"]}
    if not classes:
        return None, None
    if classes != {"demand", "non_demand"}:
        raise _Blocked(MISSING_DETERMINANT, f"selection rule for billing classes {sorted(classes)}")
    text = _fixed_text(rules, "billing_class_threshold")
    if text is None:
        raise _Blocked(MISSING_DETERMINANT, "stored billing_class_threshold rule")
    months_back = _parse_threshold(text, "billing_class_threshold")
    high = _number_before(text, "or more")
    if high is None:
        raise _Blocked(MISSING_DETERMINANT, "parsable demand threshold in the stored billing_class_threshold rule")
    record = {"rule": text, "demand_determinant_present": "demand" in bill["determinants"]}
    if "demand" not in bill["determinants"]:
        record.update(selected="non_demand", basis="no demand determinant on the bill")
        return "non_demand", record
    window, complete = _usage_window(history, bill, months_back)
    peak = max((entry["kwh"] for entry in window), default=None)
    record["window_months"] = len(window)
    if peak is not None and peak >= high:
        record.update(selected="demand", basis=f"demand determinant present and {peak} kWh >= {high} kWh in the window")
        return "demand", record
    if not complete:
        raise _Blocked(MISSING_DETERMINANT, f"{months_back + 1} contiguous billed months of kWh history")
    record.update(selected="non_demand", basis=f"demand determinant present but peak monthly {peak} kWh < {high} kWh")
    return "non_demand", record


def _select_meter_variant(components, rules, bill, history):
    variants = {c["variant"] for c in components if c["variant"] in METER_VARIANTS}
    if variants != METER_VARIANTS:
        return None
    metered = bool(bill.get("meter_number"))
    text = _fixed_text(rules, "metered_unmetered_charge_switch")
    record = {"account_metered": metered, "rule": text}
    if not metered:
        selected = "unmetered"
        record["basis"] = "account has no stored meter number"
    elif text is None:
        selected = "metered"
        record["basis"] = "account has a stored meter number; the version stores no usage-based switch rule"
    else:
        months_back = _parse_threshold(text, "metered_unmetered_charge_switch")
        low = _number_before(text, "or less")
        high = _number_before(text, "or more")
        if low is None or high is None:
            raise _Blocked(MISSING_DETERMINANT, "parsable usage thresholds in the stored metered_unmetered_charge_switch rule")
        window, complete = _usage_window(history, bill, months_back)
        peak = max((entry["kwh"] for entry in window), default=None)
        record["window_months"] = len(window)
        if peak is not None and peak >= high:
            selected = "metered"
            record["basis"] = f"peak monthly {peak} kWh >= {high} kWh in the window"
        elif complete and peak <= low:
            selected = "unmetered"
            record["basis"] = f"all {len(window)} monthly kWh <= {low}"
        else:
            raise _Blocked(MISSING_DETERMINANT, f"{months_back + 1} contiguous billed months of kWh history")
    record["selected"] = selected
    record["excluded"] = sorted(METER_VARIANTS - {selected})
    return record


def _select_voltage_variant(components, bill):
    variants = {c["variant"] for c in components if c["variant"] in VOLTAGE_VARIANTS}
    if not variants:
        return None
    voltage = str(bill.get("voltage") or "").strip().casefold()
    if voltage not in variants:
        raise _Blocked(MISSING_DETERMINANT, "service voltage class matching the primary/secondary rate variants")
    return {"account_voltage": bill["voltage"], "selected": voltage, "excluded": sorted(variants - {voltage})}


def _requirements(component):
    unit = component["unit"]
    variant = component["variant"]
    if component.get("formula") is not None:
        raise RepricingError(f"Formula component {component['id']} has no documented evaluator.")
    if unit == "usd_per_kwh":
        names = ["demand"] if component.get("tier_unit") == "kwh_per_kw" else []
        names.append(
            "total_consumption"
            if variant is None
            else USAGE_VARIANT_DETERMINANTS.get(variant, f"usage[{variant}]")
        )
        return names
    if unit == "usd_per_kw":
        return ["demand"]
    if unit == "usd_per_rkva":
        return ["rkva_demand"]
    if unit == "usd_per_luminaire":
        return [f"luminaire_count[{variant}]"]
    if unit == "usd_per_hour_unit":
        return ["operating_hours"]
    if unit == "usd_per_service":
        return ["service_count"]
    if unit in FIXED_MONTHLY_UNITS or unit in {"percent", "multiplier"}:
        return []
    raise RepricingError(f"Unknown unit {unit!r} on component {component['id']}.")


def _check_determinants(components, determinants):
    for component in components:
        for name in _requirements(component):
            if name not in determinants:
                raise _Blocked(MISSING_DETERMINANT, name)


def _check_contracted_capacity_basis(components, rules):
    """A per-kW schedule whose stored rules define no measured demand is priced
    on a contracted capacity, which the bill cannot carry."""
    if any(c["unit"] == "usd_per_kw" for c in components) and not any(
        "demand" in rule["rule_type"] for rule in rules
    ):
        raise _Blocked(
            NEEDS_CONTRACTED_AMOUNT,
            "contracted standby capacity (CL): the schedule's stored rules define no measured demand",
        )


def _check_contract_and_election(components, rules):
    for component in components:
        if component["unit"] in {"percent", "multiplier"}:
            raise _Blocked(
                NEEDS_CONTRACTED_AMOUNT,
                f"amount the {component['unit']} charge '{component['component_type']}' applies to",
            )
    for fragment, blocker in CONTRACT_AMOUNT_RULES:
        if any(fragment in rule["rule_type"] for rule in rules):
            raise _Blocked(NEEDS_CONTRACTED_AMOUNT, blocker)
    for rule in rules:
        if rule["rule_type"] in ELECTION_RULE_TYPES:
            raise _Blocked(NEEDS_ELECTION_DATA, ELECTION_RULE_TYPES[rule["rule_type"]])


def _covering_version(conn, code, bill, identity):
    try:
        return resolve_period_version(
            conn,
            code,
            bill["bill_from"],
            bill["bill_to"],
            state=identity["state"],
            utility=identity["utility"],
        )
    except ValueError as error:
        message = str(error)
        if "found 0" in message or "not fully covered" in message:
            return None
        raise RepricingError(f"Version resolution for {code!r} failed: {message}") from error


def _version_for(conn, code, bill, identity, pins):
    """The period's covering version, or the pinned version when one is given."""
    if not pins or code not in pins:
        return _covering_version(conn, code, bill, identity)
    cursor = conn.execute(
        """
        SELECT id, state, utility, schedule_code, version,
               effective_from, effective_to, status
        FROM tariff_versions WHERE id = ?
        """,
        (pins[code],),
    )
    row = cursor.fetchone()
    if row is None or row[3] != code:
        raise RepricingError(f"Pinned version {pins[code]} is not a version of {code!r}.")
    return dict(zip([column[0] for column in cursor.description], row))


def _base_schedules(conn, identity):
    cursor = conn.execute(
        """
        SELECT v.schedule_code, MIN(v.id) AS first_id
        FROM tariff_versions v
        WHERE v.state = ? AND v.utility = ? AND v.status = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM tariff_versions v2
              JOIN tariff_components c ON c.tariff_version_id = v2.id
              WHERE v2.schedule_code = v.schedule_code
                AND v2.state = v.state AND v2.utility = v.utility
                AND c.billing_category IS NOT NULL
          )
        GROUP BY v.schedule_code
        ORDER BY first_id
        """,
        (identity["state"], identity["utility"]),
    )
    return [row[0] for row in cursor.fetchall()]


def _billed_rider_codes(conn, bill, identity):
    cursor = conn.execute(
        """
        SELECT DISTINCT v.schedule_code
        FROM tariff_versions v JOIN tariff_components c ON c.tariff_version_id = v.id
        WHERE v.state = ? AND v.utility = ? AND v.status = 'active'
          AND c.billing_category = ?
        ORDER BY v.schedule_code
        """,
        (identity["state"], identity["utility"], bill["schedule_code"]),
    )
    return [row[0] for row in cursor.fetchall()]


def _blocked_candidate(code, version, error, selection):
    return {
        "schedule_code": code,
        "status": "blocked",
        "reason": error.reason,
        "blocker": error.blocker,
        "version": version,
        "selection": selection,
    }


def _version_summary(version):
    return {
        "id": version["id"],
        "version": version["version"],
        "effective_from": str(version["effective_from"]),
    }


def _engine(call, what):
    try:
        return call()
    except ValueError as error:
        raise RepricingError(f"Engine failed unexpectedly on {what}: {error}") from error


def _price_candidate(conn, bill, history, identity, code, rider_codes, pins=None):
    selection = {}
    version = None
    base_version = _version_for(conn, code, bill, identity, pins)
    if base_version is None:
        return _blocked_candidate(
            code,
            None,
            _Blocked(
                NO_COVERING_VERSION,
                f"no active version of {code} covers {bill['bill_from']} through {bill['bill_to']}",
            ),
            selection,
        )
    version = _version_summary(base_version)
    try:
        components = _components_for_version(conn, base_version["id"])
        rules = _rules_for_version(conn, base_version["id"])
        if not components:
            raise _Blocked(CROSS_REFERENCE_ONLY, "the version stores rules only, no priced components")

        riders = []
        riders_excluded = []
        for rider_code in rider_codes:
            rider_version = _version_for(conn, rider_code, bill, identity, pins)
            if rider_version is None:
                raise _Blocked(
                    NO_COVERING_VERSION,
                    f"no active version of {rider_code} covers {bill['bill_from']} through {bill['bill_to']}",
                )
            rows = [
                row
                for row in _components_for_version(conn, rider_version["id"])
                if row["billing_category"] == code
            ]
            rider_rules = _rules_for_version(conn, rider_version["id"])
            if any(rule["rule_type"] == "contribution_options" for rule in rider_rules):
                riders_excluded.append(
                    {"rider": rider_code, "reason": "optional contribution; no customer election is stored"}
                )
                continue
            if not rows:
                riders_excluded.append(
                    {"rider": rider_code, "reason": f"no rate row for schedule {code}"}
                )
                continue
            riders.append({"code": rider_code, "version": rider_version, "rows": rows})

        billing_class, class_record = _select_class(components, rules, bill, history)
        if class_record:
            selection["billing_class"] = class_record
            components = [c for c in components if c["billing_class"] == billing_class]

        meter_record = _select_meter_variant(components, rules, bill, history)
        excluded_components = []
        if meter_record:
            selection["meter_variant"] = meter_record
            excluded_components = [
                c for c in components if c["variant"] in meter_record["excluded"]
            ]
            components = [c for c in components if c["variant"] not in meter_record["excluded"]]
            if meter_record["selected"] == "unmetered" and _fixed_text(rules, "unmetered_estimation"):
                if "unmetered_kwh_estimate" not in bill["determinants"]:
                    raise _Blocked(MISSING_DETERMINANT, "unmetered_kwh_estimate (connected load x hours)")
        voltage_record = _select_voltage_variant(components, bill)
        if voltage_record:
            selection["voltage_variant"] = voltage_record
            components = [
                c for c in components if c["variant"] not in voltage_record["excluded"]
            ]

        _check_contracted_capacity_basis(components, rules)
        rider_rows = [row for rider in riders for row in rider["rows"]]
        _check_determinants(components + rider_rows, bill["determinants"])
        if any(c["season"] is not None for c in components):
            try:
                _season_for_month(conn, base_version["id"], bill["bill_to"])
            except ValueError as error:
                raise _Blocked(
                    MISSING_DETERMINANT,
                    f"billing-month season rule for month {_date(bill['bill_to']).month}",
                ) from error
        _check_contract_and_election(components, rules)
    except _Blocked as blocked:
        return _blocked_candidate(code, version, blocked, selection)

    if meter_record and excluded_components:
        if all(c["unit"] in FIXED_MONTHLY_UNITS for c in excluded_components):
            amounts = _engine(
                lambda: evaluate_schedule_components(
                    conn, excluded_components, bill["determinants"], bill["bill_to"],
                    billing_class=billing_class, version_id=base_version["id"],
                ),
                f"excluded variants of {code}",
            )
            meter_record["excluded_amount"] = sum((a["amount"] for a in amounts), Decimal(0))
        else:
            meter_record["excluded_amount"] = None

    schedule_lines = _engine(
        lambda: evaluate_schedule_components(
            conn, components, bill["determinants"], bill["bill_to"],
            billing_class=billing_class, version_id=base_version["id"],
        ),
        f"schedule {code}",
    )
    rider_lines = []
    for rider in riders:
        lines = _engine(
            lambda: evaluate_schedule_components(
                conn, rider["rows"], bill["determinants"], bill["bill_to"],
                version_id=rider["version"]["id"],
            ),
            f"rider {rider['code']} for schedule {code}",
        )
        rider_lines.extend({"rider": rider["code"], **line} for line in lines)
    schedule_subtotal = sum((line["amount"] for line in schedule_lines), Decimal(0))
    rider_subtotal = sum((line["amount"] for line in rider_lines), Decimal(0))
    notes = []
    minimum = _fixed_text(rules, "minimum_charge")
    if minimum:
        notes.append(f"minimum charge not evaluated: {minimum}")
    return {
        "schedule_code": code,
        "status": "computed",
        "reason": None,
        "blocker": None,
        "version": version,
        "selection": selection,
        "schedule_lines": schedule_lines,
        "schedule_subtotal": round_money(schedule_subtotal),
        "rider_lines": rider_lines,
        "rider_subtotal": round_money(rider_subtotal),
        "total": round_money(schedule_subtotal + rider_subtotal),
        "riders_applied": [
            {"rider": r["code"], "version_id": r["version"]["id"], "version": r["version"]["version"]}
            for r in riders
        ],
        "riders_excluded": riders_excluded,
        "notes": notes,
    }


def price_schedule(conn, bill, history, identity, code, pins=None):
    """One candidate for one schedule; `pins` maps a schedule or rider code to a version id."""
    if not bill.get("schedule_code"):
        raise RepricingError(f"Bill {bill.get('id')} has no stored schedule code.")
    rider_codes = _billed_rider_codes(conn, bill, identity)
    return _price_candidate(conn, bill, history, identity, code, rider_codes, pins)


def reprice(conn, bill, history, identity):
    """One candidate per base schedule for an already-loaded bill."""
    if not bill.get("schedule_code"):
        raise RepricingError(f"Bill {bill.get('id')} has no stored schedule code.")
    rider_codes = _billed_rider_codes(conn, bill, identity)
    return {
        "bill_id": bill["id"],
        "account_number": bill["account_number"],
        "billed_schedule_code": bill["schedule_code"],
        "bill_from": str(bill["bill_from"]),
        "bill_to": str(bill["bill_to"]),
        "determinants": dict(bill["determinants"]),
        "candidates": [
            _price_candidate(conn, bill, history, identity, code, rider_codes)
            for code in _base_schedules(conn, identity)
        ],
    }


def reprice_bill(conn, bill_id, mapping_path=DEFAULT_MAPPING_PATH):
    """Read-only: reprice a stored bill under every base schedule."""
    mapping, _ = load_mapping(mapping_path)
    identity = mapping["tariff_identity"]
    bill = _fetch_bill(conn, bill_id)
    return reprice(conn, bill, _history(conn, bill), identity)


class ReviewRefused(ValueError):
    """A review action is not allowed for the stored state."""


DECISIONS = ("works", "doesnt_work")


def _require_bill(conn, bill_id):
    if conn.execute("SELECT 1 FROM bills WHERE id = ?", (bill_id,)).fetchone() is None:
        raise ReviewRefused(f"Bill {bill_id} does not exist.")


def _decision_rows(conn, bill_id):
    cursor = conn.execute(
        """
        SELECT id, bill_id, schedule_code, decision, reviewer, note, decided_at, selected
        FROM schedule_review_decisions WHERE bill_id = ? ORDER BY id
        """,
        (bill_id,),
    )
    return {row["schedule_code"]: row for row in _row_dicts(cursor)}


def record_decision(conn, bill_id, schedule_code, decision, reviewer, note=None):
    """Store a human works/doesnt_work decision; computed numbers are never touched."""
    if decision not in DECISIONS:
        raise ReviewRefused(f"Decision must be one of {list(DECISIONS)}.")
    if not str(reviewer or "").strip():
        raise ReviewRefused("A reviewer is required.")
    _require_bill(conn, bill_id)
    identity = load_mapping(DEFAULT_MAPPING_PATH)[0]["tariff_identity"]
    if schedule_code not in _base_schedules(conn, identity):
        raise ReviewRefused(f"{schedule_code!r} is not a base schedule.")
    existing = _decision_rows(conn, bill_id).get(schedule_code)
    reviewer = str(reviewer).strip()
    if existing is None:
        conn.execute(
            "INSERT INTO schedule_review_decisions "
            "(bill_id, schedule_code, decision, reviewer, note, selected) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (bill_id, schedule_code, decision, reviewer, note),
        )
    else:
        selected = existing["selected"] if decision == "works" else 0
        conn.execute(
            "UPDATE schedule_review_decisions SET decision = ?, reviewer = ?, note = ?, "
            "decided_at = CURRENT_TIMESTAMP, selected = ? WHERE id = ?",
            (decision, reviewer, note, selected, existing["id"]),
        )
    conn.commit()
    return _decision_rows(conn, bill_id)[schedule_code]


def select_schedule(conn, bill_id, schedule_code):
    """Mark one computed, not-rejected candidate as the bill's selected schedule."""
    _require_bill(conn, bill_id)
    candidates = {c["schedule_code"]: c for c in reprice_bill(conn, bill_id)["candidates"]}
    candidate = candidates.get(schedule_code)
    if candidate is None:
        raise ReviewRefused(f"{schedule_code!r} is not a base schedule.")
    if candidate["status"] != "computed":
        raise ReviewRefused(
            f"Schedule {schedule_code} cannot be selected: {candidate['reason']} "
            f"({candidate['blocker']})."
        )
    decision = _decision_rows(conn, bill_id).get(schedule_code)
    if decision is None:
        raise ReviewRefused(
            f"Schedule {schedule_code} needs a 'works' decision before it can be selected."
        )
    if decision["decision"] == "doesnt_work":
        raise ReviewRefused(f"Schedule {schedule_code} is marked doesnt_work and cannot be selected.")
    conn.execute(
        "UPDATE schedule_review_decisions SET selected = 0 WHERE bill_id = ?", (bill_id,)
    )
    conn.execute("UPDATE schedule_review_decisions SET selected = 1 WHERE id = ?", (decision["id"],))
    conn.commit()
    return _decision_rows(conn, bill_id)[schedule_code]


def get_review_state(conn, bill_id):
    """Freshly repriced candidates joined with the stored human decisions."""
    _require_bill(conn, bill_id)
    result = reprice_bill(conn, bill_id)
    decisions = _decision_rows(conn, bill_id)
    for candidate in result["candidates"]:
        row = decisions.get(candidate["schedule_code"])
        candidate["decision"] = row
        candidate["selected"] = bool(row and row["selected"])
    result["selected_schedule"] = next(
        (c["schedule_code"] for c in result["candidates"] if c["selected"]), None
    )
    return result


def _cell(value):
    return "" if value is None else str(value)


def format_table(result):
    rows = [
        "| Schedule | Result | Schedule | Riders | Total | Selection / blocker |",
        "|---|---|---|---|---|---|",
    ]
    for candidate in result["candidates"]:
        selection = candidate["selection"]
        notes = []
        if "billing_class" in selection:
            notes.append(f"class={selection['billing_class']['selected']}")
        if "meter_variant" in selection:
            meter = selection["meter_variant"]
            notes.append(
                f"{meter['selected']} variant; excluded {', '.join(meter['excluded'])}"
                f" ${meter.get('excluded_amount')}"
            )
        if candidate["status"] == "computed":
            rows.append(
                f"| {candidate['schedule_code']} | computed | {candidate['schedule_subtotal']} "
                f"| {candidate['rider_subtotal']} | {candidate['total']} | {'; '.join(notes)} |"
            )
        else:
            rows.append(
                f"| {candidate['schedule_code']} | {candidate['reason']} | | | "
                f"| {candidate['blocker']} |"
            )
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reprice a bill under every base schedule (read-only).")
    parser.add_argument("--bill", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"), readonly=True)
        try:
            result = reprice_bill(conn, args.bill)
        finally:
            conn.close()
    except (ValueError, RepricingError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Bill {result['bill_id']} (billed schedule {result['billed_schedule_code']}, "
          f"{result['bill_from']} to {result['bill_to']})")
    print(format_table(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
