"""B9: a parse-review queue built only from explicit stored signals.

`scan_for_review` looks for four data patterns and queues each hit once. A resolution
records that a human looked; it never edits the component or rule. Fixes flow through
the parsers and a re-run.
"""

import argparse
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys

try:
    from src import db
except ImportError:
    import db

if __package__:
    from .billing_engine import evaluate_component
    from .validate_draft import NON_ORDINARY_TOKENS
else:
    from billing_engine import evaluate_component
    from validate_draft import NON_ORDINARY_TOKENS

ROOT = Path(__file__).resolve().parent.parent

VERIFY_MARKER = "verify_marker"
UNEVALUABLE_FORMULA = "unevaluable_formula"
DRAFT_MISSING_CITATION = "draft_missing_citation"
ZERO_RATE = "zero_rate_unexpected"
SIGNALS = (VERIFY_MARKER, UNEVALUABLE_FORMULA, DRAFT_MISSING_CITATION, ZERO_RATE)
MARKER = re.compile(r"\b(verify|garbled)\b", re.IGNORECASE)
TEXT_COMPONENT_FIELDS = (
    "component_type", "billing_category", "formula", "unit", "season", "variant",
    "billing_class", "tier_unit",
)
ENGINE_NO_EVALUATOR = "no documented evaluator"


class ParseReviewError(ValueError):
    """A queue action is not allowed for the stored state."""


class ItemNotFound(ParseReviewError):
    """No such queue item."""


def _rows(cursor):
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, r)) for r in cursor.fetchall()]


def _versions_by_id(conn):
    rows = _rows(conn.execute(
        "SELECT id, schedule_code, version, status FROM tariff_versions"))
    return {r["id"]: r for r in rows}


def _where(version):
    return (f"schedule {version['schedule_code']} v{version['version']} "
            f"({version['status']})")


def _engine_cannot_evaluate(component):
    try:
        evaluate_component(component, {})
    except ValueError as error:
        return ENGINE_NO_EVALUATOR in str(error)
    return False


def _candidates(conn):
    """Yield (signal, kind, ref, schedule_code, field, stored_value, detail), by signal then id."""
    versions = _versions_by_id(conn)
    rules = _rows(conn.execute(
        "SELECT id, tariff_version_id, rule_type, rule_text FROM tariff_rules ORDER BY id"))
    components = _rows(conn.execute(
        "SELECT id, tariff_version_id, " + ", ".join(TEXT_COMPONENT_FIELDS)
        + ", rate, source_page, source_cell FROM tariff_components ORDER BY id"))

    for rule in rules:
        text = rule["rule_text"]
        match = MARKER.search(text or "")
        if match:
            version = versions.get(rule["tariff_version_id"], {})
            yield (VERIFY_MARKER, "rule", str(rule["id"]), version.get("schedule_code"),
                   "rule_text", text,
                   f"rule {rule['rule_type']!r} of {_where(version) if version else 'unknown version'} "
                   f"carries the marker {match.group(0)!r}")
    for c in components:
        version = versions.get(c["tariff_version_id"], {})
        for field in TEXT_COMPONENT_FIELDS:
            value = c[field]
            match = MARKER.search(value) if isinstance(value, str) else None
            if match:
                yield (VERIFY_MARKER, "component", str(c["id"]), version.get("schedule_code"),
                       field, value,
                       f"component {c['component_type']!r} field {field} carries the marker "
                       f"{match.group(0)!r}")
    for c in components:
        version = versions.get(c["tariff_version_id"], {})
        if c["formula"] is not None and _engine_cannot_evaluate(
                {"id": c["id"], "formula": c["formula"], "rate": c["rate"], "unit": c["unit"]}):
            yield (UNEVALUABLE_FORMULA, "component", str(c["id"]), version.get("schedule_code"),
                   "formula", c["formula"],
                   f"{c['component_type']!r} in {_where(version)} is a formula the billing "
                   "engine has no evaluator for")
    for c in components:
        version = versions.get(c["tariff_version_id"], {})
        if version.get("status") != "draft":
            continue
        page, cell = c["source_page"], c["source_cell"]
        page_ok = isinstance(page, int) and not isinstance(page, bool) and page >= 1
        cell_ok = isinstance(cell, str) and bool(cell.strip())
        if not (page_ok and cell_ok):
            yield (DRAFT_MISSING_CITATION, "component", str(c["id"]), version.get("schedule_code"),
                   "citation", f"page={page!r} cell={cell!r}",
                   f"draft component {c['component_type']!r} in {_where(version)} lacks "
                   + " and ".join(n for n, ok in (("a source page", page_ok), ("a source cell", cell_ok))
                                  if not ok))
    for c in components:
        version = versions.get(c["tariff_version_id"], {})
        if c["rate"] is None:
            continue
        try:
            zero = Decimal(str(c["rate"])) == 0
        except InvalidOperation:
            continue
        ctype = (c["component_type"] or "").lower()
        if zero and not any(token in ctype for token in NON_ORDINARY_TOKENS):
            yield (ZERO_RATE, "component", str(c["id"]), version.get("schedule_code"),
                   "rate", str(c["rate"]),
                   f"{c['component_type']!r} in {_where(version)} has a zero rate and is not a "
                   f"{'/'.join(NON_ORDINARY_TOKENS)} component")


def scan_for_review(conn):
    """Queue every signal hit not already queued. Re-running creates no duplicates."""
    created = {signal: 0 for signal in SIGNALS}
    for signal, kind, ref, code, field, value, detail in _candidates(conn):
        exists = conn.execute(
            "SELECT 1 FROM parse_review_items WHERE source_kind = ? AND source_ref = ? "
            "AND field = ? AND signal = ?", (kind, ref, field, signal)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO parse_review_items "
            "(source_kind, source_ref, schedule_code, field, stored_value, signal, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, ref, code, field, value, signal, detail))
        created[signal] += 1
    conn.commit()
    return {"created": created, "total_created": sum(created.values())}


def list_items(conn, status=None):
    sql = "SELECT * FROM parse_review_items"
    params = ()
    if status is not None:
        if status not in ("open", "resolved"):
            raise ParseReviewError("status must be 'open' or 'resolved'.")
        sql += " WHERE status = ?"
        params = (status,)
    return _rows(conn.execute(sql + " ORDER BY id", params))


def resolve_item(conn, item_id, reviewer, note):
    """Record that a human looked. Never edits the component or rule the item points at."""
    if not str(reviewer or "").strip():
        raise ParseReviewError("A reviewer is required.")
    if not str(note or "").strip():
        raise ParseReviewError("A resolution note saying what happens next is required.")
    row = conn.execute(
        "SELECT status FROM parse_review_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ItemNotFound(f"No parse-review item {item_id}.")
    if row[0] == "resolved":
        raise ParseReviewError(f"Item {item_id} is already resolved.")
    conn.execute(
        "UPDATE parse_review_items SET status = 'resolved', reviewer = ?, resolution_note = ?, "
        "resolved_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'open'",
        (str(reviewer).strip(), str(note).strip(), item_id))
    conn.commit()
    return _rows(conn.execute("SELECT * FROM parse_review_items WHERE id = ?", (item_id,)))[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scan stored data and queue parse-review items.")
    parser.add_argument("--scan", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        with db.connect(db.default_target(ROOT / "data" / "tariffs.db")) as conn:
            result = scan_for_review(conn)
            items = list_items(conn)
    except (ValueError, db.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    print("created this scan:", result["created"])
    for signal in SIGNALS:
        print(f"{signal}: {sum(1 for i in items if i['signal'] == signal)} queued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
