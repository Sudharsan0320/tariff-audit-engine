import math
import sqlite3
from pathlib import Path


DATABASE_PATH = Path(__file__).resolve().parents[1] / "data" / "tariffs.db"


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

    orphan_rows = conn.execute(
        """
        SELECT c.id, c.tariff_version_id
        FROM tariff_components AS c
        LEFT JOIN tariff_versions AS v ON v.id = c.tariff_version_id
        WHERE v.id IS NULL
        ORDER BY c.id
        """
    ).fetchall()
    for component_id, component_version_id in orphan_rows:
        failures.append(
            f"Component {component_id} references missing version "
            f"{component_version_id}."
        )

    if draft_ids:
        placeholders = ",".join("?" for _ in draft_ids)
        components = conn.execute(
            f"""
            SELECT id, tariff_version_id, component_type, billing_category, rate,
                   tier_min_kwh, tier_max_kwh, season
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
                tier_min_kwh,
                tier_max_kwh,
                season,
            ) = component
            if (
                not isinstance(rate, (int, float))
                or not math.isfinite(rate)
                or rate <= 0
            ):
                failures.append(
                    f"Component {component_id} has a non-positive or invalid rate: "
                    f"{rate!r}."
                )

            key = (
                component_version_id,
                component_type,
                billing_category,
                rate,
                tier_min_kwh,
                tier_max_kwh,
                season,
            )
            if key in seen_components:
                failures.append(
                    f"Components {seen_components[key]} and {component_id} are "
                    f"duplicates for version {component_version_id}."
                )
            else:
                seen_components[key] = component_id

    return failures


def main():
    with sqlite3.connect(DATABASE_PATH) as conn:
        failures = validate_drafts(conn)

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PASS: all draft versions and components validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
