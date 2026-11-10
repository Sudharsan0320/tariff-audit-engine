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
        _base_is_residential,
        _components_for_version,
        _filter_optional_provisions,
        _fetch_bill,
        _minimum_charge_status,
        _rider_components_for_schedule,
        _rider_decisions,
        _rules_for_version,
        _structured_rules_for_versions,
        load_mapping,
    )
    from .billing_engine import (
        DETERMINANT_COMPONENT_TYPES,
        DETERMINANT_VARIANTS,
        apply_structured_demand_rules,
        _select_representations,
        _season_for_month,
        evaluate_schedule_components,
        resolve_period_version,
        round_money,
    )
    from .repricing_reasons import candidate_reasons
else:
    from audit_bills import (
        DEFAULT_MAPPING_PATH,
        _base_is_residential,
        _components_for_version,
        _filter_optional_provisions,
        _fetch_bill,
        _minimum_charge_status,
        _rider_components_for_schedule,
        _rider_decisions,
        _rules_for_version,
        _structured_rules_for_versions,
        load_mapping,
    )
    from billing_engine import (
        DETERMINANT_COMPONENT_TYPES,
        DETERMINANT_VARIANTS,
        apply_structured_demand_rules,
        _select_representations,
        _season_for_month,
        evaluate_schedule_components,
        resolve_period_version,
        round_money,
    )
    from repricing_reasons import candidate_reasons


ROOT = Path(__file__).resolve().parents[1]

CROSS_REFERENCE_ONLY = "cross_reference_only"
NO_COVERING_VERSION = "no_covering_version"
MISSING_DETERMINANT = "missing_determinant"
NEEDS_ELECTION_DATA = "needs_election_data"
NEEDS_CONTRACTED_AMOUNT = "needs_contracted_amount"
UNCLASSIFIED_SCHEDULE_KIND = "unclassified_schedule_kind"
ELIGIBILITY_MISMATCH = "eligibility_mismatch"
REASONS = (
    CROSS_REFERENCE_ONLY,
    NO_COVERING_VERSION,
    MISSING_DETERMINANT,
    NEEDS_ELECTION_DATA,
    NEEDS_CONTRACTED_AMOUNT,
    UNCLASSIFIED_SCHEDULE_KIND,
    ELIGIBILITY_MISMATCH,
)

FIXED_MONTHLY_UNITS = {"usd_per_month", "usd_per_unit_month"}
VOLTAGE_VARIANTS = {"primary", "secondary"}
METER_VARIANTS = {"metered", "unmetered"}
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


def _select_voltage_category(components, bill):
    def category_voltage(component):
        match = re.search(
            r"\b(primary|secondary)\b",
            str(component.get("billing_category") or ""),
            re.IGNORECASE,
        )
        return match.group(1).casefold() if match else None

    available = {
        category_voltage(component)
        for component in components
        if category_voltage(component) is not None
    }
    if not available:
        return None, components
    voltage = str(bill.get("voltage") or "").strip().casefold()
    if voltage not in VOLTAGE_VARIANTS:
        raise _Blocked(
            MISSING_DETERMINANT,
            "service voltage class matching the primary/secondary billing categories",
        )
    if voltage not in available:
        raise _Blocked(
            ELIGIBILITY_MISMATCH,
            f"stored rate categories do not include the account's {voltage} voltage class",
        )
    selected = [
        component
        for component in components
        if category_voltage(component) is None or category_voltage(component) == voltage
    ]
    return (
        {
            "account_voltage": bill["voltage"],
            "selected": voltage,
            "excluded": sorted(available - {voltage}),
        },
        selected,
    )


def _requirements(component):
    unit = component["unit"]
    variant = component["variant"]
    if component.get("formula") is not None:
        raise RepricingError(f"Formula component {component['id']} has no documented evaluator.")
    if unit == "usd_per_kwh":
        names = ["demand"] if component.get("tier_unit") == "kwh_per_kw" else []
        names.append(
            component.get("determinant_type")
            or DETERMINANT_VARIANTS.get(variant)
            or DETERMINANT_COMPONENT_TYPES.get(
                component.get("component_type"), "total_consumption"
            )
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


def _set_category_determinants(components):
    for component in components:
        if component["unit"] != "usd_per_kwh" or component.get("determinant_type"):
            continue
        category = str(component.get("billing_category") or "")
        if re.search(r"\bcritical[-\s]+on[-\s]?peak\b", category, re.IGNORECASE):
            component["determinant_type"] = "critical_on_peak_usage"
        elif re.search(r"\bon[-\s]peak\b", category, re.IGNORECASE):
            component["determinant_type"] = "on_peak_usage"
        elif re.search(r"\boff[-\s]peak\b", category, re.IGNORECASE):
            component["determinant_type"] = "off_peak_usage"
    return components


def _check_alternative_tod_provision(components, rules):
    rule_text = " ".join(rule["rule_text"] for rule in rules).casefold()
    if "time-of-day provision" not in rule_text:
        return
    has_tod_energy = any(
        component["unit"] == "usd_per_kwh"
        and re.search(r"\b(?:tod|time[-\s]of[-\s]day)\b", str(component.get("billing_category") or ""), re.IGNORECASE)
        for component in components
    )
    has_standard_energy = any(
        component["unit"] == "usd_per_kwh"
        and not re.search(r"\b(?:tod|time[-\s]of[-\s]day|unmetered)\b", str(component.get("billing_category") or ""), re.IGNORECASE)
        for component in components
    )
    if has_tod_energy and has_standard_energy:
        raise _Blocked(
            NEEDS_ELECTION_DATA,
            "selection of the stored load-management time-of-day provision",
        )


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


def _identity_for_bill(conn, bill, preferred):
    rows = conn.execute(
        """
        SELECT DISTINCT state, utility
        FROM tariff_versions
        WHERE schedule_code = ? AND status IN ('active', 'superseded')
          AND effective_from <= ?
          AND (effective_to IS NULL OR effective_to > ?)
        """,
        (bill["schedule_code"], bill["bill_from"], bill["bill_to"]),
    ).fetchall()
    identities = {(row[0], row[1]) for row in rows}
    preferred_identity = (preferred["state"], preferred["utility"])
    if preferred_identity in identities or not identities:
        return preferred
    if len(identities) == 1:
        state, utility = next(iter(identities))
        return {"state": state, "utility": utility}
    raise RepricingError(
        f"Bill {bill['id']} schedule {bill['schedule_code']!r} maps to multiple "
        "utilities for its effective period."
    )


def _base_schedules(conn, identity, bill):
    cursor = conn.execute(
        """
        SELECT v.schedule_code, MIN(v.id) AS first_id
        FROM tariff_versions v
        WHERE v.state = ? AND v.utility = ? AND v.status = 'active'
          AND v.schedule_kind = 'schedule'
          AND v.effective_from <= ?
          AND (v.effective_to IS NULL OR v.effective_to > ?)
        GROUP BY v.schedule_code
        ORDER BY first_id
        """,
        (
            identity["state"],
            identity["utility"],
            bill["bill_from"],
            bill["bill_to"],
        ),
    )
    return [row[0] for row in cursor.fetchall()]


def _schedule_inventory(conn, identity, bill):
    cursor = conn.execute(
        """
        SELECT DISTINCT schedule_code, schedule_kind
        FROM tariff_versions v
        WHERE v.state = ? AND v.utility = ? AND v.status = 'active'
          AND v.effective_from <= ?
          AND (v.effective_to IS NULL OR v.effective_to > ?)
        ORDER BY schedule_code
        """,
        (identity["state"], identity["utility"], bill["bill_from"], bill["bill_to"]),
    )
    return [
        {
            "schedule_code": row[0],
            "schedule_kind": row[1],
            "reason": (
                "No stored printed/producing-path schedule kind; not priced."
                if row[1] is None
                else None
            ),
        }
        for row in cursor.fetchall()
    ]


def _blocked_candidate(code, version, error, selection, details=None):
    candidate = {
        "schedule_code": code,
        "status": "blocked",
        "reason": error.reason,
        "blocker": error.blocker,
        "version": version,
        "selection": selection,
    }
    if details:
        candidate.update(details)
    return candidate


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


def _eligibility_assessment(bill, rules):
    eligibility_rules = [rule["rule_text"] for rule in rules if rule["rule_type"] == "eligibility"]
    if not eligibility_rules:
        return {
            "status": "not_established",
            "reason": "No stored eligibility rule establishes customer-class applicability.",
        }
    text = " ".join(eligibility_rules).casefold()
    customer_class = str(bill.get("customer_class") or "").casefold()
    if "residential" in text and "commercial" in customer_class:
        raise _Blocked(ELIGIBILITY_MISMATCH, "stored eligibility is residential; the bill account is commercial")
    if "commercial" in text and "residential" in customer_class:
        raise _Blocked(ELIGIBILITY_MISMATCH, "stored eligibility is commercial; the bill account is residential")
    if "ami meter" in text or "charging station" in text:
        return {
            "status": "not_established",
            "reason": "Stored eligibility requires AMI-meter or vehicle-charging evidence that the account record does not contain.",
        }
    if "employee" in text:
        return {
            "status": "not_established",
            "reason": "Stored eligibility is limited to company employees; the account record does not establish employee status.",
        }
    if "residential" in customer_class and "residential" in text:
        return {"status": "established", "reason": "Stored eligibility includes residential service."}
    if any(term in customer_class for term in ("commercial", "industrial")) and any(
        term in text for term in ("commercial", "industrial")
    ):
        return {"status": "established", "reason": "Stored eligibility includes this customer class."}
    return {
        "status": "not_established",
        "reason": "Stored eligibility text does not establish this account's customer class.",
    }


def _structured_demand_inputs(components, rules, determinants):
    for rule in rules:
        if rule["rule_type"] != "demand_determination":
            continue
        import json

        parameters = json.loads(rule["parameters_json"])
        if parameters.get("evaluator") != "sbs_demand_v1":
            continue
        generation = parameters["generation_demand"]
        contract = parameters["contract_capacity_demand"]
        if any(
            c.get("component_type") == generation["component_type"]
            and c.get("variant") == generation["variant"]
            for c in components
        ):
            for name in (generation["measured_field"], generation["reservation_field"]):
                if name not in determinants:
                    raise _Blocked(MISSING_DETERMINANT, name)
        if any(
            c.get("component_type") in contract["component_types"]
            and c.get("variant") == contract["variant"]
            for c in components
        ) and contract["capacity_field"] not in determinants:
            raise _Blocked(MISSING_DETERMINANT, contract["capacity_field"])


def _price_candidate(conn, bill, history, identity, code, mapping, pins=None):
    bill = {**bill, "determinants": dict(bill["determinants"])}
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
    rider_applicability = []
    try:
        rules = _rules_for_version(conn, base_version["id"])
        components = _filter_optional_provisions(
            _components_for_version(conn, base_version["id"]), rules, mapping
        )
        structured_rules = _structured_rules_for_versions(conn, [base_version["id"]])
        structured_minimum_rules = [
            rule for rule in structured_rules if rule["rule_type"] == "minimum_charge"
        ]
        selection["minimum_charge"] = {
            "status": "not_evaluated",
            "reason": "The candidate did not reach minimum-charge evaluation.",
            **(
                {"source_page": structured_minimum_rules[0]["source_page"]}
                if structured_minimum_rules
                else {}
            ),
        }
        selection["structured_demand_rules"] = [
            {
                "rule_type": rule["rule_type"],
                "source_page": rule["source_page"],
                "status": "not_evaluated",
            }
            for rule in structured_rules
            if rule["rule_type"] == "demand_determination"
        ]
        riders = []
        riders_excluded = []
        included_riders, rider_applicability = _rider_decisions(
            conn, bill, base_version, mapping
        )
        billed_included_riders = included_riders
        billed_rider_applicability = rider_applicability
        if bill["schedule_code"] != code:
            billed_base_version = _version_for(
                conn, bill["schedule_code"], bill, identity, pins
            )
            if billed_base_version is None:
                billed_included_riders = []
                billed_rider_applicability = []
            else:
                billed_included_riders, billed_rider_applicability = _rider_decisions(
                    conn, bill, billed_base_version, mapping
                )
        billed_rider_codes = {
            rider["schedule_code"] for rider, _, _ in billed_included_riders
        }
        if not components:
            raise _Blocked(CROSS_REFERENCE_ONLY, "the version stores rules only, no priced components")
        eligibility = _eligibility_assessment(bill, rules)
        selection["eligibility"] = eligibility
        if pins:
            included_riders = [
                (
                    _version_for(conn, rider["schedule_code"], bill, identity, pins),
                    _components_for_version(
                        conn,
                        pins[rider["schedule_code"]],
                    ),
                    _rules_for_version(
                        conn,
                        pins[rider["schedule_code"]],
                    ),
                )
                if rider["schedule_code"] in pins
                else (rider, rider_components, rider_rules)
                for rider, rider_components, rider_rules in included_riders
            ]
        base_is_residential = _base_is_residential(conn, base_version)
        for rider_version, rider_components, rider_rules in included_riders:
            rider_code = rider_version["schedule_code"]
            if rider_code not in billed_rider_codes:
                continue
            if any(rule["rule_type"] == "contribution_options" for rule in rider_rules):
                continue
            rows = _rider_components_for_schedule(
                rider_components,
                code,
                base_is_residential,
                rider_rules,
                mapping,
            )
            if not rows:
                continue
            riders.append({"code": rider_code, "version": rider_version, "rows": rows})
        for decision in billed_rider_applicability:
            rider_code = decision["schedule_code"]
            rider_version = _version_for(conn, rider_code, bill, identity, pins)
            if rider_version is None:
                continue
            rider_rules = _rules_for_version(conn, rider_version["id"])
            if any(rule["rule_type"] == "contribution_options" for rule in rider_rules) and any(
                row["billing_category"] == bill["schedule_code"]
                for row in _components_for_version(conn, rider_version["id"])
            ):
                riders_excluded.append({
                    "rider": rider_code,
                    "reason": "optional contribution; no customer election is stored",
                })
        _check_alternative_tod_provision(components, rules)
        structured_demand_rules = [
            rule for rule in structured_rules if rule["rule_type"] == "demand_determination"
        ]
        if structured_demand_rules:
            _structured_demand_inputs(components, structured_demand_rules, bill["determinants"])
            try:
                bill["determinants"] = apply_structured_demand_rules(
                    components, bill["determinants"], bill["bill_to"], structured_demand_rules
                )
            except ValueError as error:
                raise RepricingError(f"Structured demand rule for {code} failed: {error}") from error
            selection["structured_demand_rules"] = [
                {"rule_type": r["rule_type"], "source_page": r["source_page"], "status": "evaluated"}
                for r in structured_demand_rules
            ]

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

        components = _select_representations(components)
        components = _set_category_determinants(components)
        _check_contracted_capacity_basis(components, rules)
        rider_rows = [row for rider in riders for row in rider["rows"]]
        rider_rows = _set_category_determinants(rider_rows)
        _check_determinants(components + rider_rows, bill["determinants"])
        voltage_category_record, components = _select_voltage_category(
            components, bill
        )
        rider_voltage_record, rider_rows = _select_voltage_category(
            rider_rows, bill
        )
        if voltage_category_record or rider_voltage_record:
            records = [
                record
                for record in (voltage_category_record, rider_voltage_record)
                if record is not None
            ]
            selection["voltage_category"] = {
                "account_voltage": bill["voltage"],
                "selected": records[0]["selected"],
                "excluded": sorted(
                    {
                        voltage
                        for record in records
                        for voltage in record["excluded"]
                    }
                ),
            }
        selected_rider_rows = {id(row) for row in rider_rows}
        for rider in riders:
            rider["rows"] = [
                row for row in rider["rows"] if id(row) in selected_rider_rows
            ]
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
        return _blocked_candidate(
            code, version, blocked, selection,
            {"rider_applicability": rider_applicability},
        )

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
    schedule_subtotal = sum((line["amount"] for line in schedule_lines), Decimal(0))
    minimum_status = _minimum_charge_status(
        rules,
        structured_minimum_rules[0] if structured_minimum_rules else None,
        schedule_subtotal if structured_minimum_rules else None,
    )
    if minimum_status["status"] == "evaluated":
        floor_total = Decimal(minimum_status["post_floor_subtotal"])
        if minimum_status["bound"]:
            schedule_lines.append({
                "component_type": "minimum_charge_adjustment",
                "amount": floor_total - round_money(schedule_subtotal),
                "source_page": minimum_status["source_page"],
            })
        schedule_subtotal = floor_total
    selection["minimum_charge"] = minimum_status
    notes = []
    minimum_text = _fixed_text(rules, "minimum_charge")
    if minimum_text and minimum_status["status"] != "evaluated":
        notes.append(f"minimum charge not evaluated: {minimum_text}")
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
    rider_subtotal = sum((line["amount"] for line in rider_lines), Decimal(0))
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
        "rider_applicability": rider_applicability,
        "notes": notes,
    }


def price_schedule(conn, bill, history, identity, code, pins=None):
    """One candidate for one schedule; `pins` maps a schedule or rider code to a version id.

    A computed candidate carries the same difference and reasons `reprice` attaches,
    measured against the bill's own schedule priced without pins.
    """
    if not bill.get("schedule_code"):
        raise RepricingError(f"Bill {bill.get('id')} has no stored schedule code.")
    mapping = load_mapping(DEFAULT_MAPPING_PATH)[0]
    candidate = _price_candidate(conn, bill, history, identity, code, mapping, pins)
    if candidate.get("status") != "computed":
        return candidate
    if code == bill["schedule_code"]:
        priced = [candidate]
    else:
        own = _price_candidate(conn, bill, history, identity, bill["schedule_code"], mapping)
        priced = [own, candidate]
    _attach_reasons(bill, priced)
    return candidate


def _attach_reasons(bill, candidates):
    """Difference and reasons against the bill's own computed schedule.

    Blocked candidates keep their status and named reason and receive neither
    a difference nor a reason. A computed candidate whose own schedule is not
    computed gets no invented comparison.
    """
    own = next((c for c in candidates if c["schedule_code"] == bill["schedule_code"]), None)
    own_ready = own is not None and own.get("status") == "computed"
    for candidate in candidates:
        if candidate.get("status") != "computed":
            continue
        if not own_ready:
            candidate["difference"] = None
            candidate["reasons"] = []
            continue
        candidate["difference"] = _amount_difference(candidate["total"], own["total"])
        candidate["reasons"] = candidate_reasons(own, candidate)
    return candidates


def _amount_difference(candidate_total, own_total):
    return Decimal(str(candidate_total)) - Decimal(str(own_total))


def reprice(conn, bill, history, identity, mapping=None):
    """One candidate per base schedule for an already-loaded bill."""
    if not bill.get("schedule_code"):
        raise RepricingError(f"Bill {bill.get('id')} has no stored schedule code.")
    mapping = mapping or load_mapping(DEFAULT_MAPPING_PATH)[0]
    candidates = [
        _price_candidate(conn, bill, history, identity, code, mapping)
        for code in _base_schedules(conn, identity, bill)
    ]
    return {
        "bill_id": bill["id"],
        "account_number": bill["account_number"],
        "billed_schedule_code": bill["schedule_code"],
        "bill_from": str(bill["bill_from"]),
        "bill_to": str(bill["bill_to"]),
        "determinants": dict(bill["determinants"]),
        "candidates": _attach_reasons(bill, candidates),
        "schedule_inventory": _schedule_inventory(conn, identity, bill),
    }


def reprice_bill(conn, bill_id, mapping_path=DEFAULT_MAPPING_PATH):
    """Read-only: reprice a stored bill under every base schedule."""
    mapping, _ = load_mapping(mapping_path)
    bill = _fetch_bill(conn, bill_id)
    identity = _identity_for_bill(conn, bill, mapping["tariff_identity"])
    return reprice(conn, bill, _history(conn, bill), identity, mapping)


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
    bill = _fetch_bill(conn, bill_id)
    if schedule_code not in _base_schedules(conn, identity, bill):
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
        if selection.get("eligibility", {}).get("status") == "not_established":
            notes.append("eligibility not established")
        minimum = selection.get("minimum_charge", {})
        if minimum.get("status") == "evaluated":
            notes.append(
                f"minimum ${minimum['floor']}; "
                f"{'binding' if minimum['bound'] else 'not binding'}"
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
