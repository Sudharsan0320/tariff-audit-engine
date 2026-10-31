"""Tariff change alert: the B3 diff plus the impact on one sample bill, as Markdown."""

import argparse
from decimal import ROUND_HALF_UP, Decimal
import sys
from pathlib import Path

try:
    from src import db
except ImportError:
    import db

if __package__:
    from .audit_bills import DEFAULT_MAPPING_PATH, _fetch_bill, load_mapping
    from .audit_report import _display_rate, _markdown_cell, _safe_filename
    from .schedule_review import RepricingError, _history, price_schedule
    from .tariff_diff import DiffError, diff_versions
else:
    from audit_bills import DEFAULT_MAPPING_PATH, _fetch_bill, load_mapping
    from audit_report import _display_rate, _markdown_cell, _safe_filename
    from schedule_review import RepricingError, _history, price_schedule
    from tariff_diff import DiffError, diff_versions

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "data" / "reports"
CENT = Decimal("0.01")


class AlertError(ValueError):
    """The alert cannot be built from the stored data."""


def _money(value):
    return format(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _signed(value):
    text = _money(value)
    return text if text.startswith("-") or Decimal(text) == 0 else "+" + text


def _rate(value):
    return "—" if value is None else _display_rate(value)


def _cell(value):
    return "—" if value is None else _markdown_cell(value)


def _is_rider(conn, schedule_code):
    row = conn.execute(
        """
        SELECT COUNT(*) FROM tariff_versions v JOIN tariff_components c ON c.tariff_version_id = v.id
        WHERE v.schedule_code = ? AND c.billing_category IS NOT NULL
        """,
        (schedule_code,),
    ).fetchone()
    return row[0] > 0


def sample_bill_id_for(conn, schedule_code):
    """Most recent bill on the schedule (or, for a rider, on a schedule it prices)."""
    row = conn.execute(
        "SELECT id FROM bills WHERE schedule_code = ? ORDER BY bill_to DESC, id DESC LIMIT 1",
        (schedule_code,),
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        """
        SELECT b.id FROM bills b
        WHERE b.schedule_code IN (
            SELECT c.billing_category FROM tariff_versions v
            JOIN tariff_components c ON c.tariff_version_id = v.id
            WHERE v.schedule_code = ? AND c.billing_category IS NOT NULL)
        ORDER BY b.bill_to DESC, b.id DESC LIMIT 1
        """,
        (schedule_code,),
    ).fetchone()
    return row[0] if row else None


def _lines(candidate):
    counts, lines = {}, {}
    for scope, rows in (("schedule", candidate["schedule_lines"]), ("rider", candidate["rider_lines"])):
        for row in rows:
            name = f"{row['rider']} · {row['component_type']}" if scope == "rider" else row["component_type"]
            counts[name] = counts.get(name, 0) + 1
            lines[(name, counts[name])] = row["amount"]
    return lines


def _impact(conn, diff, bill_id):
    code = diff["schedule_code"]
    old_id, new_id = diff["old_version"]["id"], diff["new_version"]["id"]
    bill = _fetch_bill(conn, bill_id)
    identity = load_mapping(DEFAULT_MAPPING_PATH)[0]["tariff_identity"]
    history = _history(conn, bill)
    rider = _is_rider(conn, code)
    target = bill["schedule_code"] if rider else code
    old = price_schedule(conn, bill, history, identity, target, {code: old_id})
    new = price_schedule(conn, bill, history, identity, target, {code: new_id})
    return bill, rider, target, old, new


def _impact_lines(diff, bill, rider, target, old, new):
    out = [
        "## Sample-bill impact",
        "",
        f"- Sample bill: {bill['id']} — account {bill['account_number']}, "
        f"{bill['bill_from']} through {bill['bill_to']}, billed schedule {bill['schedule_code']}",
        f"- Repriced under schedule {target} with {'rider ' if rider else 'schedule '}"
        f"{diff['schedule_code']} pinned to version {diff['old_version']['version']} "
        f"(old) and version {diff['new_version']['version']} (new); the bill's period need not "
        "fall inside either version's effective dates.",
        "- The schedule and the same in-force riders that were billed are repriced with the "
        "engine's rounding (half-up cents per line), so a line's change can differ by a cent from "
        "(new rate − old rate) × determinant; Rider G-CM is excluded (no customer election is stored).",
        "",
    ]
    for label, candidate in (("old", old), ("new", new)):
        if candidate["status"] != "computed":
            out += [
                f"Impact not computed: the {label} version reprices to `{candidate['reason']}` "
                f"({_markdown_cell(candidate['blocker'])}). No partial total is shown.",
                "",
            ]
            return out
    if rider and diff["schedule_code"] not in {r["rider"] for r in new["riders_applied"]}:
        out += [f"Rider {diff['schedule_code']} does not price schedule {target}; the impact on this bill is $0.00.", ""]
    out += [
        "| Measure | Old | New | Change |",
        "|---|---:|---:|---:|",
        f"| Schedule subtotal | {_money(old['schedule_subtotal'])} | {_money(new['schedule_subtotal'])} "
        f"| {_signed(new['schedule_subtotal'] - old['schedule_subtotal'])} |",
        f"| Rider subtotal | {_money(old['rider_subtotal'])} | {_money(new['rider_subtotal'])} "
        f"| {_signed(new['rider_subtotal'] - old['rider_subtotal'])} |",
        f"| **Total** | **{_money(old['total'])}** | **{_money(new['total'])}** "
        f"| **{_signed(new['total'] - old['total'])}** |",
        "",
    ]
    old_lines, new_lines = _lines(old), _lines(new)
    changed = [
        (key, old_lines.get(key), new_lines.get(key))
        for key in sorted(old_lines.keys() | new_lines.keys())
        if old_lines.get(key) != new_lines.get(key)
    ]
    if changed:
        out += ["| Line | Old | New | Change |", "|---|---:|---:|---:|"]
        for (name, _), before, after in changed:
            delta = (after or Decimal(0)) - (before or Decimal(0))
            out.append(
                f"| {_markdown_cell(name)} | {_money(before) if before is not None else '—'} "
                f"| {_money(after) if after is not None else '—'} | {_signed(delta)} |"
            )
        out.append("")
    return out


def _citation(side):
    page, cell = side["source_page"], side["source_cell"]
    return f"p. {page}, {_markdown_cell(cell)}" if page is not None or cell else "—"


def _label(row):
    parts = [row["component_type"]] + [
        str(row[f]) for f in ("variant", "billing_class", "season", "billing_category") if row[f] is not None
    ]
    if row["tier_min"] is not None or row["tier_max"] is not None:
        parts.append(f"tier {row['tier_min']}–{row['tier_max']}")
    return _markdown_cell(" · ".join(parts))


def render_alert(diff, impact_lines):
    old, new = diff["old_version"], diff["new_version"]
    out = [
        f"# Tariff change alert — schedule {diff['schedule_code']}",
        "",
        f"- Schedule: {diff['schedule_code']} ({new['utility']}, {new['state']})",
        f"- Version: {old['version']} → {new['version']} (ids {old['id']} → {new['id']})",
        f"- Old version effective: {old['effective_from']} to {old['effective_to'] or 'open'}",
        f"- New version effective: {new['effective_from']} to {new['effective_to'] or 'open'}",
        "",
        "## Changes",
        "",
    ]
    if not diff["has_changes"]:
        out += ["No component or rule differs between the two versions.", ""]
    if diff["rate_changed"]:
        out += [
            "### Rate changes",
            "",
            "| Component | Unit | Old rate | New rate | Change | Old citation | New citation |",
            "|---|---|---:|---:|---:|---|---|",
        ]
        for row in diff["rate_changed"]:
            delta = "—" if row["delta"] is None else ("+" if row["delta"] > 0 else "") + _rate(row["delta"])
            out.append(
                f"| {_label(row)} | {_cell(row['unit'])} | {_rate(row['old_rate'])} | {_rate(row['new_rate'])} "
                f"| {delta} | {_citation(row['old'])} | {_citation(row['new'])} |"
            )
        out.append("")
    if diff["formula_changed"]:
        out += ["### Formula changes", "", "| Component | Old formula | New formula | Old citation | New citation |", "|---|---|---|---|---|"]
        for row in diff["formula_changed"]:
            out.append(
                f"| {_label(row)} | {_cell(row['old_formula'])} | {_cell(row['new_formula'])} "
                f"| {_citation(row['old'])} | {_citation(row['new'])} |"
            )
        out.append("")
    for title, key, side in (("Components added", "added", "new"), ("Components removed", "removed", "old")):
        if diff[key]:
            out += [f"### {title}", "", "| Component | Unit | Rate | Formula | Citation |", "|---|---|---:|---|---|"]
            for row in diff[key]:
                out.append(
                    f"| {_label(row)} | {_cell(row['unit'])} | {_rate(row['rate'])} "
                    f"| {_cell(row['formula'])} | {_citation(row)} |"
                )
            out.append("")
    for title, key in (("Rules added", "added"), ("Rules removed", "removed")):
        if diff["rules"][key]:
            out += [f"### {title}", ""]
            out += [f"- `{r['rule_type']}`: {_markdown_cell(r['rule_text'])}" for r in diff["rules"][key]]
            out.append("")
    out += [f"Unchanged components: {diff['unchanged_components']}.", ""]
    return "\n".join(out + impact_lines) + "\n"


def build_alert(conn, old_version_id, new_version_id, sample_bill_id=None):
    try:
        diff = diff_versions(conn, old_version_id, new_version_id)
    except DiffError as error:
        raise AlertError(str(error)) from error
    if sample_bill_id is None:
        impact = ["## Sample-bill impact", "", "No sample bill is stored for this schedule; no impact is computed.", ""]
    else:
        try:
            impact = _impact_lines(diff, *_impact(conn, diff, sample_bill_id))
        except (ValueError, RepricingError) as error:
            raise AlertError(str(error)) from error
    return render_alert(diff, impact)


def alert_path(conn, old_version_id, new_version_id, report_dir=None):
    versions = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT id, schedule_code, version FROM tariff_versions WHERE id IN (?, ?)",
            (old_version_id, new_version_id),
        ).fetchall()
    }
    code, old_no = versions[old_version_id]
    new_no = versions[new_version_id][1]
    return Path(report_dir or REPORT_DIR) / f"{_safe_filename(code)}-v{old_no}-to-v{new_no}-alert.md"


def write_alert(conn, old_version_id, new_version_id, sample_bill_id=None, report_dir=None):
    markdown = build_alert(conn, old_version_id, new_version_id, sample_bill_id)
    path = alert_path(conn, old_version_id, new_version_id, report_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write a tariff change alert for one version pair.")
    parser.add_argument("--old", type=int, required=True)
    parser.add_argument("--new", type=int, required=True)
    parser.add_argument("--bill", type=int, required=True, help="Sample bill for the impact section.")
    args = parser.parse_args(argv)
    try:
        conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"), readonly=True)
        try:
            path = write_alert(conn, args.old, args.new, args.bill)
        finally:
            conn.close()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
