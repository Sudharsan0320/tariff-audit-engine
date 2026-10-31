"""Render a printable comparison: the bill as billed vs its selected schedule.

The right side is recomputed on every call (never stored) from the stored
tariff data; only the human selection is read from schedule_review_decisions.
Content is deterministic: no wall clock, only stored facts.
"""

import argparse
from decimal import Decimal
import sys
from pathlib import Path

try:
    from src import db
    from src.audit_bills import DEFAULT_MAPPING_PATH, load_mapping
    from src.audit_report import _markdown_cell, _safe_filename
    from src.schedule_review import RepricingError, ReviewRefused, get_review_state
except ImportError:
    import db
    from audit_bills import DEFAULT_MAPPING_PATH, load_mapping
    from audit_report import _markdown_cell, _safe_filename
    from schedule_review import RepricingError, ReviewRefused, get_review_state


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "data" / "reports"
OUT_OF_SCOPE = "out_of_scope"


class ComparisonRefused(ValueError):
    """The comparison cannot be produced from the stored state."""


def _row_dicts(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def load_comparison_data(conn, bill_id, mapping_path=DEFAULT_MAPPING_PATH):
    """Fetch everything the renderer needs; refuse without a selected schedule."""
    try:
        state = get_review_state(conn, bill_id)
    except ReviewRefused as error:
        raise ComparisonRefused(str(error)) from error
    selected_code = state["selected_schedule"]
    if selected_code is None:
        raise ComparisonRefused(f"Bill {bill_id} has no selected schedule.")
    candidate = next(c for c in state["candidates"] if c["schedule_code"] == selected_code)
    if candidate["status"] != "computed":
        raise ComparisonRefused(
            f"Selected schedule {selected_code} is no longer computable: "
            f"{candidate['reason']} ({candidate['blocker']})."
        )
    bill = _row_dicts(
        conn.execute(
            "SELECT b.id, b.account_number, b.bill_from, b.bill_to, b.schedule_code, "
            "b.total_billed, a.service_address "
            "FROM bills b JOIN accounts a ON a.account_number = b.account_number WHERE b.id = ?",
            (bill_id,),
        )
    )[0]
    charges = _row_dicts(
        conn.execute(
            "SELECT id, line_label, amount FROM bill_charges WHERE bill_id = ? ORDER BY id",
            (bill_id,),
        )
    )
    mapping, _ = load_mapping(mapping_path)
    return bill, charges, candidate, mapping["labels"], mapping["mapping_version"]


def _billed_lines(charges, labels):
    lines = []
    for charge in charges:
        entry = labels.get(charge["line_label"])
        if entry is None:
            raise ComparisonRefused(
                f"Billed line {charge['line_label']!r} has no mapping entry; "
                "like-for-like scope cannot be established."
            )
        lines.append(
            {
                "label": charge["line_label"],
                "amount": Decimal(str(charge["amount"])),
                "out_of_scope": entry["disposition"] == OUT_OF_SCOPE,
                "reason": entry.get("reason"),
            }
        )
    return lines


def comparison_totals(bill, charges, candidate, labels):
    lines = _billed_lines(charges, labels)
    billed_total = sum((line["amount"] for line in lines), Decimal(0))
    if bill["total_billed"] is not None and Decimal(str(bill["total_billed"])) != billed_total:
        raise ComparisonRefused(
            f"Billed lines sum to {billed_total} but the bill total is {bill['total_billed']}."
        )
    like_for_like = sum(
        (line["amount"] for line in lines if not line["out_of_scope"]), Decimal(0)
    )
    return {
        "billed_total": billed_total,
        "billed_like_for_like": like_for_like,
        "candidate_total": candidate["total"],
        "difference": candidate["total"] - like_for_like,
    }


def _money(value):
    return f"{value:,.2f}"


def _signed(value):
    return f"{value:+,.2f}"


def render_comparison(bill, charges, candidate, labels, mapping_version, decision=None):
    """Pure: stored bill facts + one computed candidate -> Markdown."""
    totals = comparison_totals(bill, charges, candidate, labels)
    lines = _billed_lines(charges, labels)
    decision = decision or candidate.get("decision") or {}
    out = [
        f"# Schedule comparison — account {bill['account_number']}",
        "",
        f"- Service address: {bill['service_address']}",
        f"- Bill period: {bill['bill_from']} through {bill['bill_to']} (bill {bill['id']})",
        f"- Billed schedule: {bill['schedule_code']}",
        f"- Compared schedule: {candidate['schedule_code']} "
        f"(tariff version {candidate['version']['version']}, effective {candidate['version']['effective_from']})",
        f"- Mapping version: {mapping_version}",
        f"- Reviewer decision: {decision.get('decision', 'n/a')} by {decision.get('reviewer', 'n/a')}"
        f" on {decision.get('decided_at', 'n/a')}",
    ]
    if decision.get("note"):
        out.append(f"- Reviewer note: {_markdown_cell(decision['note'])}")
    out += [
        "",
        "## Totals",
        "",
        "| Measure | Amount |",
        "|---|---:|",
        f"| Billed total (all printed lines) | ${_money(totals['billed_total'])} |",
        f"| Billed like-for-like (out-of-scope lines excluded) | ${_money(totals['billed_like_for_like'])} |",
        f"| Schedule {candidate['schedule_code']} computed total | ${_money(totals['candidate_total'])} |",
        f"| Difference (computed − billed like-for-like) | ${_signed(totals['difference'])} |",
        "",
        "## As billed",
        "",
        "| Line | Amount | Scope |",
        "|---|---:|---|",
    ]
    for line in lines:
        scope = "out of scope (excluded from comparison)" if line["out_of_scope"] else "in scope"
        out.append(f"| {_markdown_cell(line['label'])} | {_money(line['amount'])} | {scope} |")
    out += [
        "",
        f"## Schedule {candidate['schedule_code']} computed",
        "",
        "| Component | Amount |",
        "|---|---:|",
    ]
    for line in candidate["schedule_lines"]:
        out.append(f"| {_markdown_cell(line['component_type'])} | {_money(line['amount'])} |")
    out.append(f"| **Schedule subtotal** | **{_money(candidate['schedule_subtotal'])}** |")
    out += ["", "| Rider | Component | Amount |", "|---|---|---:|"]
    for line in candidate["rider_lines"]:
        out.append(
            f"| {_markdown_cell(line['rider'])} | {_markdown_cell(line['component_type'])} "
            f"| {_money(line['amount'])} |"
        )
    out.append(f"| **Rider subtotal** | | **{_money(candidate['rider_subtotal'])}** |")
    out += ["", "## Basis and limitations", ""]
    selection = candidate["selection"]
    if "billing_class" in selection:
        record = selection["billing_class"]
        out.append(f"- Billing class {record['selected']}: {_markdown_cell(record['basis'])}.")
    if "meter_variant" in selection:
        record = selection["meter_variant"]
        out.append(
            f"- Basic Customer Charge variant {record['selected']} "
            f"(excluded: {', '.join(record['excluded'])}): {_markdown_cell(record['basis'])}."
        )
    if "voltage_variant" in selection:
        record = selection["voltage_variant"]
        out.append(f"- Voltage variant {record['selected']} (account voltage {record['account_voltage']}).")
    out.append(
        "- Riders are the same in-force riders that were billed, each priced at the "
        f"rates stored for schedule {candidate['schedule_code']}."
    )
    excluded = {entry["rider"]: entry["reason"] for entry in candidate["riders_excluded"]}
    gcm_reason = excluded.get("RIDER G-CM", "optional contribution; no customer election is stored")
    out.append(f"- Rider G-CM is excluded from every candidate: {gcm_reason}.")
    for rider, reason in excluded.items():
        if rider != "RIDER G-CM":
            out.append(f"- {rider} not applied: {reason}.")
    for note in candidate["notes"]:
        out.append(f"- {_markdown_cell(note)}")
    for line in lines:
        if line["out_of_scope"]:
            out.append(f"- {_markdown_cell(line['label'])} is out of scope: {_markdown_cell(line['reason'])}")
    out.append("")
    return "\n".join(out)


def _totals_block(totals, schedule_code):
    return "\n".join(
        [
            f"Billed total (all lines):    ${_money(totals['billed_total'])}",
            f"Billed like-for-like:        ${_money(totals['billed_like_for_like'])}",
            f"Schedule {schedule_code} computed total: ${_money(totals['candidate_total'])}",
            f"Difference (computed - billed like-for-like): ${_signed(totals['difference'])}",
        ]
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render the billed-vs-selected-schedule comparison.")
    parser.add_argument("--bill", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"), readonly=True)
        try:
            data = load_comparison_data(conn, args.bill)
        finally:
            conn.close()
        bill, charges, candidate, labels, mapping_version = data
        markdown = render_comparison(bill, charges, candidate, labels, mapping_version)
        totals = comparison_totals(bill, charges, candidate, labels)
        output_path = REPORT_DIR / (
            f"{_safe_filename(bill['account_number'])}-{_safe_filename(bill['bill_to'])}-"
            f"schedule-{_safe_filename(candidate['schedule_code'])}-comparison.md"
        )
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    except (ComparisonRefused, RepricingError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(_totals_block(totals, candidate["schedule_code"]))
    print(f"Wrote {output_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
