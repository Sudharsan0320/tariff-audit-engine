"""Decimal-only tariff evaluation and effective-version resolution."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itertools import groupby
import re


CENT = Decimal("0.01")
ROUNDING = ROUND_HALF_UP
DETERMINANT_VARIANTS = {
    "on_peak": "on_peak_usage",
    "off_peak": "off_peak_usage",
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
    determinant_type = DETERMINANT_VARIANTS.get(variant, "total_consumption")
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
        if "demand" not in determinants:
            raise ValueError("Component requires missing determinant 'demand'.")
        return rate * as_decimal(determinants["demand"])
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
