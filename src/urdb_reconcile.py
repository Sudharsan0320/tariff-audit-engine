"""B7: reconcile stored tariff rates against a URDB-format CSV extract.

Read-only on both sides: the database is opened read-only and the extract is an
input that is never written. A disagreement is a flag for human review, not a
verdict that either side is wrong.

Matching rule (explicit, recorded in every report):
  * a URDB rate corresponds to a stored schedule only when its rate name contains the
    phrase "Schedule <code>" (whole token, case-insensitive), or when the caller passes
    an explicit "<rate name>=<code>" mapping;
  * the URDB rate's start/end dates must overlap the version's effective range
    (rows with no usable dates are not filtered on dates).
Compared quantities: the fixed monthly charge and flat (single period, single tier)
energy rates. Tolerance: 0.000001 on both. Our side stays Decimal.
"""

import argparse
import csv
import gzip
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys

try:
    from src import db
except ImportError:
    import db

if __package__:
    from .audit_report import _markdown_cell, _safe_filename
else:
    from audit_report import _markdown_cell, _safe_filename

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "data" / "reports"
TOLERANCE = Decimal("0.000001")
FIXED = "fixed_monthly_charge"
ENERGY = "flat_energy_rate"
MATCH_RULE = (
    'A URDB rate corresponds to a stored schedule when its rate name contains "Schedule <code>" '
    "as a whole token (case-insensitive) or the caller supplied an explicit rate-name=code mapping; "
    "its start/end dates must overlap the stored version's effective range. "
    "Nothing else is matched."
)
ENERGY_RATE_COLUMN = re.compile(r"^energyratestructure/period(\d+)/tier(\d+)rate$")
REQUIRED_COLUMNS = ("utility", "name", "sector", "fixedchargefirstmeter", "fixedchargeunits")


class UrdbFormatError(ValueError):
    """The extract is not in the URDB column layout."""


def _decimal(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def _date(value):
    text = (value or "").strip()[:10]
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


def _open_extract(path):
    path = Path(path)
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    return open(path, "r", encoding="utf-8", errors="replace", newline="")


def load_urdb_rows(path, utilities):
    """Stream the extract and keep rows whose utility equals one of the given names."""
    wanted = {name.strip().casefold() for name in utilities}
    csv.field_size_limit(10**9)
    rows = []
    with _open_extract(path) as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        rate_columns = [c for c in (reader.fieldnames or []) if ENERGY_RATE_COLUMN.match(c)]
        if missing or not rate_columns:
            raise UrdbFormatError(
                "Not a URDB-format extract; missing columns: "
                + ", ".join(missing + ([] if rate_columns else ["energyratestructure/period*/tier*rate"]))
            )
        for row in reader:
            if (row.get("utility") or "").strip().casefold() in wanted:
                rows.append(_normalize(row, rate_columns))
    return rows


def _normalize(row, rate_columns):
    rates = []
    for column in rate_columns:
        value = _decimal(row.get(column))
        if value is not None:
            period, tier = ENERGY_RATE_COLUMN.match(column).groups()
            unit = (row.get(column.replace("rate", "unit")) or "").strip()
            rates.append((int(period), int(tier), value, unit))
    rates.sort()
    return {
        "label": (row.get("label") or "").strip(),
        "utility": row["utility"].strip(),
        "name": (row["name"] or "").strip(),
        "sector": (row["sector"] or "").strip(),
        "startdate": _date(row.get("startdate")),
        "enddate": _date(row.get("enddate")),
        "fixed_charge": _decimal(row["fixedchargefirstmeter"]),
        "fixed_units": (row["fixedchargeunits"] or "").strip(),
        "energy_rates": rates,
    }


def _rows(cursor):
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, r)) for r in cursor.fetchall()]


def load_our_versions(conn, version_ids=None):
    if version_ids:
        marks = ",".join("?" for _ in version_ids)
        versions = _rows(conn.execute(
            f"SELECT id, schedule_code, version, effective_from, effective_to FROM tariff_versions "
            f"WHERE id IN ({marks}) ORDER BY id", tuple(version_ids)))
        if len(versions) != len(set(version_ids)):
            raise ValueError("Some requested version ids do not exist.")
    else:
        versions = _rows(conn.execute(
            "SELECT id, schedule_code, version, effective_from, effective_to FROM tariff_versions "
            "WHERE status = 'active' ORDER BY id"))
    for version in versions:
        version["effective_from"] = str(version["effective_from"])
        version["effective_to"] = str(version["effective_to"]) if version["effective_to"] else None
        components = _rows(conn.execute(
            "SELECT id, component_type, unit, variant, rate, tier_min, tier_max FROM tariff_components "
            "WHERE tariff_version_id = ? AND billing_category IS NULL AND rate IS NOT NULL ORDER BY id",
            (version["id"],)))
        version["fixed"] = []
        version["energy"] = []
        for c in components:
            rate = Decimal(str(c["rate"]))
            entry = {"component_id": c["id"], "component_type": c["component_type"],
                     "variant": c["variant"], "rate": rate}
            if c["component_type"] == "basic_customer_charge" and c["unit"] == "usd_per_unit_month":
                version["fixed"].append(entry)
            elif (c["unit"] == "usd_per_kwh" and c["tier_min"] is None and c["tier_max"] is None
                  and not c["component_type"].startswith("rider")):
                version["energy"].append(entry)
    return versions


def _correspondence(urdb_name, version, explicit):
    code = version["schedule_code"]
    if explicit.get(urdb_name.casefold()) == code:
        return f'explicit mapping "{urdb_name}" = {code}'
    pattern = r"(?<![A-Za-z0-9])schedule\s+" + re.escape(code) + r"(?![A-Za-z0-9])"
    if re.search(pattern, urdb_name, re.IGNORECASE):
        return f'rate name contains "Schedule {code}"'
    return None


def _overlaps(row, version):
    start, end = row["startdate"], row["enddate"]
    ours_from, ours_to = version["effective_from"], version["effective_to"]
    if start and ours_to and start >= ours_to:
        return False
    if end and end < ours_from:
        return False
    return True


def _nearest(theirs, ours):
    return min(ours, key=lambda o: (abs(o["rate"] - theirs), o["component_id"]))


def _compare(kind, theirs, ours, ctx, agree, disagree, unmatched_ours, unmatched_theirs):
    base = dict(ctx, kind=kind)
    if theirs is None and not ours:
        return
    if theirs is None:
        for o in ours:
            unmatched_ours.append(dict(base, reason="URDB rate carries no comparable value",
                                       **_ours(o)))
        return
    if not ours:
        unmatched_theirs.append(dict(base, theirs=theirs, reason="no stored component to compare"))
        return
    hits = [o for o in ours if abs(o["rate"] - theirs) <= TOLERANCE]
    if hits:
        agree.append(dict(base, theirs=theirs, **_ours(hits[0])))
        for o in ours:
            if o is not hits[0] and o not in hits:
                unmatched_ours.append(dict(base, reason="not the stored component the URDB value agrees with",
                                           **_ours(o)))
    else:
        near = _nearest(theirs, ours)
        disagree.append(dict(base, theirs=theirs, delta=theirs - near["rate"], **_ours(near)))
        for o in ours:
            if o is not near:
                unmatched_ours.append(dict(base, reason="other stored component, not the nearest to the URDB value",
                                           **_ours(o)))


def _ours(entry):
    return {"ours": entry["rate"], "component_id": entry["component_id"],
            "component_type": entry["component_type"], "variant": entry["variant"]}


def reconcile(conn, urdb_rows, version_ids=None, explicit_map=None):
    explicit = {k.casefold(): v for k, v in (explicit_map or {}).items()}
    versions = load_our_versions(conn, version_ids)
    agree, disagree, unmatched_ours, unmatched_theirs = [], [], [], []
    matched_versions = set()
    used_rows = set()

    for index, row in enumerate(urdb_rows):
        for version in versions:
            basis = _correspondence(row["name"], version, explicit)
            if basis is None:
                continue
            ctx = {"schedule_code": version["schedule_code"], "version_id": version["id"],
                   "urdb_name": row["name"], "urdb_label": row["label"], "basis": basis}
            used_rows.add(index)
            if not _overlaps(row, version):
                unmatched_theirs.append(dict(ctx, kind="rate", reason=(
                    f"URDB dates {row['startdate']}..{row['enddate']} do not overlap "
                    f"version {version['effective_from']}..{version['effective_to']}")))
                continue
            matched_versions.add(version["id"])
            fixed = row["fixed_charge"] if row["fixed_units"] in ("", "$/month") else None
            if row["fixed_charge"] is not None and fixed is None:
                unmatched_theirs.append(dict(ctx, kind=FIXED, theirs=row["fixed_charge"],
                                             reason=f"fixed charge unit {row['fixed_units']!r} is not $/month"))
            else:
                _compare(FIXED, fixed, version["fixed"], ctx, agree, disagree, unmatched_ours, unmatched_theirs)
            rates = row["energy_rates"]
            if len(rates) == 1 and rates[0][3] in ("", "kWh"):
                _compare(ENERGY, rates[0][2], version["energy"], ctx, agree, disagree,
                         unmatched_ours, unmatched_theirs)
            elif rates:
                unmatched_theirs.append(dict(ctx, kind=ENERGY, reason=(
                    f"energy structure is not flat ({len(rates)} period/tier rates or non-kWh unit)")))
            else:
                for o in version["energy"]:
                    unmatched_ours.append(dict(ctx, kind=ENERGY, reason="URDB rate carries no energy rate",
                                               **_ours(o)))

    for index, row in enumerate(urdb_rows):
        if index not in used_rows:
            unmatched_theirs.append({"kind": "rate", "urdb_name": row["name"], "urdb_label": row["label"],
                                     "reason": "no stored schedule corresponds to this URDB rate name"})
    for version in versions:
        if version["id"] in matched_versions:
            continue
        for kind, entries in ((FIXED, version["fixed"]), (ENERGY, version["energy"])):
            for o in entries:
                unmatched_ours.append({"kind": kind, "schedule_code": version["schedule_code"],
                                       "version_id": version["id"],
                                       "reason": "no URDB rate corresponds to this schedule", **_ours(o)})

    return {
        "match_rule": MATCH_RULE,
        "tolerance": str(TOLERANCE),
        "versions_compared": [v["id"] for v in versions],
        "urdb_rows_considered": len(urdb_rows),
        "counts": {"agree": len(agree), "disagree": len(disagree),
                   "unmatched_ours": len(unmatched_ours), "unmatched_theirs": len(unmatched_theirs)},
        "agree": agree, "disagree": disagree,
        "unmatched_ours": unmatched_ours, "unmatched_theirs": unmatched_theirs,
    }


def _plain(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def counts_block(result):
    c = result["counts"]
    return (f"agree: {c['agree']}\ndisagree: {c['disagree']}\n"
            f"unmatched_ours: {c['unmatched_ours']}\nunmatched_theirs: {c['unmatched_theirs']}")


def render_markdown(result, utilities, source_name):
    result = _plain(result)
    lines = ["# URDB reconciliation", "",
             f"- Utility filter: {', '.join(utilities)}", f"- Extract: {source_name}",
             f"- URDB rows considered: {result['urdb_rows_considered']}",
             f"- Stored versions compared: {len(result['versions_compared'])}",
             f"- Tolerance: {result['tolerance']}", f"- Matching rule: {result['match_rule']}", "",
             "A disagreement is a flag for review, not a verdict.", "", "## Counts", ""]
    lines += [f"- {k}: {v}" for k, v in result["counts"].items()]
    if result["urdb_rows_considered"] == 0:
        lines += ["", "No URDB rows matched the utility filter; zero matches is a truthful result."]
    for key, title, cols in (
        ("disagree", "Disagreements", ("schedule_code", "kind", "urdb_name", "ours", "theirs", "delta", "basis")),
        ("agree", "Agreements", ("schedule_code", "kind", "urdb_name", "ours", "theirs", "basis")),
        ("unmatched_ours", "Unmatched (ours)", ("schedule_code", "kind", "ours", "reason")),
        ("unmatched_theirs", "Unmatched (theirs)", ("urdb_name", "kind", "theirs", "reason")),
    ):
        lines += ["", f"## {title}", ""]
        if not result[key]:
            lines.append("None.")
            continue
        lines += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for item in result[key]:
            lines.append("| " + " | ".join(_markdown_cell(item.get(c, "")) for c in cols) + " |")
    return "\n".join(lines) + "\n"


def write_reports(result, utilities, csv_path, report_dir=None):
    report_dir = Path(report_dir) if report_dir else REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = "urdb-" + _safe_filename("-".join(utilities))
    json_path, md_path = report_dir / f"{stem}-reconciliation.json", report_dir / f"{stem}-reconciliation.md"
    payload = _plain(dict(result, utilities=list(utilities), extract=Path(csv_path).name))
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(result, utilities, Path(csv_path).name), encoding="utf-8")
    return json_path, md_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reconcile stored rates against a URDB-format CSV extract.")
    parser.add_argument("--csv", required=True, help="URDB CSV extract (.csv or .csv.gz); never modified")
    parser.add_argument("--utility", action="append", required=True, help="URDB utility name (repeatable)")
    parser.add_argument("--versions", help="comma-separated tariff version ids (default: all active)")
    parser.add_argument("--map", action="append", default=[], help='explicit "URDB rate name=schedule code"')
    args = parser.parse_args(argv)
    try:
        explicit = dict(item.rsplit("=", 1) for item in args.map)
        ids = [int(x) for x in args.versions.split(",")] if args.versions else None
        rows = load_urdb_rows(args.csv, args.utility)
        conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"), readonly=True)
        try:
            result = reconcile(conn, rows, ids, explicit)
        finally:
            conn.close()
        json_path, md_path = write_reports(result, args.utility, args.csv)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(counts_block(result))
    print(f"Wrote {json_path}\nWrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
