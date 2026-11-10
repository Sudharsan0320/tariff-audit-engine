"""Decimal-only tariff evaluation and effective-version resolution."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itertools import groupby
import json
import re


CENT = Decimal("0.01")
ROUNDING = ROUND_HALF_UP
DETERMINANT_VARIANTS = {
    "on_peak": "on_peak_usage",
    "off_peak": "off_peak_usage",
}
DETERMINANT_COMPONENT_TYPES = {
    "on_peak_energy_charge": "on_peak_usage",
    "off_peak_energy_charge": "off_peak_usage",
}


def as_decimal(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("A finite numeric value is required.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid decimal value: {value!r}.") from None
    if not number.is_finite():
        raise ValueError(f"Non-finite decimal value: {value!r}.")
    return number


def round_money(value):
    return as_decimal(value).quantize(CENT, rounding=ROUNDING)


def _structured_rule_parameters(rule):
    if rule.get("citation_status") != "cited" or rule.get("source_page") is None:
        raise ValueError("Structured tariff rule must have a page citation.")
    parameters = rule.get("parameters")
    if parameters is None and rule.get("parameters_json") is not None:
        parameters = json.loads(rule["parameters_json"])
    if not isinstance(parameters, dict):
        raise ValueError("Structured tariff rule parameters must be an object.")
    return parameters


def _holiday_date(year, holiday):
    month = holiday.get("month")
    if holiday.get("day") is not None:
        return date(year, month, holiday["day"])
    weekday = holiday.get("weekday")
    occurrence = holiday.get("occurrence")
    if not isinstance(weekday, int) or weekday not in range(7):
        raise ValueError("Recurring holiday weekday must be between 0 and 6.")
    if occurrence == -1:
        last_day = (
            date(year + 1, month, 1) - date.resolution
            if month == 12
            else date(year, month + 1, 1) - date.resolution
        )
        return last_day - timedelta(days=(last_day.weekday() - weekday) % 7)
    if not isinstance(occurrence, int) or occurrence not in range(1, 6):
        raise ValueError("Recurring holiday occurrence must be -1 or between 1 and 5.")
    first_day = date(year, month, 1)
    first_occurrence = first_day + timedelta(
        days=(weekday - first_day.weekday()) % 7
    )
    holiday_date = first_occurrence + timedelta(weeks=occurrence - 1)
    if holiday_date.month != month:
        raise ValueError("Recurring holiday occurrence falls outside its month.")
    return holiday_date


def classify_tariff_period(timestamp, structured_rules):
    """Classify a local timestamp using cited on-peak/off-peak period rules."""
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp)
        except ValueError:
            raise ValueError(f"Invalid tariff timestamp: {timestamp!r}.") from None
    if not isinstance(timestamp, datetime):
        raise ValueError("Tariff timestamp must be a datetime or ISO datetime string.")
    if timestamp.tzinfo is not None:
        raise ValueError("Tariff period timestamps must be local and timezone-naive.")

    periods = {}
    for rule in structured_rules:
        if rule.get("rule_type") not in {"on_peak_hours", "off_peak_hours"}:
            continue
        parameters = _structured_rule_parameters(rule)
        period_name = parameters.get("period")
        if period_name not in {"on_peak", "off_peak"} or period_name in periods:
            raise ValueError("Structured periods must define one on-peak and off-peak rule.")
        periods[period_name] = parameters
    if set(periods) != {"on_peak", "off_peak"}:
        raise ValueError("Both structured on-peak and off-peak rules are required.")

    on_peak = periods["on_peak"]
    off_peak = periods["off_peak"]
    if (
        on_peak.get("timezone") != "local"
        or off_peak.get("timezone") != "local"
        or off_peak.get("weekday_behavior") != "complement_of_on_peak"
        or on_peak.get("weekdays") != [0, 1, 2, 3, 4]
    ):
        raise ValueError("Unsupported structured time-of-use period definition.")
    start = time.fromisoformat(on_peak["start_local"])
    end = time.fromisoformat(on_peak["end_local"])
    if start >= end:
        raise ValueError("On-peak interval must start before it ends.")

    holidays = {
        _holiday_date(timestamp.year, holiday)
        for holiday in off_peak.get("holidays", [])
    }
    is_weekday = timestamp.weekday() in on_peak["weekdays"]
    is_on_peak = (
        is_weekday and timestamp.date() not in holidays
        and start <= timestamp.time() < end
    )
    return "on_peak" if is_on_peak else "off_peak"


# Schema of each demand evaluator: the bill-determinant keys its path looks up.
# sbs_demand_v1 reads determinants[generation["measured_field"]],
# determinants[generation["reservation_field"]], and
# determinants[contract["capacity_field"]] (and schedule_review._structured_demand_inputs
# requires those same keys on the bill). A stored rule has to use these names.
EVALUATOR_FIELDS = {
    "sbs_demand_v1": {
        "measured_field": "highest_on_peak_demand_kw",
        "reservation_field": "supplemental_capacity_reservation_kw",
        "capacity_field": "standby_contract_capacity_kw",
    },
}


def apply_structured_demand_rules(components, determinants, bill_to, rules):
    """Derive component-specific kW determinants from supported cited rules."""
    resolved = dict(determinants)
    component_demands = dict(determinants.get("demand_by_component_type", {}))
    matching_rules = [
        rule
        for rule in rules
        if rule.get("rule_type") == "demand_determination"
    ]
    evaluators = []
    for rule in matching_rules:
        parameters = _structured_rule_parameters(rule)
        if parameters.get("evaluator") == "sbs_demand_v1":
            evaluators.append((rule, parameters))
    if len(evaluators) > 1:
        raise ValueError("Multiple structured SBS demand evaluators apply.")
    if not evaluators:
        return resolved

    _, parameters = evaluators[0]
    date_value = _iso_date(bill_to)
    generation = parameters["generation_demand"]
    contract = parameters["contract_capacity_demand"]
    generation_components = [
        component for component in components
        if component.get("component_type") == generation["component_type"]
        and component.get("variant") == generation["variant"]
        and component.get("unit") == "usd_per_kw"
    ]
    contract_components = [
        component for component in components
        if component.get("component_type") in contract["component_types"]
        and component.get("variant") == contract["variant"]
        and component.get("unit") == "usd_per_kw"
    ]
    required_generation = bool(generation_components)
    required_contract = bool(contract_components)
    if required_generation:
        if date_value.month not in generation["months"]:
            raise ValueError(
                f"Structured SBS generation demand does not cover month "
                f"{date_value.month}."
            )
        measured = as_decimal(determinants.get(generation["measured_field"]))
        reservation = as_decimal(determinants.get(generation["reservation_field"]))
        if measured < 0 or reservation < 0:
            raise ValueError("SBS demand inputs cannot be negative.")
        component_demands[generation["component_type"]] = max(
            measured - reservation, Decimal(0)
        )
    if required_contract:
        capacity = as_decimal(determinants.get(contract["capacity_field"]))
        if capacity < 0:
            raise ValueError("SBS contract capacity cannot be negative.")
        for component_type in contract["component_types"]:
            if any(
                component.get("component_type") == component_type
                and component.get("variant") == contract["variant"]
                and component.get("unit") == "usd_per_kw"
                for component in contract_components
            ):
                component_demands[component_type] = capacity

    if component_demands:
        resolved["demand_by_component_type"] = component_demands
    return resolved


def _iso_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"Invalid ISO date: {value!r}.") from None


def _dict_rows(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _dict_row(cursor):
    rows = _dict_rows(cursor)
    return rows[0] if rows else None


def resolve_version(conn, schedule_code, bill_date, state=None, utility=None):
    """Resolve exactly one active tariff version for a bill date."""
    bill_date = _iso_date(bill_date)
    query = """
        SELECT id, state, utility, schedule_code, version,
               effective_from, effective_to, status
        FROM tariff_versions
        WHERE schedule_code = ? AND status IN ('active', 'superseded')
    """
    parameters = [schedule_code]
    if state is not None:
        query += " AND state = ?"
        parameters.append(state)
    if utility is not None:
        query += " AND utility = ?"
        parameters.append(utility)
    matches = []
    for version in _dict_rows(conn.execute(query, parameters)):
        start = _iso_date(version["effective_from"])
        end = _iso_date(version["effective_to"]) if version["effective_to"] else None
        if start <= bill_date and (end is None or bill_date < end):
            matches.append(version)
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one active version for schedule {schedule_code!r} "
            f"on {bill_date.isoformat()}, found {len(matches)}."
        )
    return matches[0]


def resolve_period_version(conn, schedule_code, bill_from, bill_to, state=None, utility=None):
    """Resolve by bill_to and refuse bills that span a version boundary."""
    start = _iso_date(bill_from)
    end = _iso_date(bill_to)
    version = resolve_version(conn, schedule_code, end, state=state, utility=utility)
    effective_from = _iso_date(version["effective_from"])
    effective_to = (
        _iso_date(version["effective_to"]) if version["effective_to"] else None
    )
    if start < effective_from or (effective_to is not None and end >= effective_to):
        raise ValueError(
            f"Bill period {start.isoformat()} through {end.isoformat()} is not "
            f"fully covered by active version {version['id']} for schedule "
            f"{schedule_code!r}."
        )
    return version


def _determinant_for(component, determinants):
    variant = component.get("variant")
    determinant_type = (
        component.get("determinant_type")
        or DETERMINANT_VARIANTS.get(variant)
        or DETERMINANT_COMPONENT_TYPES.get(
            component.get("component_type"), "total_consumption"
        )
    )
    if determinant_type not in determinants:
        raise ValueError(
            f"Component {component.get('id')} requires missing determinant "
            f"{determinant_type!r}."
        )
    return determinant_type, as_decimal(determinants[determinant_type])


def evaluate_component(component, determinants):
    """Evaluate one untiered component; tier blocks must be passed as a group."""
    if component.get("formula") is not None:
        raise ValueError(
            f"Formula component {component.get('id')} has no documented evaluator."
        )
    rate = as_decimal(component.get("rate"))
    unit = component.get("unit")
    if component.get("tier_min") is not None or component.get("tier_max") is not None:
        raise ValueError("Tiered components must be evaluated as a component group.")
    if unit == "usd_per_kwh":
        _, quantity = _determinant_for(component, determinants)
        return rate * quantity
    if unit == "usd_per_kw":
        component_demands = determinants.get("demand_by_component_type", {})
        if not isinstance(component_demands, dict):
            raise ValueError("Component-specific demand values must be an object.")
        if component.get("component_type") in component_demands:
            demand = component_demands[component["component_type"]]
        elif "demand" in determinants:
            demand = determinants["demand"]
        else:
            raise ValueError("Component requires missing determinant 'demand'.")
        return rate * as_decimal(demand)
    if unit in {"usd_per_month", "usd_per_unit_month"}:
        return rate
    raise ValueError(
        f"Unsupported unit {unit!r} for component {component.get('id')}."
    )


def evaluate_component_group(components, determinants):
    """Evaluate a homogeneous component family, including marginal tiers."""
    components = sorted(
        components,
        key=lambda component: (
            as_decimal(component.get("tier_min") or 0),
            component.get("id") or 0,
        ),
    )
    if not components:
        return Decimal(0)
    tiered = any(
        component.get("tier_min") is not None
        or component.get("tier_max") is not None
        for component in components
    )
    if not tiered:
        return sum(
            (evaluate_component(component, determinants) for component in components),
            Decimal(0),
        )
    if not all(component.get("unit") == "usd_per_kwh" for component in components):
        raise ValueError("Tiered components must use usd_per_kwh.")
    tier_units = {component.get("tier_unit") for component in components}
    if len(tier_units) != 1:
        raise ValueError("Tiered component group has inconsistent tier units.")
    tier_unit = next(iter(tier_units))
    if tier_unit == "kwh":
        total = as_decimal(determinants.get("total_consumption"))
        scale = Decimal(1)
    elif tier_unit == "kwh_per_kw":
        if "demand" not in determinants:
            raise ValueError("Tiered component requires missing determinant 'demand'.")
        total = as_decimal(determinants.get("total_consumption"))
        scale = as_decimal(determinants["demand"])
    else:
        raise ValueError(f"Unsupported tier unit {tier_unit!r}.")
    if scale <= 0 and total:
        raise ValueError("Tier scale must be positive for nonzero usage.")

    amount = Decimal(0)
    previous_max = Decimal(0)
    for component in components:
        lower = as_decimal(component.get("tier_min") or 0) * scale
        upper = (
            as_decimal(component["tier_max"]) * scale
            if component.get("tier_max") is not None
            else max(lower, total)
        )
        if lower > previous_max:
            raise ValueError("Tiered component group contains a gap.")
        if lower < previous_max:
            raise ValueError("Tiered component group contains overlapping blocks.")
        if upper < lower:
            raise ValueError("Tier maximum is below its minimum.")
        block_quantity = max(Decimal(0), min(total, upper) - lower)
        amount += as_decimal(component["rate"]) * block_quantity
        previous_max = upper
    if previous_max < total:
        raise ValueError("Tiered component group does not cover all usage.")
    return amount


def _representation_family(component):
    category = component.get("billing_category")
    if category:
        family = str(category).casefold().strip()
        variant = component.get("variant")
        family = re.sub(
            r"\s*\(\s*(?:generation|distribution|total)\s*\)\s*$",
            "",
            family,
            flags=re.IGNORECASE,
        ).strip()
        if variant in {"generation", "distribution", "total"}:
            family = re.sub(
                rf"\s*[-–—]\s*{variant}\s*$", "", family, flags=re.IGNORECASE
            ).strip()
        return family
    return component.get("component_type")


def _representation_variant(component):
    variant = component.get("variant")
    if variant in {"generation", "distribution", "total"}:
        return variant
    category = str(component.get("billing_category") or "")
    match = re.search(
        r"\(\s*(generation|distribution|total)\s*\)\s*$",
        category,
        flags=re.IGNORECASE,
    )
    return match.group(1).casefold() if match else variant


def _select_representations(components):
    groups = {}
    for component in components:
        key = (
            _representation_family(component),
            component.get("unit"),
            component.get("season"),
            component.get("tier_min"),
            component.get("tier_max"),
            component.get("tier_unit"),
            component.get("billing_class"),
        )
        groups.setdefault(key, []).append(component)

    selected = []
    for group in groups.values():
        variants = {_representation_variant(component) for component in group}
        if "total" in variants and variants.intersection({"generation", "distribution"}):
            selected.extend(
                component
                for component in group
                if _representation_variant(component) == "total"
            )
        else:
            selected.extend(group)
    return selected


def _season_for_month(conn, version_id, bill_to):
    rows = conn.execute(
        """
        SELECT rule_text FROM tariff_rules
        WHERE tariff_version_id = ? AND rule_type = 'billing_month_seasons'
        """,
        (version_id,),
    ).fetchall()
    month = _iso_date(bill_to).month
    season_by_month = {}
    range_pattern = re.compile(
        r"([A-Za-z]+) billing months ([A-Za-z]+) through ([A-Za-z]+)",
        re.IGNORECASE,
    )
    for (rule_text,) in rows:
        for season, first_month, last_month in range_pattern.findall(rule_text):
            first = datetime.strptime(first_month, "%B").month
            last = datetime.strptime(last_month, "%B").month
            months = (
                range(first, last + 1)
                if first <= last
                else (*range(first, 13), *range(1, last + 1))
            )
            for season_month in months:
                season_name = season.casefold()
                if (
                    season_month in season_by_month
                    and season_by_month[season_month] != season_name
                ):
                    raise ValueError(
                        f"Tariff version {version_id} has conflicting seasons "
                        f"for month {season_month}."
                    )
                season_by_month[season_month] = season_name
    if month not in season_by_month:
        raise ValueError(
            f"No tariff season rule resolves bill month {month} for version "
            f"{version_id}."
        )
    return season_by_month[month]


def evaluate_schedule_components(
    conn, components, determinants, bill_to, billing_class=None, version_id=None
):
    """Return rounded line-family amounts for a schedule's selected components."""
    selected = [
        component
        for component in components
        if billing_class is None
        or component.get("billing_class") == billing_class
    ]
    seasonal = [
        component for component in selected if component.get("season") is not None
    ]
    if seasonal:
        if version_id is None:
            raise ValueError("A tariff version is required to resolve seasonal rates.")
        active_season = _season_for_month(conn, version_id, bill_to)
        selected = [
            component
            for component in selected
            if component.get("season") is None
            or component.get("season") == active_season
        ]
    selected = _select_representations(selected)
    selected.sort(
        key=lambda component: (
            component.get("component_type") or "",
            component.get("billing_category") or "",
            component.get("variant") or "",
            component.get("billing_class") or "",
            component.get("season") or "",
        )
    )
    groups = []
    for _, grouped in groupby(
        selected,
        key=lambda component: (
            component.get("component_type"),
            component.get("billing_category"),
            component.get("variant"),
            component.get("billing_class"),
            component.get("season"),
        ),
    ):
        group = list(grouped)
        raw_amount = evaluate_component_group(group, determinants)
        groups.append(
            {
                "component_type": group[0].get("component_type"),
                "amount": round_money(raw_amount),
            }
        )
    return groups
