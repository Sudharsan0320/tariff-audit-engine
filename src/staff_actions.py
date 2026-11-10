"""Approve, dismiss, and correct a queue item.

A correction is stored only after the pipeline's own component verifier accepts it.
The queue row itself keeps the one field that failed. The rest of the proposal is
taken from the stored reading (the cache entry named by the item's schedule and
component index) or, for a component that was stored, from that component row.
If neither record carries the proposal, Correct is refused and nothing is invented.
"""

import json
from datetime import date
from pathlib import Path

try:
    from src import ai_extract, db
    from src.packet_pipeline import store_rates
    from src.staff_accounts import _now, ensure_tables
    from src.staff_queue import SCOPE_REFUSED, StaffQueueError, item_detail, utility_for, _index
except ImportError:
    import ai_extract
    import db
    from packet_pipeline import store_rates
    from staff_accounts import _now, ensure_tables
    from staff_queue import SCOPE_REFUSED, StaffQueueError, item_detail, utility_for, _index

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"

NOTE_REQUIRED = "A note is required to dismiss an item."
NOT_OPEN = "This item is no longer open."
NO_PROPOSAL = (
    "This item does not carry the refused component proposal, so it cannot be corrected."
)
SIGN_IN_REQUIRED = "Sign in is required."

# The one field a reviewer may change, by the signal that refused the proposal.
EDITABLE = {
    "rate_year_missing": "rate_year",
    "unit_not_in_vocabulary": "unit",
    "rate_not_on_page": "rate",
    "zero_rate_unexpected": "rate",
}

_books = {}


class StaffActionError(StaffQueueError):
    """The action was refused before a decision was recorded."""


def _raw_item(conn, item_id):
    cursor = conn.execute("SELECT * FROM parse_review_items WHERE id = ?", (item_id,))
    row = cursor.fetchone()
    if row is None:
        raise StaffActionError(f"No queue item {item_id}.")
    names = [column[0] for column in cursor.description]
    return dict(zip(names, row))


def _books_for(utility):
    if utility in _books:
        return _books[utility]
    found = []
    seen = set()
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
    if len(matches) != 1:
        return None
    return next(iter(matches.values()))


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
        row = conn.execute(
            "SELECT component_type, billing_category, rate, unit, variant, source_page, source_cell "
            "FROM tariff_components WHERE id = ?",
            (int(ref),),
        ).fetchone()
        if row is None or not row[0] or row[5] is None or not row[6]:
            return None
        proposal = {
            "component_type": row[0],
            "printed_label": row[1],
            "rate": row[2],
            "unit": row[3],
            "variant": row[4],
            "source_page": row[5],
            "source_quote": row[6],
        }
        if any(proposal.get(key) in (None, "") for key in ("rate", "unit")):
            return None
        return {"proposal": proposal, "book": None, "cache_key": None, "payload": {}, "field": EDITABLE[signal]}
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
    if not all(proposal.get(key) not in (None, "") for key in ("component_type", "rate", "unit", "source_quote", "source_page")):
        return None
    return {
        "proposal": proposal,
        "book": book,
        "cache_key": cache_key,
        "payload": payload,
        "field": EDITABLE[signal],
    }


def _utility(conn, item):
    scopes, schedules, components, _pages, rules, _rule_pages, names = _index(conn)
    return utility_for(item, scopes, schedules, components, rules, names)


def correctable(conn, item):
    return carried_proposal(conn, item) is not None


def _record(conn, item_id, account, action, outcome, note, payload):
    conn.execute(
        "INSERT INTO staff_decisions(item_id, account_id, action, outcome, note, corrected_payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, account["id"], action, outcome, note or None,
         None if payload is None else json.dumps(payload, default=str), _now().isoformat()),
    )


def _close(conn, item, account, status, note):
    conn.execute(
        "UPDATE parse_review_items SET status = ?, reviewer = ?, resolution_note = ?, resolved_at = ? WHERE id = ?",
        (status, account["username"], note or None, _now().isoformat(), item["id"]),
    )


def decisions_for(conn, account_scopes, item_id=None):
    allowed = set(account_scopes)
    index = _index(conn)
    rows = conn.execute(
        "SELECT d.id, d.item_id, a.username, a.role, d.action, d.outcome, d.note, "
        "d.corrected_payload, d.created_at, i.source_kind, i.source_ref, i.schedule_code, "
        "i.field, i.stored_value, i.signal, i.detail, i.status "
        "FROM staff_decisions d "
        "JOIN staff_accounts a ON a.id = d.account_id "
        "JOIN parse_review_items i ON i.id = d.item_id "
        "ORDER BY d.id"
    ).fetchall()
    names = ["id", "item_id", "username", "role", "action", "outcome", "note", "corrected_payload",
             "created_at", "source_kind", "source_ref", "schedule_code", "field", "stored_value",
             "signal", "detail", "status"]
    visible = []
    for row in rows:
        record = dict(zip(names, row))
        item = {key: record[key] for key in ("source_kind", "source_ref", "schedule_code", "detail")}
        utility = utility_for(item, *index[:3], index[4], index[6])
        # index layout: scopes, schedules, components, pages, rules, rule_pages, names
        scope = index[0].get(utility) if utility else None
        if scope not in allowed:
            continue
        if item_id is not None and record["item_id"] != item_id:
            continue
        visible.append(record)
    return visible


def _require_open(conn, account, item_id):
    if account is None:
        raise StaffActionError(SIGN_IN_REQUIRED)
    try:
        presented = item_detail(conn, item_id, account["scopes"])
    except StaffQueueError as error:
        raise StaffActionError(str(error)) from error
    item = _raw_item(conn, item_id)
    if item.get("status") != "open":
        raise StaffActionError(NOT_OPEN)
    return item, presented


def dismiss_item(conn, account, item_id, note):
    ensure_tables(conn)
    item, _presented = _require_open(conn, account, item_id)
    if not str(note or "").strip():
        raise StaffActionError(NOTE_REQUIRED)
    _close(conn, item, account, "dismissed", note.strip())
    _record(conn, item_id, account, "dismiss", "applied", note.strip(), None)
    conn.commit()
    return item_detail(conn, item_id, account["scopes"])


def approve_item(conn, account, item_id, note):
    ensure_tables(conn)
    item, _presented = _require_open(conn, account, item_id)
    text = str(note or "").strip() or None
    _close(conn, item, account, "approved", text)
    _record(conn, item_id, account, "approve", "applied", text, None)
    conn.commit()
    return item_detail(conn, item_id, account["scopes"])


def _apply_edit(proposal, field, value):
    edited = dict(proposal)
    text = str(value or "").strip()
    if field == "rate_year":
        if not text:
            raise StaffActionError("A rate year is required.")
        edited["rate_year"] = text
    elif field == "unit":
        if not text:
            raise StaffActionError("A unit is required.")
        edited["unit"] = text
    elif field == "rate":
        if text == "":
            raise StaffActionError("A rate is required.")
        edited["rate"] = text
    else:
        raise StaffActionError(NO_PROPOSAL)
    return edited


def _year_version(conn, utility, schedule, payload, label, pages):
    match = None
    for year in payload.get("rate_years") or []:
        if str(year.get("label") or "").strip() == label:
            match = year
            break
    if match is None:
        return None, {"signal": "rate_year_missing", "detail": f"rate year {label!r} is not in the stored reading"}
    parsed = ai_extract.iso_date(match.get("effective_from"))
    try:
        effective = date.fromisoformat(parsed) if parsed else None
    except ValueError:
        effective = None
    if effective is None or not any(ai_extract.printed_date_on_page(effective, text) for text in pages.values()):
        return None, {
            "signal": "rate_year_missing",
            "detail": f"rate year {label!r} has no printed effective date on the cited pages",
        }
    found = conn.execute(
        "SELECT id FROM tariff_versions WHERE utility = ? AND schedule_code = ? AND effective_from = ? "
        "ORDER BY id LIMIT 1",
        (utility, schedule, effective.isoformat()),
    ).fetchone()
    if found is None:
        return None, {
            "signal": "rate_year_missing",
            "detail": f"no stored version of {schedule} starts on {effective.isoformat()}",
        }
    return found[0], None


def correct_item(conn, account, item_id, value):
    ensure_tables(conn)
    item, _presented = _require_open(conn, account, item_id)
    carried = carried_proposal(conn, item)
    if carried is None:
        raise StaffActionError(NO_PROPOSAL)
    edited = _apply_edit(carried["proposal"], carried["field"], value)
    book = carried["book"]
    if book is None:
        book = _book_for(_utility(conn, item))
    page_number = edited.get("source_page")
    if book is None or carried["cache_key"] is None:
        pages = {}
        if book is not None and page_number:
            pages = _pages(book, f"x|{page_number}-{page_number}", page_number)
    else:
        pages = _pages(book, carried["cache_key"], page_number)
    vocab = ai_extract.vocabulary(conn)
    row, failure = ai_extract._verify_component(edited, pages, vocab, {"component_type": set(), "rule_type": set(), "unit": set()})
    if failure is None and carried["field"] == "rate_year":
        version_id, failure = _year_version(
            conn, _utility(conn, item), item.get("schedule_code"), carried["payload"], edited["rate_year"], pages)
    elif failure is None:
        version_id, failure = _version_for_component(conn, item, carried, pages)
    if failure:
        _record(conn, item_id, account, "correct", "rejected_by_gate", failure.get("detail"), edited)
        conn.commit()
        return {"item": item_detail(conn, item_id, account["scopes"]), "signal": failure.get("signal"), "detail": failure.get("detail")}
    stored = store_rates(conn, version_id, [row])
    note = f"stored {stored} component on version {version_id}"
    _close(conn, item, account, "corrected", note)
    _record(conn, item_id, account, "correct", "applied", note, edited)
    conn.commit()
    return {"item": item_detail(conn, item_id, account["scopes"]), "signal": None, "stored": stored, "version_id": version_id}


def _version_for_component(conn, item, carried, pages):
    ref = str(item.get("source_ref") or "")
    if item.get("source_kind") == "component" and ref.isdigit():
        found = conn.execute(
            "SELECT tariff_version_id FROM tariff_components WHERE id = ?", (int(ref),)).fetchone()
        if found:
            return found[0], None

    payload = carried["payload"]
    utility = _utility(conn, item)
    schedule = item.get("schedule_code")
    if len(payload.get("rate_years") or []) >= 2:
        label = str(carried["proposal"].get("rate_year") or "").strip()
        if not label:
            return None, {
                "signal": "rate_year_missing",
                "detail": "the corrected component has no rate year to identify its version",
            }
        return _year_version(conn, utility, schedule, payload, label, pages)

    parsed = ai_extract.iso_date(payload.get("effective_date"))
    try:
        effective = date.fromisoformat(parsed) if parsed else None
    except ValueError:
        effective = None
    if effective is None or not any(
        ai_extract.printed_date_on_page(effective, text) for text in pages.values()
    ):
        return None, {
            "signal": "rate_year_missing",
            "detail": "the reading's effective date is not verified on the cited pages",
        }
    found = conn.execute(
        "SELECT id FROM tariff_versions WHERE utility = ? AND schedule_code = ? AND effective_from = ? "
        "ORDER BY id LIMIT 1",
        (utility, schedule, effective.isoformat()),
    ).fetchone()
    if found is None:
        return None, {
            "signal": "no_verified_rates",
            "detail": "no stored version matches the verified reading's effective date",
        }
    return found[0], None
