"""Render deterministic Markdown reports from stored audit results."""

import argparse
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import json
import re
import sys
from pathlib import Path

try:
    from src import audit_bills, db
except ImportError:
    import audit_bills
    import db


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAPPING_PATH = ROOT / "docs" / "bill-label-mapping.json"
PIPELINE_MANIFEST_PATH = ROOT / "docs" / "pipeline-manifest.json"
REPORT_DIR = ROOT / "data" / "reports"


class AuditReportError(ValueError):
    """Stored audit data cannot produce a complete, reconciled report."""


class AuditRunNotFound(LookupError):
    """No requested audit run exists."""


def _json_value(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def _row_dict(cursor, row):
    return dict(zip((column[0] for column in cursor.description), row))


def _markdown_cell(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
    )


def _decimal(value, context):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise AuditReportError(f"{context} is not a valid decimal: {value!r}.") from error


def _numeric_text(value):
    number = _decimal(value, "Stored numeric value")
    return format(number.normalize(), "f")


def _display_rate(value):
    # Display only: the source PDFs print rates to at most 4 decimal places in cents.
    rate = _decimal(value, "Stored rate").quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    return format(rate.normalize(), "f")


def _determinant_type(mapping_entry):
    return {
        "on_peak": "on_peak_usage",
        "off_peak": "off_peak_usage",
    }.get(mapping_entry.get("variant"), "total_consumption")


def _load_tariff_sources():
    manifest = json.loads(PIPELINE_MANIFEST_PATH.read_text(encoding="utf-8"))
    sources = manifest.get("tariff_pdfs")

    def flatten(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [item for child in value.values() for item in flatten(child)]
        if isinstance(value, list):
            return [item for child in value for item in flatten(child)]
        raise AuditReportError("Pipeline manifest contains an invalid tariff_pdfs entry.")

    return flatten(sources)


def load_report_data(conn, bill_id=None, run_id=None, mapping_path=DEFAULT_MAPPING_PATH):
    """Load one stored run and the evidence needed to render it."""
    if (bill_id is None) == (run_id is None):
        raise ValueError("Provide exactly one of bill_id or run_id.")

    if run_id is not None:
        where, params = "r.id = ?", (run_id,)
        ordering = ""
    else:
        where, params = "r.bill_id = ?", (bill_id,)
        ordering = "ORDER BY r.created_at DESC, r.id DESC"

    cursor = conn.execute(
        f"""
        SELECT r.*, b.account_number, b.bill_from, b.bill_to, b.schedule_code,
               b.total_billed, b.source_file, a.service_address
        FROM audit_runs r
        JOIN bills b ON b.id = r.bill_id
        JOIN accounts a ON a.account_number = b.account_number
        WHERE {where}
        {ordering}
        LIMIT 1
        """,
        params,
    )
    row = cursor.fetchone()
    if row is None:
        raise AuditRunNotFound(
            f"No audit run found for bill {bill_id}."
            if bill_id is not None
            else f"No audit run {run_id}."
        )
    run = _row_dict(cursor, row)
    run["report"] = _json_value(run.pop("report_json"))

    lines_cursor = conn.execute(
        """
        SELECT l.*, c.rate AS component_rate, c.unit AS component_unit,
               c.variant AS component_variant, c.source_page, c.source_cell,
               c.tariff_version_id, v.schedule_code AS component_schedule_code,
               v.version AS component_version,
               v.effective_from AS component_effective_from,
               v.effective_to AS component_effective_to
        FROM audit_lines l
        LEFT JOIN tariff_components c ON c.id = l.tariff_component_id
        LEFT JOIN tariff_versions v ON v.id = c.tariff_version_id
        WHERE l.audit_run_id = ?
        ORDER BY l.id
        """,
        (run["id"],),
    )
    lines = [_row_dict(lines_cursor, item) for item in lines_cursor.fetchall()]
    components = {}
    for line in lines:
        line["detail"] = _json_value(line["detail"]) if line["detail"] else {}
        if line["tariff_component_id"] is not None:
            components[line["tariff_component_id"]] = {
                "rate": line["component_rate"],
                "unit": line["component_unit"],
                "variant": line["component_variant"],
                "source_page": line["source_page"],
                "source_cell": line["source_cell"],
                "tariff_version_id": line["tariff_version_id"],
                "schedule_code": line["component_schedule_code"],
                "version": line["component_version"],
                "effective_from": line["component_effective_from"],
                "effective_to": line["component_effective_to"],
            }

    missing_lines = [
        line
        for line in lines
        if line["disposition"] == "direct" and line["verdict"] == "MISSING"
    ]
    mapping = None
    if missing_lines:
        mapping, _ = audit_bills.load_mapping(mapping_path)
        if mapping["mapping_version"] != run["mapping_version"]:
            raise AuditReportError(
                "Cannot reconstruct MISSING lines: checked-in mapping version "
                f"{mapping['mapping_version']!r} does not match run mapping version "
                f"{run['mapping_version']!r}."
            )
        for line in missing_lines:
            entry = mapping["labels"].get(line["line_label"])
            if not entry or entry.get("disposition") != "direct":
                raise AuditReportError(
                    f"Cannot reconstruct MISSING line {line['line_label']!r}: "
                    "the run-version mapping has no direct mapping for it."
                )
            determinant_type = _determinant_type(entry)
            determinant_rows = conn.execute(
                """
                SELECT value FROM bill_determinants
                WHERE bill_id = ? AND determinant_type = ?
                """,
                (run["bill_id"], determinant_type),
            ).fetchall()
            line["_determinant_values"] = [item[0] for item in determinant_rows]

    run["tariff_sources"] = _load_tariff_sources()
    return run, lines, components, mapping


def _summary_block(report, lines):
    totals = report.get("like_for_like_totals")
    if not isinstance(totals, dict):
        raise AuditReportError("The audit run has no stored like-for-like totals.")
    if any(totals.get(key) is None for key in ("billed", "expected", "delta")):
        raise AuditReportError("The audit run has incomplete like-for-like totals.")
    counts = Counter(line["verdict"] for line in lines)
    verdicts = ", ".join(f"{name}: {counts[name]}" for name in sorted(counts))
    return "\n".join(
        (
            "## Summary",
            "",
            f"- Billed total (audited lines): **${totals['billed']}**",
            f"- Recomputed like-for-like total: **${totals['expected']}**",
            f"- Difference: **${totals['delta']}**",
            f"- Verdict counts: {verdicts or 'none'}",
        )
    )


def _reconciliation_check(report, lines):
    total_delta = _decimal(
        report.get("like_for_like_totals", {}).get("delta"),
        "Stored audit total delta",
    )
    line_delta = Decimal(0)
    for line in lines:
        if not isinstance(line.get("verdict"), str) or not line["verdict"].strip():
            raise AuditReportError(
                f"Audit line {line.get('id', '?')} has no verdict."
            )
        if line.get("disposition") != "out_of_scope" and line.get("delta") is None:
            raise AuditReportError(
                f"Audit line {line.get('id', '?')} has no stored delta."
            )
        if line.get("delta") is not None:
            line_delta += _decimal(
                line["delta"], f"Delta for line {line.get('line_label')!r}"
            )
    if line_delta != total_delta:
        raise AuditReportError(
            f"Audit line deltas do not reconcile: lines sum to {line_delta}, "
            f"run total delta is {total_delta}."
        )


def _versions_for_report(report, components):
    versions = {}
    base = report.get("tariff_version")
    if isinstance(base, dict):
        base_id = base.get("id")
        versions[base_id] = {
            "schedule_code": report.get("billed_schedule_code"),
            "version": base.get("version"),
            "effective_from": base.get("effective_from"),
            "effective_to": base.get("effective_to"),
        }
    for component in components.values():
        version_id = component.get("tariff_version_id")
        if version_id is not None:
            versions[version_id] = {
                "schedule_code": component.get("schedule_code"),
                "version": component.get("version"),
                "effective_from": component.get("effective_from"),
                "effective_to": component.get("effective_to"),
            }
    if any(not item.get("schedule_code") for item in versions.values()):
        raise AuditReportError("A tariff version used by this run has no schedule code.")
    return sorted(
        versions.values(),
        key=lambda item: (str(item["schedule_code"]), str(item["version"])),
    )


def _line_cells(line, components, mapping):
    disposition = line.get("disposition")
    detail = line.get("detail") or {}
    if disposition == "direct":
        component_id = line.get("tariff_component_id")
        component = components.get(component_id)
        if component is None:
            raise AuditReportError(
                f"Direct line {line['line_label']!r} has no joined tariff component."
            )
        page = component.get("source_page")
        locator = component.get("source_cell")
        if (
            not isinstance(page, int)
            or isinstance(page, bool)
            or page < 1
            or not isinstance(locator, str)
            or not locator.strip()
        ):
            raise AuditReportError(
                f"Direct line {line['line_label']!r} has no stored tariff citation."
            )
        missing = line.get("verdict") == "MISSING"
        rate = component.get("rate") if missing else detail.get("rate")
        unit = component.get("unit") if missing else detail.get("unit")
        if rate is None or not unit:
            raise AuditReportError(
                f"Direct line {line['line_label']!r} has no stored applied rate and unit."
            )
        if missing:
            if mapping is None or mapping.get("mapping_version") != line.get(
                "_mapping_version"
            ):
                raise AuditReportError(
                    f"Cannot reconstruct MISSING line {line['line_label']!r}: "
                    "the run-version mapping is unavailable."
                )
            entry = mapping["labels"].get(line["line_label"])
            if not entry or entry.get("disposition") != "direct":
                raise AuditReportError(
                    f"Cannot reconstruct MISSING line {line['line_label']!r}: "
                    "the run-version mapping has no direct mapping for it."
                )
            determinant_type = _determinant_type(entry)
            values = line.get("_determinant_values", [])
            if len(values) != 1 or values[0] is None:
                raise AuditReportError(
                    f"Cannot reconstruct MISSING line {line['line_label']!r}: "
                    f"determinant {determinant_type!r} resolves to "
                    f"{len(values)} stored values; exactly one is required."
                )
            determinant = f"{determinant_type} = {_numeric_text(values[0])}"
            if unit in {"usd_per_month", "usd_per_unit_month"}:
                determinant = "Not used (fixed monthly charge)"
        else:
            determinant_type = detail.get("determinant_type")
            determinant_value = detail.get("determinant_value")
            if not determinant_type or determinant_value in (None, ""):
                raise AuditReportError(
                    f"Direct line {line['line_label']!r} has no stored determinant."
                )
            determinant = f"{determinant_type} = {determinant_value}"
            if unit in {"usd_per_month", "usd_per_unit_month"}:
                determinant = "Not used (fixed monthly charge)"
        citation = f"p. {page}; {locator}"
        return (
            f"{_display_rate(rate)} {unit}",
            determinant,
            citation,
        )
    if disposition == "zero_expected":
        reason = detail.get("evidence")
        if not reason:
            raise AuditReportError(
                f"Zero-expected line {line['line_label']!r} has no stored disposition reason."
            )
        return "—", "—", f"Zero expected: {reason}"
    if disposition == "out_of_scope":
        reason = detail.get("reason")
        if not reason:
            raise AuditReportError(
                f"Out-of-scope line {line['line_label']!r} has no stored disposition reason."
            )
        return "—", "—", f"Out of scope: {reason}"
    raise AuditReportError(
        f"Audit line {line.get('line_label')!r} has unknown disposition {disposition!r}."
    )


def _notes(report, lines):
    notes = []
    for line in lines:
        if line.get("disposition") == "out_of_scope":
            detail = line.get("detail") or {}
            notes.append(
                f"**{line['line_label']} out of scope:** {detail.get('reason')}"
            )
    for component in report.get("conditional_components", []):
        notes.append(
            f"**Conditional {component.get('component_type', 'tariff component')}:** "
            f"{component.get('reason', 'The audit records this component as conditional.')}"
        )
    minimum = report.get("minimum_charge")
    if minimum:
        notes.append(
            f"**Minimum charge ({minimum.get('status', 'recorded')}):** "
            f"{minimum.get('reason', 'No further reason was recorded.')}"
        )
    alternative = report.get("alternative_schedule")
    if alternative and alternative.get("schedule_code") == "110":
        notes.append(
            f"**Schedule 110 informational only:** "
            f"{alternative.get('eligibility_note', 'Eligibility is not established.')}"
        )
    return notes


def render_report(run, lines, components, mapping=None):
    """Purely render the supplied stored audit data as deterministic Markdown."""
    report = run.get("report")
    if not isinstance(report, dict):
        raise AuditReportError("The audit run has no valid stored report JSON.")
    lines = [dict(line) for line in lines]
    components = dict(components)
    for line in lines:
        line["detail"] = line.get("detail") or {}
        if line.get("disposition") == "direct" and line.get("verdict") == "MISSING":
            line["_mapping_version"] = run.get("mapping_version")
    _reconciliation_check(report, lines)

    account_number = run.get("account_number") or report.get("account_number")
    service_address = run.get("service_address")
    bill_from = report.get("bill_from")
    bill_to = report.get("bill_to")
    schedule_code = report.get("billed_schedule_code")
    created_at = run.get("created_at")
    if not all((account_number, service_address, bill_from, bill_to, schedule_code, created_at)):
        raise AuditReportError("The audit run is missing required report header fields.")
    if not run.get("tariff_snapshot_hash"):
        raise AuditReportError("The audit run has no tariff snapshot hash.")

    versions = _versions_for_report(report, components)
    version_lines = [
        f"- Schedule {item['schedule_code']} v{item['version']} "
        f"(effective {item['effective_from']} through {item['effective_to'] or 'open-ended'})"
        for item in versions
    ]
    metadata = [
        "# Audit report",
        "",
        f"- **Account number:** {_markdown_cell(account_number)}",
        f"- **Service address:** {_markdown_cell(service_address)}",
        f"- **Bill period:** {bill_from} through {bill_to}",
        f"- **Schedule code:** {_markdown_cell(schedule_code)}",
        "- **Tariff version(s) in force:**",
        *(version_lines or ["- No tariff versions recorded."]),
        f"- **Mapping version:** {_markdown_cell(run['mapping_version'])}",
        f"- **Run ID:** {run['id']}",
        f"- **Run created at:** {created_at}",
        f"- **Tariff snapshot hash:** `{run['tariff_snapshot_hash']}`",
    ]

    output = metadata + [
        "",
        _summary_block(report, lines),
        "",
        "## Line items",
        "",
        "| Label | Billed | Expected | Delta | Verdict | Applied rate / unit | Determinant used | Citation / disposition |",
        "|---|---:|---:|---:|---|---|---|---|",
    ]
    for line in lines:
        rate, determinant, evidence = _line_cells(line, components, mapping)
        values = (
            line.get("line_label", ""),
            line.get("billed_amount") if line.get("billed_amount") is not None else "—",
            line.get("expected_amount") if line.get("expected_amount") is not None else "—",
            line.get("delta") if line.get("delta") is not None else "—",
            line["verdict"],
            rate,
            determinant,
            evidence,
        )
        output.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")

    notes = _notes(report, lines)
    output.extend(("", "## Notes", ""))
    output.extend(f"- {note}" for note in notes or ["No additional limitations were recorded."])
    tariff_sources = run.get("tariff_sources")
    if not tariff_sources:
        raise AuditReportError("No tariff source documents are recorded in the pipeline manifest.")
    bill_source = run.get("source_file")
    if not bill_source:
        raise AuditReportError("The bill PDF source path is not stored.")
    sources_text = ", ".join(f"`{_markdown_cell(path)}`" for path in tariff_sources)
    output.extend(
        (
            "",
            "---",
            f"Evidence source documents: tariff PDF(s) {sources_text}; "
            f"bill PDF `{_markdown_cell(bill_source)}`.",
        )
    )
    return "\n".join(output) + "\n"


def _safe_filename(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-") or "unknown"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render a stored audit report as Markdown.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--bill", type=int, help="Render the bill's latest audit run.")
    selection.add_argument("--run", type=int, help="Render a specific audit run.")
    args = parser.parse_args(argv)

    try:
        conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"), readonly=True)
        try:
            run, lines, components, mapping = load_report_data(
                conn, bill_id=args.bill, run_id=args.run
            )
        finally:
            conn.close()
        markdown = render_report(run, lines, components, mapping)
        output_path = REPORT_DIR / (
            f"{_safe_filename(run['account_number'])}-"
            f"{_safe_filename(run['bill_to'])}-run{run['id']}.md"
        )
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        print(_summary_block(run["report"], lines))
        return 0
    except AuditRunNotFound as error:
        print(str(error), file=sys.stderr)
        return 1
    except AuditReportError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
