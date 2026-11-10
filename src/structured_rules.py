"""Stage 12d: structured rules as a pipeline output, derived only from stored verified tariff_rules.

Input is one version's stored `tariff_rules` rows. Output is `structured_tariff_rules` rows in the
11C shape (rule_type, applies-to, parameters JSON, original text, source_page, citation_status).
Each supported type has ONE deterministic pattern over the stored text. A rule is structured only
when its whole text matches the pattern and every parameter is determined; anything else is queued
as `structured_rule_unparsed` and nothing is guessed or partially filled. No model, no PDF.

Two parameters come from other stored, verified rows of the same version rather than from the rule
text: the minimum-charge `value` (the stored basic_customer_charge rate, since the text only says
"equal to the Basic Service Charge") and the SBS demand component types (the stored usd_per_kw
components by variant). If those rows are missing or ambiguous the rule is queued.
"""
import json
import re
from decimal import Decimal

try:
    from src import parse_review
    from src.billing_engine import EVALUATOR_FIELDS
except ImportError:
    import parse_review
    from billing_engine import EVALUATOR_FIELDS

SIGNAL = "structured_rule_unparsed"
QUEUE_KIND = "rule"
QUEUE_FIELD = "structured_rule"

# stored tariff_rules.rule_type -> structured rule_type (11C vocabulary)
TYPE_MAP = {
    "eligibility": "eligibility",
    "minimum_charge": "minimum_charge",
    "closed_schedule": "provision_availability",
    "on_peak_hours": "on_peak_hours",
    "off_peak_hours": "off_peak_hours",
    "demand_determination": "demand_determination",
}

TIME = r"(\d{1,2}) ?(a\.m\.|p\.m\.)"
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
# Fixed legal-holiday definitions, the same shapes the evaluator's _holiday_date reads.
HOLIDAYS = {
    "New Year's Day": {"day": 1, "month": 1, "name": "New Year's Day"},
    "Memorial Day": {"month": 5, "name": "Memorial Day", "occurrence": -1, "weekday": 0},
    "Independence Day": {"day": 4, "month": 7, "name": "Independence Day"},
    "Labor Day": {"month": 9, "name": "Labor Day", "occurrence": 1, "weekday": 0},
    "Thanksgiving Day": {"month": 11, "name": "Thanksgiving Day", "occurrence": 4, "weekday": 3},
    "Christmas Day": {"day": 25, "month": 12, "name": "Christmas Day"},
}

PATTERNS = {
    "eligibility": re.compile(
        r"Available for electric service through one meter to individual (residential) customers, "
        r"including rural residential customers engaged principally in agricultural pursuits, "
        r"who take Standard Service from the Company\."),
    "minimum_charge": re.compile(
        r"(?:The schedule is )?[Ss]ubject to a minimum monthly charge equal to the Basic Service Charge\."),
    "provision_availability": re.compile(
        r"The (.+?) provision is no longer available to new customers; existing customers may remain\."),
    "on_peak_hours": re.compile(
        rf"The on-peak billing period is {TIME} to {TIME}, local time, for all weekdays, Monday through Friday\."),
    "off_peak_hours": re.compile(
        rf"The off-peak billing period is {TIME} to {TIME}, local time, (?:for all weekdays|on weekdays), "
        r"all hours (?:of the day )?on Saturdays and Sundays, and the legally observed holidays of (.+)\."),
    "demand_determination": re.compile(
        r"Generation demand is the highest on-peak demand in excess of the supplemental service "
        r"capacity reservation in ((?:[A-Z][a-z]{2}-[A-Z][a-z]{2})(?: and [A-Z][a-z]{2}-[A-Z][a-z]{2})*); "
        r"transmission and distribution demands equal the standby service contract capacity kW\."),
}


class Unparsed(Exception):
    """The text or its supporting rows do not determine every parameter."""


def _clock(hour, meridiem):
    hour = int(hour)
    if not 1 <= hour <= 12:
        raise Unparsed("hour outside 1-12")
    return (hour % 12) + (12 if meridiem == "p.m." else 0)


def _hhmm(hour, meridiem):
    return f"{_clock(hour, meridiem):02d}:00"


def _months(text):
    months = []
    for start, end in re.findall(r"([A-Z][a-z]{2})-([A-Z][a-z]{2})", text):
        if start not in MONTHS or end not in MONTHS:
            raise Unparsed(f"unknown month in {text!r}")
        month = MONTHS[start]
        while True:
            months.append(month)
            if month == MONTHS[end]:
                break
            month = month % 12 + 1
    return months


def _only_value(components, component_type, unit):
    rates = {Decimal(str(c["rate"])) for c in components
             if c["component_type"] == component_type and c["unit"] == unit and c["rate"] is not None}
    if len(rates) != 1:
        raise Unparsed(f"{len(rates)} distinct stored {component_type} {unit} rates (exactly one required)")
    value = rates.pop()
    return str(value.quantize(Decimal("0.01"))) if value == value.quantize(Decimal("0.01")) else str(value)


def _parameters(kind, match, components, siblings):
    if kind == "eligibility":
        return {"customer_type": f"individual {match.group(1)}", "includes_rural_agricultural_customers": True,
                "single_meter": True, "takes_standard_service": True}
    if kind == "minimum_charge":
        return {"derived_from": "basic_customer_charge", "unit": "usd_per_month",
                "value": _only_value(components, "basic_customer_charge", "usd_per_month")}
    if kind == "provision_availability":
        return {"existing_customers_may_remain": True, "new_customers_allowed": False,
                "provision": match.group(1)}
    if kind == "on_peak_hours":
        start, end = _hhmm(*match.group(1, 2)), _hhmm(*match.group(3, 4))
        if start >= end:
            raise Unparsed("on-peak interval does not start before it ends")
        return {"end_local": end, "period": "on_peak", "start_local": start, "timezone": "local",
                "weekdays": [0, 1, 2, 3, 4]}
    if kind == "off_peak_hours":
        start, end = _hhmm(*match.group(1, 2)), _hhmm(*match.group(3, 4))
        on_peak = siblings.get("on_peak_hours")
        if not on_peak or (start, end) != (on_peak["end_local"], on_peak["start_local"]):
            raise Unparsed("off-peak window is not the complement of a parsed on-peak rule of this version")
        names = [n.strip() for n in re.split(r",\s*(?:and\s+)?|\s+and\s+", match.group(5)) if n.strip()]
        if not names or any(n not in HOLIDAYS for n in names):
            raise Unparsed("holiday list names a holiday with no fixed definition: "
                           + ", ".join(n for n in names if n not in HOLIDAYS))
        return {"holidays": [dict(HOLIDAYS[n]) for n in names], "period": "off_peak", "timezone": "local",
                "weekday_behavior": "complement_of_on_peak", "weekends": "all_day"}
    if kind == "demand_determination":
        usd_kw = [c for c in components if c["unit"] == "usd_per_kw"]
        generation = sorted({c["component_type"] for c in usd_kw if c["variant"] == "generation"})
        contract = sorted({c["component_type"] for c in usd_kw if c["variant"] == "distribution"})
        if len(generation) != 1 or not contract:
            raise Unparsed("stored usd_per_kw components do not give one generation type and a distribution type")
        fields = EVALUATOR_FIELDS["sbs_demand_v1"]
        return {"contract_capacity_demand": {"capacity_field": fields["capacity_field"],
                                             "component_types": contract, "variant": "distribution"},
                "evaluator": "sbs_demand_v1",
                "generation_demand": {"component_type": generation[0],
                                      "measured_field": fields["measured_field"],
                                      "months": _months(match.group(1)),
                                      "reservation_field": fields["reservation_field"],
                                      "variant": "generation"}}
    raise Unparsed("no pattern")


def structure_rules(rules, components):
    """(structured, unparsed) for one version. `rules` / `components` are dict rows; pure, no writes."""
    structured, unparsed, siblings = [], [], {}
    # on-peak is parsed first so the off-peak complement check can use it
    ordered = sorted(rules, key=lambda r: (r["rule_type"] != "on_peak_hours", r["id"]))
    for rule in ordered:
        kind = TYPE_MAP.get(rule["rule_type"])
        text = re.sub(r"\s+", " ", rule["rule_text"] or "").strip()
        if kind is None:
            unparsed.append((rule, f"no structured pattern for rule_type {rule['rule_type']!r}"))
            continue
        match = PATTERNS[kind].fullmatch(text)
        if not match:
            unparsed.append((rule, f"text does not fully match the {kind} pattern"))
            continue
        if rule["citation_status"] != "cited" or rule["source_page"] is None:
            unparsed.append((rule, "parent rule is uncited"))
            continue
        try:
            parameters = _parameters(kind, match, components, siblings)
        except Unparsed as error:
            unparsed.append((rule, str(error)))
            continue
        if any(s["rule_type"] == kind for s in structured):
            unparsed.append((rule, f"a {kind} rule is already structured for this version"))
            continue
        if kind == "on_peak_hours":
            siblings[kind] = parameters
        structured.append({"rule_type": kind, "parameters": parameters, "source_rule_id": rule["id"],
                           "source_rule_text": rule["rule_text"], "source_page": rule["source_page"],
                           "citation_status": rule["citation_status"]})
    return structured, [u for u in sorted(unparsed, key=lambda u: u[0]["id"])]


def _dicts(cursor):
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def structure_version(conn, version_id):
    """Structure one version's stored rules; idempotent. Returns counts and the rows' summary."""
    version = conn.execute("SELECT schedule_code, utility FROM tariff_versions WHERE id = ?",
                           (version_id,)).fetchone()
    if version is None:
        raise ValueError(f"no tariff version {version_id}")
    schedule, utility = version
    rules = _dicts(conn.execute(
        "SELECT id, rule_type, rule_text, source_page, citation_status FROM tariff_rules "
        "WHERE tariff_version_id = ? ORDER BY id", (version_id,)))
    components = _dicts(conn.execute(
        "SELECT component_type, rate, unit, variant FROM tariff_components WHERE tariff_version_id = ?",
        (version_id,)))
    structured, unparsed = structure_rules(rules, components)
    new_structured = new_unparsed = 0
    for row in structured:
        found = conn.execute(
            "SELECT 1 FROM structured_tariff_rules WHERE tariff_version_id = ? AND rule_type = ? "
            "AND applies_to_schedule = ? AND applies_to_version_id = ?",
            (version_id, row["rule_type"], schedule, version_id)).fetchone()
        if found:
            continue
        conn.execute(
            "INSERT INTO structured_tariff_rules(tariff_version_id, rule_type, applies_to_schedule, "
            "applies_to_version_id, parameters_json, source_rule_text, source_page, citation_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, row["rule_type"], schedule, version_id,
             json.dumps(row["parameters"], sort_keys=True), row["source_rule_text"], row["source_page"],
             row["citation_status"]))
        new_structured += 1
    for rule, reason in unparsed:
        new_unparsed += int(parse_review.queue_item(
            conn, QUEUE_KIND, str(rule["id"]), schedule, QUEUE_FIELD, (rule["rule_text"] or "")[:500], SIGNAL,
            f"{utility} {schedule} rule {rule['id']} ({rule['rule_type']}, p. {rule['source_page']}) "
            f"was not structured: {reason}"))
    conn.commit()
    return {"rules": len(rules), "structured": len(structured), "unparsed": len(unparsed),
            "structured_new": new_structured, "unparsed_queued_new": new_unparsed,
            "types": [s["rule_type"] for s in structured]}
