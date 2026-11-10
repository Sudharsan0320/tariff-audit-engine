"""Stage 12g: structured rules proposed by a separate AI reading and verified deterministically.

A structured reading is its own model call per schedule section with its own cache
(`data/cache/ai-extract-structured/<model>/<book-digest>.json`, sections keyed like the main cache),
so the main readings are never invalidated. The model proposes rules in the 11C shapes (the
primary's six hand-built rows and what the billing evaluator reads); nothing is stored unless every
deterministic check below passes. No pattern parsing of rule text: the model reads, code verifies.

A proposal: {rule_type, applies_to, parameters, source_page, source_quote, [gap], [rate_year]}.
Verification order, first failure wins (each failure is queued with its own signal, never stored):
  type      rule_type is one of the six existing structured types      -> structured_type_unknown
  quote     source_quote is on its cited page (whitespace-normalised)  -> structured_quote_not_found
  gap       a gap's quote (and text) is on the gap's cited page        -> structured_quote_not_found
  shape     parameters have exactly the type's keys and value types    -> structured_shape_invalid
            (demand_determination field names must be the evaluator's canonical ones)
  printed   per-type printed-quantity checks (see PARAMETER_CHECKS)    -> structured_parameter_not_printed
"""
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from src import ai_extract, parse_review
    from src.billing_engine import EVALUATOR_FIELDS
except ImportError:
    import ai_extract
    import parse_review
    from billing_engine import EVALUATOR_FIELDS

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "cache" / "ai-extract-structured"
PROMPT_MARKER = "You propose structured tariff rules"

SIG_TYPE = "structured_type_unknown"
SIG_QUOTE = "structured_quote_not_found"
SIG_SHAPE = "structured_shape_invalid"
SIG_PARAM = "structured_parameter_not_printed"
SIG_READING = "structured_reading_unusable"

RULE_TYPES = ("eligibility", "minimum_charge", "provision_availability",
              "on_peak_hours", "off_peak_hours", "demand_determination")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
AMOUNT = re.compile(r"^\d+(?:\.\d+)?$")
MONTH_ABBR = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
MONTH_NAME = ("january", "february", "march", "april", "may", "june", "july", "august",
              "september", "october", "november", "december")
_MONTH_NUMBER = {name: i for i, name in enumerate(MONTH_NAME, 1)}
_MONTH_NUMBER.update({abbr: i for i, abbr in enumerate(MONTH_ABBR, 1)})
_MONTH_WORD = "|".join(MONTH_NAME + MONTH_ABBR)
# A printed range entails every month from its start through its end, wrapping the year when
# the start falls after the end (December–February is 12, 1, 2). Interior months count as
# printed even when the page never names them. Forms: "June–September", "June - September",
# "December through February", with a full or abbreviated name and an en dash, hyphen, or "through".
_MONTH_RANGE = re.compile(
    rf"\b({_MONTH_WORD})\b\s*(?:-|through)\s*\b({_MONTH_WORD})\b")


# ---------------------------------------------------------------- shapes (11C rows + evaluator)
def _str(v):
    return isinstance(v, str) and bool(v.strip())


def _bool(v):
    return isinstance(v, bool)


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _exact(value, spec, where):
    """Exactly the keys in `spec`, each passing its check. Returns an error string or None."""
    if not isinstance(value, dict):
        return f"{where} must be an object"
    if set(value) != set(spec):
        missing, extra = sorted(set(spec) - set(value)), sorted(set(value) - set(spec))
        return f"{where} keys differ (missing {missing}, unexpected {extra})"
    for key, check in spec.items():
        if not check(value[key]):
            return f"{where}.{key} has the wrong type or value ({value[key]!r})"
    return None


def _list_of(check, minimum=1):
    return lambda v: isinstance(v, list) and len(v) >= minimum and all(check(i) for i in v)


def _holiday(v):
    if not isinstance(v, dict) or not _str(v.get("name")) or not _int(v.get("month")) or not 1 <= v["month"] <= 12:
        return False
    if set(v) == {"day", "month", "name"}:
        return _int(v["day"]) and 1 <= v["day"] <= 31
    if set(v) == {"month", "name", "occurrence", "weekday"}:
        return (_int(v["weekday"]) and 0 <= v["weekday"] <= 6 and _int(v["occurrence"])
                and (v["occurrence"] == -1 or 1 <= v["occurrence"] <= 5))
    return False


def _eq(expected):
    return lambda v: v == expected


SHAPES = {
    "eligibility": {"customer_type": _str, "includes_rural_agricultural_customers": _bool,
                    "single_meter": _bool, "takes_standard_service": _bool},
    "minimum_charge": {"derived_from": _str, "unit": _eq("usd_per_month"),
                       "value": lambda v: isinstance(v, str) and bool(AMOUNT.match(v))},
    "provision_availability": {"existing_customers_may_remain": _bool, "new_customers_allowed": _bool,
                               "provision": _str},
    "on_peak_hours": {"end_local": lambda v: isinstance(v, str) and bool(HHMM.match(v)), "period": _eq("on_peak"),
                      "start_local": lambda v: isinstance(v, str) and bool(HHMM.match(v)), "timezone": _eq("local"),
                      "weekdays": _list_of(lambda d: _int(d) and 0 <= d <= 6)},
    "off_peak_hours": {"holidays": _list_of(_holiday, 0), "period": _eq("off_peak"), "timezone": _eq("local"),
                       "weekday_behavior": _eq("complement_of_on_peak"), "weekends": _eq("all_day")},
    "demand_determination": {
        "evaluator": _eq("sbs_demand_v1"),
        "generation_demand": lambda v: _exact(v, {
            "component_type": _str, "measured_field": _str,
            "months": _list_of(lambda m: _int(m) and 1 <= m <= 12), "reservation_field": _str,
            "variant": _str}, "generation_demand") is None,
        "contract_capacity_demand": lambda v: _exact(v, {
            "capacity_field": _str, "component_types": _list_of(_str), "variant": _str},
            "contract_capacity_demand") is None,
    },
}


def _canonical_field_error(parameters):
    """demand_determination field names are schema: they must be the named evaluator's keys."""
    evaluator = parameters.get("evaluator")
    canonical = EVALUATOR_FIELDS.get(evaluator)
    if not canonical:
        return f"evaluator {evaluator!r} has no canonical field names"
    generation = parameters["generation_demand"]
    contract = parameters["contract_capacity_demand"]
    actual = {
        "measured_field": ("generation_demand.measured_field", generation["measured_field"]),
        "reservation_field": ("generation_demand.reservation_field", generation["reservation_field"]),
        "capacity_field": ("contract_capacity_demand.capacity_field", contract["capacity_field"]),
    }
    mismatches = []
    for field, expected in canonical.items():
        where, got = actual[field]
        if got != expected:
            mismatches.append(
                f"{where} {got!r} is not the canonical name {expected!r} for evaluator {evaluator!r}")
    return "; ".join(mismatches) if mismatches else None


def shape_error(rule_type, parameters):
    error = _exact(parameters, SHAPES[rule_type], "parameters")
    if error:
        return error
    if rule_type == "on_peak_hours" and parameters["start_local"] >= parameters["end_local"]:
        return "parameters on-peak interval must start before it ends"
    if rule_type == "demand_determination":
        return _canonical_field_error(parameters)
    return None


# ---------------------------------------------------------------- printed-quantity checks
def _flat(text):
    return " ".join(str(text).replace("’", "'").replace("‘", "'").split()).casefold()


def _hour_printed(hhmm, text):
    """The clock time appears on the page as 'h a.m./p.m.', 'h:mm a.m.', 'noon', 'midnight' or 24-hour 'hh:mm'."""
    hour, minute = int(hhmm[:2]), int(hhmm[3:])
    flat = _flat(text)
    if hhmm in flat:
        return True
    if hour == 12 and minute == 0 and "noon" in flat:
        return True
    if hour == 0 and minute == 0 and "midnight" in flat:
        return True
    twelve = hour % 12 or 12
    meridiem = r"a\.?\s?m\.?" if hour < 12 else r"p\.?\s?m\.?"
    clock = rf"{twelve}(?::{minute:02d})?" if minute == 0 else rf"{twelve}:{minute:02d}"
    return re.search(rf"(?<![\d.:]){clock}\s*{meridiem}", flat) is not None


def _check_eligibility(params, text, ctx):
    missing = [w for w in re.findall(r"[a-z]+", params["customer_type"].casefold()) if w not in _flat(text)]
    return f"customer_type words not printed on the page: {missing}" if missing else None


def _check_minimum_charge(params, text, ctx):
    value = Decimal(params["value"])
    derived = [Decimal(str(c["rate"])) for c in ctx["components"]
               if c["component_type"] == params["derived_from"] and c["unit"] == "usd_per_month"
               and c["rate"] is not None]
    if derived:
        if value not in derived:
            return (f"value {params['value']} is not a stored {params['derived_from']} rate of this version "
                    f"({sorted(str(d) for d in derived)})")
        return None
    if not ai_extract.rate_on_page(value, text):
        return (f"value {params['value']} is not printed on the page and this version stores no "
                f"{params['derived_from']} usd_per_month component to derive it from")
    return None


def _check_provision(params, text, ctx):
    if _flat(params["provision"]) not in _flat(text):
        return f"provision {params['provision']!r} is not printed on the page"
    return None


def _check_on_peak(params, text, ctx):
    bad = [p[k] for p in (params,) for k in ("start_local", "end_local") if not _hour_printed(p[k], text)]
    return f"clock times not printed on the page: {bad}" if bad else None


def _check_off_peak(params, text, ctx):
    unprinted = [h["name"] for h in params["holidays"] if _flat(h["name"]) not in _flat(text)]
    if unprinted:
        return f"holidays not named on the page: {unprinted}"
    try:
        for holiday in params["holidays"]:
            ctx["holiday_date"](2026, holiday)
    except (ValueError, TypeError) as error:
        return f"a holiday definition the evaluator cannot compute: {error}"
    return None


def _months_in_printed_ranges(flat):
    """Months entailed by a range printed on the page, including unnamed interior months."""
    text = flat.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    entailed = set()
    for match in _MONTH_RANGE.finditer(text):
        start, end = _MONTH_NUMBER[match.group(1)], _MONTH_NUMBER[match.group(2)]
        if start <= end:
            entailed.update(range(start, end + 1))
        else:
            entailed.update(range(start, 13))
            entailed.update(range(1, end + 1))
    return entailed


def _check_demand(params, text, ctx):
    stored = {(c["component_type"], c["variant"]) for c in ctx["components"]}
    gen, contract = params["generation_demand"], params["contract_capacity_demand"]
    wanted = [(gen["component_type"], gen["variant"])] + [(t, contract["variant"]) for t in contract["component_types"]]
    absent = [w for w in wanted if w not in stored]
    if absent:
        return f"component (type, variant) not stored for this version: {absent}"
    flat = _flat(text)
    entailed = _months_in_printed_ranges(flat)
    months = [MONTH_ABBR[m - 1] for m in gen["months"]
              if m not in entailed and not re.search(rf"\b{MONTH_ABBR[m - 1]}", flat)]
    return f"months not printed on the page: {months}" if months else None


PARAMETER_CHECKS = {
    "eligibility": _check_eligibility, "minimum_charge": _check_minimum_charge,
    "provision_availability": _check_provision, "on_peak_hours": _check_on_peak,
    "off_peak_hours": _check_off_peak, "demand_determination": _check_demand,
}

# The standby excess-demand sentence is a printed branch the demand parameters do not cover.
EXCESS_DEMAND_BRANCH = "the excess demand shall be considered as supplemental demand"


def _holiday_date():
    try:
        from src import billing_engine
    except ImportError:
        import billing_engine
    return billing_engine._holiday_date


# ---------------------------------------------------------------- prompt
def _evaluator_field_clause():
    """Field names are schema, stated with the parameter shape, one clause per evaluator."""
    parts = []
    for evaluator, fields in EVALUATOR_FIELDS.items():
        listed = ", ".join(f'{name} is "{value}"' for name, value in fields.items())
        parts.append(f'for evaluator "{evaluator}", {listed}')
    return (
        "An evaluator's field names are part of its schema and are stated with its parameter shape; "
        "use exactly these values and no others: "
        + "; ".join(parts)
        + ". "
    )


def system_prompt(component_types=(), component_pairs=()):
    listing = ", ".join(component_types) if component_types else "(none stored yet)"
    if component_pairs:
        pair_listing = ", ".join(
            f"({ctype}, {variant if variant else 'null'})" for ctype, variant in component_pairs)
    else:
        pair_listing = "(none stored yet)"
    return (
        f"{PROMPT_MARKER} for one electric-tariff schedule from the pages given. "
        "Reply with one JSON object {\"proposals\": [...]} and no other text. Propose a rule only when the "
        "pages print it; propose nothing for anything you would have to guess. "
        "Each proposal: rule_type, applies_to (the schedule's own designation), parameters, source_page "
        "(the page number labeling the page in the input), source_quote (copied verbatim from that page; it "
        "is checked against that page), and optionally rate_year (the label of the one rate year it belongs "
        "to, only when the schedule prints several rate years). "
        "rule_type must be one of: eligibility, minimum_charge, provision_availability, on_peak_hours, "
        "off_peak_hours, demand_determination. parameters must have exactly these keys and no others: "
        "eligibility {customer_type: string, includes_rural_agricultural_customers: boolean, single_meter: "
        "boolean, takes_standard_service: boolean}; "
        "minimum_charge {derived_from: component_type string, unit: \"usd_per_month\", value: decimal string}; "
        "provision_availability {existing_customers_may_remain: boolean, new_customers_allowed: boolean, "
        "provision: string as printed}; "
        "on_peak_hours {period: \"on_peak\", start_local: \"HH:MM\", end_local: \"HH:MM\", timezone: "
        "\"local\", weekdays: [0-6 integers, Monday=0]}; "
        "off_peak_hours {period: \"off_peak\", timezone: \"local\", weekday_behavior: "
        "\"complement_of_on_peak\", weekends: \"all_day\", holidays: [{name, month, day} for a fixed date or "
        "{name, month, weekday, occurrence} for a recurring one, occurrence -1 meaning last]}; "
        "demand_determination {evaluator: \"sbs_demand_v1\", generation_demand: {component_type, "
        "measured_field, months: [1-12], reservation_field, variant}, contract_capacity_demand: "
        "{capacity_field, component_types: [..], variant}}. "
        + _evaluator_field_clause() +
        "If a demand_determination section prints a branch your parameters do not cover, add one proposal-level "
        "object under the key gap — not gaps, and not an array — {text, source_page, source_quote, "
        "required_inputs: [strings]}. text and source_quote are verbatim substrings of the cited page, not a "
        "paraphrase. source_quote for demand_determination includes the generation-demand sentence and the "
        "transmission and distribution sentence, not a fragment of either. A printed branch must be either "
        "covered by parameters or reported as that gap, never dropped. "
        f"Stored component_type values: {listing}. "
        f"Stored (component_type, variant) pairs: {pair_listing}. "
        "Reference demand components only by these exact pairs."
    )


def user_prompt(section):
    lines = [f"Section heading: {section['designation']}",
             f"Printed name: {section['name'] or 'not printed in the index'}"]
    for page in section["pages"]:
        lines.append(f"--- page {page['n']} ---")
        lines.append(page["text"])
    return "\n".join(lines)


def read_structured(section, system, caller):
    """One live structured reading: (raw text, stop reason, usage used by this call)."""
    before = dict(getattr(caller, "usage", {}) or {})
    raw, stop = caller.complete(system, user_prompt(section))
    after = getattr(caller, "usage", None) or {}
    delta = {f: max(0, int(after.get(f, 0) or 0) - int(before.get(f, 0) or 0)) for f in ai_extract.USAGE_FIELDS}
    return raw, stop, delta


# ---------------------------------------------------------------- verification
def parse_proposals(raw, stop):
    """(proposals, error). A reading that stopped early or is not the JSON object required is unusable."""
    if stop not in ("end_turn", None):
        return [], f"structured reading stopped at {stop}"
    try:
        payload = ai_extract.parse_model_json(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return [], "structured reading was not the JSON object required"
    proposals = payload.get("proposals")
    if not isinstance(proposals, list):
        return [], "structured reading has no proposals list"
    return proposals, None


def _failure(code, version_id, index, field, value, signal, detail):
    item = ai_extract._queue(code, field, value, signal, detail, "structured", index)
    item["source_ref"] += f":v{version_id}"
    return item


def verify_proposals(proposals, section, components, version_id, rate_year=None, multi_year=False):
    """(rows, queued, applicable). Pure: no writes. `components` are this version's stored components."""
    pages = ai_extract._page_map(section)
    code = section["schedule_code"]
    own = {code, ai_extract.schedule_code(section["designation"], section.get("kind"))}
    ctx = {"components": components, "holiday_date": _holiday_date()}
    rows, queued, applicable = [], [], 0
    for index, proposal in enumerate(proposals):
        if multi_year and isinstance(proposal, dict) and proposal.get("rate_year") not in (None, "", rate_year):
            continue
        applicable += 1

        def fail(field, value, signal, detail):
            queued.append(_failure(code, version_id, index, field, str(value)[:500], signal, detail))

        if not isinstance(proposal, dict):
            fail("proposal", proposal, SIG_SHAPE, "proposal is not an object")
            continue
        rule_type = str(proposal.get("rule_type") or "").strip()
        if rule_type not in RULE_TYPES:
            fail("rule_type", rule_type, SIG_TYPE, f"rule_type {rule_type!r} is not in the structured vocabulary")
            continue
        quote = proposal.get("source_quote")
        try:
            page = int(proposal.get("source_page"))
        except (TypeError, ValueError):
            page = None
        text = pages.get(page)
        if text is None or not ai_extract.quote_on_page(quote, text):
            fail("source_quote", quote, SIG_QUOTE, f"{rule_type}: source quote not on page {page}")
            continue
        gap = proposal.get("gap")
        gap_row = None
        if gap is not None:
            if rule_type != "demand_determination" or not isinstance(gap, dict):
                fail("gap", gap, SIG_SHAPE, "a gap is allowed only on demand_determination and must be an object")
                continue
            try:
                gap_page = int(gap.get("source_page"))
            except (TypeError, ValueError):
                gap_page = None
            gap_text = pages.get(gap_page)
            gap_quote = gap.get("source_quote")
            printed = gap.get("text") or gap_quote
            required = gap.get("required_inputs")
            if gap_text is None or not ai_extract.quote_on_page(gap_quote, gap_text) \
                    or not ai_extract.quote_on_page(printed, gap_text):
                fail("gap_quote", gap_quote, SIG_QUOTE, f"gap quote or text not on page {gap_page}")
                continue
            if not _list_of(_str)(required):
                fail("gap", gap, SIG_SHAPE, "gap.required_inputs must be a non-empty list of strings")
                continue
            gap_row = {"required_inputs": required, "source_page": gap_page, "status": "not_structured",
                       "text": ai_extract.squash(printed)}
        applies_to = ai_extract.schedule_code(str(proposal.get("applies_to") or ""), section.get("kind"))
        if applies_to not in own:
            fail("applies_to", proposal.get("applies_to"), SIG_SHAPE,
                 f"applies_to must be this schedule ({code}); cross-schedule rules are not stored")
            continue
        parameters = proposal.get("parameters")
        error = shape_error(rule_type, parameters)
        if error:
            fail("parameters", json.dumps(parameters, default=str), SIG_SHAPE, f"{rule_type}: {error}")
            continue
        error = PARAMETER_CHECKS[rule_type](parameters, text, ctx)
        if error:
            fail("parameters", json.dumps(parameters, default=str), SIG_PARAM, f"{rule_type}: {error}")
            continue
        if "gaps" in proposal:
            fail("gap", "gaps", SIG_SHAPE,
                 f'{rule_type}: the key is "gap" (one object); "gaps" is not accepted')
            continue
        if rule_type == "demand_determination" and EXCESS_DEMAND_BRANCH in _flat(text):
            quoted = _flat((gap or {}).get("source_quote"))
            stated = _flat((gap or {}).get("text"))
            if EXCESS_DEMAND_BRANCH not in quoted and EXCESS_DEMAND_BRANCH not in stated:
                fail("gap", gap, SIG_SHAPE,
                     "demand_determination dropped the printed excess-demand branch; report it as gap")
                continue
        stored = dict(parameters)
        if gap_row:
            stored["gap"] = gap_row
        rows.append({"rule_type": rule_type, "parameters": stored, "source_rule_text": ai_extract.squash(quote),
                     "source_page": page, "citation_status": "cited"})
    return rows, queued, applicable


# ---------------------------------------------------------------- storage
def store_proposals(conn, version_id, schedule, rows):
    """Insert verified rows for one version; idempotent. Returns (stored_now_or_already, queued_conflicts)."""
    present, conflicts = 0, []
    for row in rows:
        encoded = json.dumps(row["parameters"], sort_keys=True)
        found = conn.execute(
            "SELECT parameters_json FROM structured_tariff_rules WHERE tariff_version_id = ? AND rule_type = ? "
            "AND applies_to_schedule = ? AND applies_to_version_id = ?",
            (version_id, row["rule_type"], schedule, version_id)).fetchone()
        if found:
            if found[0] == encoded:
                present += 1
            else:
                conflicts.append(_failure(
                    schedule, version_id, 0, "parameters", encoded[:500], SIG_SHAPE,
                    f"{row['rule_type']}: a different structured rule of this type is already stored"))
            continue
        conn.execute(
            "INSERT INTO structured_tariff_rules(tariff_version_id, rule_type, applies_to_schedule, "
            "applies_to_version_id, parameters_json, source_rule_text, source_page, citation_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, row["rule_type"], schedule, version_id, encoded, row["source_rule_text"],
             row["source_page"], row["citation_status"]))
        present += 1
    return present, conflicts
