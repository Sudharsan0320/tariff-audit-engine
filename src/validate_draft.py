import math
import sqlite3
from pathlib import Path


DATABASE_PATH = Path(__file__).resolve().parents[1] / "data" / "tariffs.db"
VALID_UNITS = {
    "usd_per_kwh",
    "usd_per_kw",
    "usd_per_rkva",
    "usd_per_month",
    "usd_per_unit_month",
    "usd_per_luminaire",
    "usd_per_service",
    "usd_per_hour_unit",
    "percent",
    "multiplier",
}
NON_ORDINARY_TOKENS = ("credit", "adjustment", "payment")


def validate_drafts(conn, version_id=None):
    """Return validation failures for all drafts, or one draft when version_id is given."""
    failures = []
    version_query = """
        SELECT id, state, utility, schedule_code
        FROM tariff_versions
        WHERE status = 'draft'
    """
    params = ()
    if version_id is not None:
        version_query += " AND id = ?"
        params = (version_id,)

    drafts = conn.execute(version_query, params).fetchall()
    draft_ids = {row[0] for row in drafts}

    if version_id is not None and not drafts:
        return [f"Version {version_id} does not exist as a draft."]

    for version in drafts:
        draft_id, state, utility, schedule_code = version
        for field, value in (
            ("state", state),
            ("utility", utility),
            ("schedule_code", schedule_code),
        ):
            if not isinstance(value, str) or not value.strip():
                failures.append(f"Draft version {draft_id} has an empty {field}.")

    orphan_rows = (
        conn.execute(
            """
            SELECT c.id, c.tariff_version_id
            FROM tariff_components AS c
            LEFT JOIN tariff_versions AS v ON v.id = c.tariff_version_id
            WHERE v.id IS NULL
            ORDER BY c.id
            """
        ).fetchall()
        if version_id is None
        else []
    )
    for component_id, component_version_id in orphan_rows:
        failures.append(
            f"Component {component_id} references missing version "
            f"{component_version_id}."
        )

    orphan_rules = (
        conn.execute(
            """
            SELECT r.id, r.tariff_version_id
            FROM tariff_rules AS r
            LEFT JOIN tariff_versions AS v ON v.id = r.tariff_version_id
            WHERE v.id IS NULL
            ORDER BY r.id
            """
        ).fetchall()
        if version_id is None
        else []
    )
    for rule_id, rule_version_id in orphan_rules:
        failures.append(
            f"Rule {rule_id} references missing version {rule_version_id}."
        )

    if draft_ids:
        placeholders = ",".join("?" for _ in draft_ids)
        components = conn.execute(
            f"""
            SELECT id, tariff_version_id, component_type, billing_category, rate,
                   formula, unit, tier_min, tier_max, tier_unit, season, variant,
                   billing_class
            FROM tariff_components
            WHERE tariff_version_id IN ({placeholders})
            ORDER BY id
            """,
            tuple(sorted(draft_ids)),
        ).fetchall()
        seen_components = {}
        for component in components:
            (
                component_id,
                component_version_id,
                component_type,
                billing_category,
                rate,
                formula,
                unit,
                tier_min,
                tier_max,
                tier_unit,
                season,
                variant,
                billing_class,
            ) = component

            rate_present = rate is not None
            formula_present = isinstance(formula, str) and bool(formula.strip())
            if rate_present == formula_present:
                failures.append(
                    f"Component {component_id} must have exactly one of rate or "
                    f"formula."
                )

            if rate_present:
                if (
                    not isinstance(rate, (int, float))
                    or isinstance(rate, bool)
                    or not math.isfinite(rate)
                ):
                    failures.append(
                        f"Component {component_id} has an invalid rate: {rate!r}."
                    )
                elif rate < 0 and not (
                    isinstance(component_type, str)
                    and any(
                        token in component_type.lower()
                        for token in NON_ORDINARY_TOKENS
                    )
                ):
                    failures.append(
                        f"Component {component_id} has a non-positive rate: "
                        f"{rate!r}."
                    )

            if unit not in VALID_UNITS:
                failures.append(
                    f"Component {component_id} has an invalid unit: {unit!r}."
                )

            if tier_min is not None and (
                not isinstance(tier_unit, str) or not tier_unit.strip()
            ):
                failures.append(
                    f"Component {component_id} has tier_min but no tier_unit."
                )

            key = (
                component_version_id,
                component_type,
                billing_category,
                rate,
                formula,
                unit,
                tier_min,
                tier_max,
                season,
                variant,
                billing_class,
            )
            if key in seen_components:
                failures.append(
                    f"Components {seen_components[key]} and {component_id} are "
                    f"duplicates for version {component_version_id}."
                )
            else:
                seen_components[key] = component_id

        rules = conn.execute(
            f"""
            SELECT id, tariff_version_id, rule_type, rule_text
            FROM tariff_rules
            WHERE tariff_version_id IN ({placeholders})
            ORDER BY id
            """,
            tuple(sorted(draft_ids)),
        ).fetchall()
        seen_rules = {}
        for rule_id, rule_version_id, rule_type, rule_text in rules:
            if not isinstance(rule_type, str) or not rule_type.strip():
                failures.append(f"Rule {rule_id} has an empty rule_type.")
            if not isinstance(rule_text, str) or not rule_text.strip():
                failures.append(f"Rule {rule_id} has an empty rule_text.")

            key = (rule_version_id, rule_type, rule_text)
            if key in seen_rules:
                failures.append(
                    f"Rules {seen_rules[key]} and {rule_id} are duplicates for "
                    f"version {rule_version_id}."
                )
            else:
                seen_rules[key] = rule_id

    return failures


def main():
    with sqlite3.connect(DATABASE_PATH) as conn:
        failures = validate_drafts(conn)

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PASS: all draft versions, components, and rules validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
