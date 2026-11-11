"""Operator actions over the parse-review queue: list, correct, dismiss. Single person, account-free.

Local CLI only: no server, no port, no login, no passwords. Replaces the staff console's correction capability
(console removed 2026-11-11). The actor is always the fixed label `operator`.

Correct: the corrected candidate re-enters through the SAME verifier gate the pipeline uses
(`ai_extract._verify_component`: the quote on the cited page and, for a component, the rate printed on that
page), then the version check (rate year / effective date printed on the cited pages). A correction that
fails the gate raises GateRefused with the verifier's reason; nothing is stored and nothing is logged. There is
no force option. The queue row keeps the one field that failed; the rest of the proposal comes from the stored
reading (the cache entry named by the item's schedule and component index) or, for a stored component, that
component row. If neither carries the proposal, Correct is refused and nothing is invented.

Dismiss: a note is required; the item is closed as dismissed.

Every applied action appends one row to `operator_changes` (item, action, before/after payload, note, actor,
timestamp). The table is append-only (triggers refuse UPDATE and DELETE). There is no revert command: the
log keeps before-values, so a manual revert stays possible.

    python -m src.operator_actions list
    python -m src.operator_actions correct ITEM_ID VALUE
    python -m src.operator_actions dismiss ITEM_ID "note"
"""

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from src import ai_extract, db, review_status
    from src.monitoring import reason_for, utility_for, utility_index
    from src.packet_pipeline import store_rates
except ImportError:
    import ai_extract
    import db
    import review_status
    from monitoring import reason_for, utility_for, utility_index
    from packet_pipeline import store_rates

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
ACTOR = "operator"
APPEND_ONLY = "operator_changes is append-only"

NOTE_REQUIRED = "A note is required to dismiss an item."
NOT_OPEN = "This item is no longer open."
NO_PROPOSAL = "This item does not carry the refused component proposal, so it cannot be corrected."

# The one field the operator may change, by the signal that refused the proposal.
EDITABLE = {
    "rate_year_missing": "rate_year",
    "unit_not_in_vocabulary": "unit",
    "rate_not_on_page": "rate",
    "zero_rate_unexpected": "rate",
}
# Fields a correction may change on a component that is already stored, and their columns.
MODIFIABLE = {"rate": "rate", "unit": "unit"}

_books = {}


class OperatorActionError(ValueError):
    """The action was refused before anything was written."""


class GateRefused(OperatorActionError):
    """The verifier gate refused the corrected candidate. Nothing was stored or logged."""

    def __init__(self, signal, detail):
        super().__init__(f"{signal}: {detail}")
        self.signal, self.detail = signal, detail


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operator_changes (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('correct', 'dismiss')),
            before_payload TEXT,
            after_payload TEXT,
            note TEXT,
            actor TEXT NOT NULL DEFAULT 'operator' CHECK (actor = 'operator'),
            created_at TEXT NOT NULL
        )
    """)
    if isinstance(conn, sqlite3.Connection):
        for event in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS operator_changes_no_{event.lower()} BEFORE {event} "
                         f"ON operator_changes BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY}'); END")
    else:
        conn.execute("CREATE OR REPLACE FUNCTION operator_changes_append_only() RETURNS trigger "
                     f"LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '{APPEND_ONLY}'; END $$")
        conn.execute("CREATE OR REPLACE TRIGGER operator_changes_no_change BEFORE UPDATE OR DELETE "
                     "ON operator_changes FOR EACH ROW EXECUTE FUNCTION operator_changes_append_only()")
    conn.commit()


def _log(conn, item_id, action, before, after, note):
    conn.execute(
        "INSERT INTO operator_changes(item_id, action, before_payload, after_payload, note, actor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, action, None if before is None else json.dumps(before, default=str),
         None if after is None else json.dumps(after, default=str), note, ACTOR, _now()))


def _rows(cursor):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _snapshot(conn, table, row_id):
    found = _rows(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)))
    return found[0] if found else None


def _item(conn, item_id):
    item = _snapshot(conn, "parse_review_items", item_id)
    if item is None:
        raise OperatorActionError(f"No queue item {item_id}.")
    return item


def _open_item(conn, item_id):
    item = _item(conn, item_id)
    if item.get("status") != "open":
        raise OperatorActionError(NOT_OPEN)
    return item


def _utility(conn, item):
    return utility_for(item, utility_index(conn))


def _close(conn, item, status, note):
    conn.execute("UPDATE parse_review_items SET status = ?, reviewer = ?, resolution_note = ?, resolved_at = ? "
                 "WHERE id = ?", (status, ACTOR, note or None, _now(), item["id"]))


# ---- list ----

def open_items(conn):
    index = utility_index(conn)
    out = []
    for item in _rows(conn.execute("SELECT * FROM parse_review_items WHERE status = 'open' ORDER BY id")):
        out.append({"id": item["id"], "utility": utility_for(item, index), "schedule_code": item["schedule_code"],
                    "field": item["field"], "stored_value": item["stored_value"], "signal": item["signal"],
                    "reason": reason_for(item), "correctable": item.get("signal") in EDITABLE})
    return out


# ---- dismiss ----

def dismiss_item(conn, item_id, note):
    text = str(note or "").strip()
    if not text:
        raise OperatorActionError(NOTE_REQUIRED)
    item = _open_item(conn, item_id)
    try:
        _close(conn, item, "dismissed", text)
        _log(conn, item_id, "dismiss", {"item_status": "open"}, {"item_status": "dismissed"}, text)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _item(conn, item_id)


# ---- correct ----

def _books_for(utility):
    if utility in _books:
        return _books[utility]
    found, seen = [], set()
    if RAW_DIR.exists():
        try:
            from src import document_intake
        except ImportError:
            import document_intake
        for path in sorted(RAW_DIR.glob("*.pdf")):
            detection = document_intake.identify_document(path)
            if detection.get("utility") == utility and detection.get("doc_kind") == document_intake.TARIFF:
                digest = ai_extract.content_hash(path)
                if digest not in seen:
                    found.append(path)
                    seen.add(digest)
    _books[utility] = found
    return found


def _book_for(utility):
    books = _books_for(utility)
    return books[0] if len(books) == 1 else None


def _payload_for(utility, schedule, index, item):
    matches = {}
    field = EDITABLE.get(item.get("signal"))
    expected = str(item.get("stored_value") or "").strip()
    for book in _books_for(utility):
        digest = ai_extract.content_hash(book)
        cache = ai_extract._load_cache(ai_extract._cache_path(ai_extract.CACHE_DIR, digest))
        for key, entry in cache.get("sections", {}).items():
            if key.split("|", 1)[0] != schedule:
                continue
            try:
                payload = ai_extract.parse_model_json(entry.get("raw") or "")
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            components = payload.get("components") or []
            if not (0 <= index < len(components) and isinstance(components[index], dict)):
                continue
            proposal = dict(components[index])
            actual = proposal.get(field)
            if item.get("signal") == "rate_year_missing":
                if actual not in (None, ""):
                    continue
            elif str(actual if actual is not None else "").strip() != expected:
                continue
            matches[(digest, key)] = (book, key, payload, proposal)
    return next(iter(matches.values())) if len(matches) == 1 else None


def _pages(book, cache_key, page_number):
    start, end = (int(part) for part in cache_key.split("|", 1)[1].split("-"))
    wanted = set(range(start, end + 1))
    if page_number:
        wanted.add(int(page_number))
    pages = {}
    import pdfplumber
    with pdfplumber.open(book) as pdf:
        for number in sorted(wanted):
            if 1 <= number <= len(pdf.pages):
                pages[number] = pdf.pages[number - 1].extract_text() or ""
    return pages


def carried_proposal(conn, item):
    """The refused component, if a stored record still has its fields. Else None."""
    signal = item.get("signal")
    if signal not in EDITABLE:
        return None
    ref = str(item.get("source_ref") or "")
    if item.get("source_kind") == "component" and ref.isdigit():
        row = conn.execute("SELECT component_type, billing_category, rate, unit, variant, source_page, source_cell "
                           "FROM tariff_components WHERE id = ?", (int(ref),)).fetchone()
        if row is None or not row[0] or row[5] is None or not row[6]:
            return None
        proposal = {"component_type": row[0], "printed_label": row[1], "rate": row[2], "unit": row[3],
                    "variant": row[4], "source_page": row[5], "source_quote": row[6]}
        if any(proposal.get(key) in (None, "") for key in ("rate", "unit")):
            return None
        return {"proposal": proposal, "book": None, "cache_key": None, "payload": {},
                "field": EDITABLE[signal], "component_id": int(ref)}
    parts = ref.split(":")
    if len(parts) < 2 or parts[1] != "component" or not parts[-1].isdigit():
        return None
    utility = _utility(conn, item)
    if not utility:
        return None
    found = _payload_for(utility, item.get("schedule_code") or parts[0], int(parts[-1]), item)
    if found is None:
        return None
    book, cache_key, payload, proposal = found
    if not all(proposal.get(k) not in (None, "") for k in ("component_type", "rate", "unit", "source_quote", "source_page")):
        return None
    return {"proposal": proposal, "book": book, "cache_key": cache_key, "payload": payload, "field": EDITABLE[signal]}


def _apply_edit(proposal, field, value):
    text = str(value or "").strip()
    if field not in ("rate_year", "unit", "rate"):
        raise OperatorActionError(NO_PROPOSAL)
    if not text:
        raise OperatorActionError(f"A {field.replace('_', ' ')} is required.")
    edited = dict(proposal)
    edited[field] = text
    return edited


def _dated_version(conn, utility, schedule, effective_text, pages, missing_detail, absent_signal, absent_detail):
    parsed = ai_extract.iso_date(effective_text)
    try:
        effective = date.fromisoformat(parsed) if parsed else None
    except ValueError:
        effective = None
    if effective is None or not any(ai_extract.printed_date_on_page(effective, text) for text in pages.values()):
        return None, {"signal": "rate_year_missing", "detail": missing_detail}
    found = conn.execute("SELECT id FROM tariff_versions WHERE utility = ? AND schedule_code = ? AND effective_from = ? "
                         "ORDER BY id LIMIT 1", (utility, schedule, effective.isoformat())).fetchone()
    if found is None:
        return None, {"signal": absent_signal, "detail": absent_detail.format(schedule=schedule, date=effective.isoformat())}
    return found[0], None


def _year_version(conn, utility, schedule, payload, label, pages):
    match = next((y for y in payload.get("rate_years") or [] if str(y.get("label") or "").strip() == label), None)
    if match is None:
        return None, {"signal": "rate_year_missing", "detail": f"rate year {label!r} is not in the stored reading"}
    return _dated_version(conn, utility, schedule, match.get("effective_from"), pages,
                          f"rate year {label!r} has no printed effective date on the cited pages",
                          "rate_year_missing", "no stored version of {schedule} starts on {date}")


def _version_for_component(conn, item, carried, pages):
    ref = str(item.get("source_ref") or "")
    if item.get("source_kind") == "component" and ref.isdigit():
        found = conn.execute("SELECT tariff_version_id FROM tariff_components WHERE id = ?", (int(ref),)).fetchone()
        if found:
            return found[0], None
    payload = carried["payload"]
    utility, schedule = _utility(conn, item), item.get("schedule_code")
    if len(payload.get("rate_years") or []) >= 2:
        label = str(carried["proposal"].get("rate_year") or "").strip()
        if not label:
            return None, {"signal": "rate_year_missing",
                          "detail": "the corrected component has no rate year to identify its version"}
        return _year_version(conn, utility, schedule, payload, label, pages)
    return _dated_version(conn, utility, schedule, payload.get("effective_date"), pages,
                          "the reading's effective date is not verified on the cited pages",
                          "no_verified_rates", "no stored version matches the verified reading's effective date")


def correct_item(conn, item_id, value):
    """Store (or modify) the corrected component only if the verifier gate accepts it. Raises GateRefused."""
    item = _open_item(conn, item_id)
    carried = carried_proposal(conn, item)
    if carried is None:
        raise OperatorActionError(NO_PROPOSAL)
    edited = _apply_edit(carried["proposal"], carried["field"], value)
    book = carried["book"] or _book_for(_utility(conn, item))
    page_number = edited.get("source_page")
    if carried["cache_key"] is None:
        pages = _pages(book, f"x|{page_number}-{page_number}", page_number) if book is not None and page_number else {}
    else:
        pages = _pages(book, carried["cache_key"], page_number)
    vocab = ai_extract.vocabulary(conn)
    row, failure = ai_extract._verify_component(edited, pages, vocab, {"component_type": set(), "rule_type": set(), "unit": set()})
    version_id = None
    if failure is None and carried["field"] == "rate_year":
        version_id, failure = _year_version(conn, _utility(conn, item), item.get("schedule_code"), carried["payload"],
                                            edited["rate_year"], pages)
    elif failure is None:
        version_id, failure = _version_for_component(conn, item, carried, pages)
    if failure:
        raise GateRefused(failure.get("signal"), failure.get("detail"))
    try:
        if carried.get("component_id") is not None:
            result = _modify_component(conn, item, carried, row)
        else:
            result = _store_component(conn, item, version_id, row)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result["item"] = _item(conn, item_id)
    return result


def _store_component(conn, item, version_id, row):
    ids = lambda: {r[0] for r in conn.execute("SELECT id FROM tariff_components WHERE tariff_version_id = ?", (version_id,))}
    before = ids()
    stored = store_rates(conn, version_id, [row])
    new_ids = sorted(ids() - before)
    if len(new_ids) != stored:
        raise OperatorActionError("The stored rows could not be identified, so nothing was saved.")
    note = f"stored {stored} component on version {version_id}"
    _close(conn, item, "corrected", note)
    _log(conn, item["id"], "correct", {"item_status": "open", "table": "tariff_components", "rows": []},
         {"item_status": "corrected", "table": "tariff_components",
          "rows": [_snapshot(conn, "tariff_components", i) for i in new_ids]}, note)
    return {"stored": stored, "modified": 0, "version_id": version_id, "component_ids": new_ids}


def _modify_component(conn, item, carried, row):
    field = carried["field"]
    if field not in MODIFIABLE:
        raise OperatorActionError(NO_PROPOSAL)
    component_id = carried["component_id"]
    old = _snapshot(conn, "tariff_components", component_id)
    if old is None:
        raise OperatorActionError(NO_PROPOSAL)
    value = float(row["rate"]) if field == "rate" else row["unit"]
    conn.execute(f"UPDATE tariff_components SET {MODIFIABLE[field]} = ? WHERE id = ?", (value, component_id))
    new = _snapshot(conn, "tariff_components", component_id)
    note = f"changed the {field} of component {component_id} from {old[MODIFIABLE[field]]} to {new[MODIFIABLE[field]]}"
    _close(conn, item, "corrected", note)
    _log(conn, item["id"], "correct", {"item_status": "open", "table": "tariff_components", "rows": [old]},
         {"item_status": "corrected", "table": "tariff_components", "rows": [new]}, note)
    return {"stored": 0, "modified": 1, "version_id": old["tariff_version_id"], "component_ids": [component_id]}


# ---- CLI ----

def main(argv=None):
    parser = argparse.ArgumentParser(prog="operator_actions", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    c = sub.add_parser("correct")
    c.add_argument("item_id", type=int)
    c.add_argument("value")
    d = sub.add_parser("dismiss")
    d.add_argument("item_id", type=int)
    d.add_argument("note")
    args = parser.parse_args(argv)
    conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"))
    try:
        if args.action == "list":
            for item in open_items(conn):
                print(json.dumps(item, default=str))
            return 0
        review_status.verify(conn)
        ensure_table(conn)
        if args.action == "dismiss":
            result = dismiss_item(conn, args.item_id, args.note)
        else:
            result = correct_item(conn, args.item_id, args.value)
        print(json.dumps(result, default=str))
        return 0
    except GateRefused as refused:
        print(f"Refused by the verifier gate ({refused.signal}): {refused.detail}. Nothing was stored.", file=sys.stderr)
        return 2
    except OperatorActionError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
