"""Diff two stored versions of one tariff schedule. A pure function of stored data."""

from collections import Counter
from decimal import Decimal

KEY_FIELDS = (
    "component_type", "unit", "variant", "billing_class",
    "season", "tier_min", "tier_max", "billing_category",
)
VERSION_FIELDS = (
    "id", "state", "utility", "schedule_code", "version",
    "effective_from", "effective_to", "status",
)


class DiffError(ValueError):
    """The requested diff is not well defined for the stored versions."""


def _decimal(value):
    return None if value is None else Decimal(str(value))


def _rows(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _load_version(conn, version_id):
    cursor = conn.execute(
        f"SELECT {', '.join(VERSION_FIELDS)} FROM tariff_versions WHERE id = ?", (version_id,)
    )
    rows = _rows(cursor)
    if not rows:
        raise DiffError(f"No tariff version {version_id}.")
    return rows[0]


def _key(component):
    return tuple(
        _decimal(component[f]) if f in ("tier_min", "tier_max") else component[f]
        for f in KEY_FIELDS
    )


def _sort_key(key):
    return tuple((value is None, value if value is not None else 0) for value in key)


def _components(conn, version_id):
    cursor = conn.execute(
        """
        SELECT id, component_type, unit, variant, billing_class, season, tier_min,
               tier_max, billing_category, rate, formula, tier_unit, source_page, source_cell
        FROM tariff_components WHERE tariff_version_id = ? ORDER BY id
        """,
        (version_id,),
    )
    by_key = {}
    for row in _rows(cursor):
        key = _key(row)
        if key in by_key:
            raise DiffError(
                f"Version {version_id} has two components with natural key "
                f"{dict(zip(KEY_FIELDS, map(str, key)))}; the diff would be ambiguous."
            )
        by_key[key] = row
    return by_key


def _rules(conn, version_id):
    cursor = conn.execute(
        "SELECT rule_type, rule_text FROM tariff_rules WHERE tariff_version_id = ?", (version_id,)
    )
    return Counter((rule_type, rule_text) for rule_type, rule_text in cursor.fetchall())


def _key_dict(key):
    return {field: value for field, value in zip(KEY_FIELDS, key)}


def _side(component):
    return {
        "component_id": component["id"],
        "rate": _decimal(component["rate"]),
        "formula": component["formula"],
        "source_page": component["source_page"],
        "source_cell": component["source_cell"],
    }


def diff_versions(conn, old_version_id, new_version_id):
    old_meta = _load_version(conn, old_version_id)
    new_meta = _load_version(conn, new_version_id)
    if old_version_id == new_version_id:
        raise DiffError("Old and new version are the same version.")
    for field in ("schedule_code", "state", "utility"):
        if old_meta[field] != new_meta[field]:
            raise DiffError(
                f"Versions {old_version_id} and {new_version_id} are not versions of the same "
                f"schedule ({field}: {old_meta[field]!r} vs {new_meta[field]!r})."
            )

    old, new = _components(conn, old_version_id), _components(conn, new_version_id)
    added = [
        {**_key_dict(key), **_side(new[key])}
        for key in sorted(new.keys() - old.keys(), key=_sort_key)
    ]
    removed = [
        {**_key_dict(key), **_side(old[key])}
        for key in sorted(old.keys() - new.keys(), key=_sort_key)
    ]
    rate_changed, formula_changed, unchanged = [], [], 0
    for key in sorted(old.keys() & new.keys(), key=_sort_key):
        before, after = _side(old[key]), _side(new[key])
        changed = False
        if before["rate"] != after["rate"]:
            changed = True
            delta = None if before["rate"] is None or after["rate"] is None else after["rate"] - before["rate"]
            rate_changed.append({
                **_key_dict(key),
                "old_rate": before["rate"], "new_rate": after["rate"], "delta": delta,
                "old": before, "new": after,
            })
        if before["formula"] != after["formula"]:
            changed = True
            formula_changed.append({
                **_key_dict(key),
                "old_formula": before["formula"], "new_formula": after["formula"],
                "old": before, "new": after,
            })
        unchanged += 0 if changed else 1

    old_rules, new_rules = _rules(conn, old_version_id), _rules(conn, new_version_id)
    rules = {
        "added": [
            {"rule_type": t, "rule_text": x} for (t, x) in sorted((new_rules - old_rules).elements())
        ],
        "removed": [
            {"rule_type": t, "rule_text": x} for (t, x) in sorted((old_rules - new_rules).elements())
        ],
    }
    return {
        "schedule_code": old_meta["schedule_code"],
        "old_version": old_meta,
        "new_version": new_meta,
        "added": added,
        "removed": removed,
        "rate_changed": rate_changed,
        "formula_changed": formula_changed,
        "rules": rules,
        "unchanged_components": unchanged,
        "has_changes": bool(
            added or removed or rate_changed or formula_changed or rules["added"] or rules["removed"]
        ),
    }
