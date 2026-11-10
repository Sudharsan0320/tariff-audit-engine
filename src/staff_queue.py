"""Plain-language queue for the staff console.

An item's scope is the scope of its utility. The utility is read from the
item's stored version (a component or rule id) or, when the item names no
version, from the utility named in its detail or from a schedule that belongs
to exactly one utility. Nothing here writes tariff data.
"""

import re

SCOPE_REFUSED = "This item is outside your scope."
SCOPE_UNKNOWN = "This item's utility has no scope on record."

KIND_WORDS = {
    "component": "Component",
    "rule": "Rule",
    "tariff_book_section": "Tariff section",
}

SIGNAL_WORDS = {
    "rate_year_missing": "The extracted rate has no rate year, so it was not stored.",
    "rate_not_on_page": "A rate in the reading is not printed on the cited page.",
    "quote_not_found": "The quoted text was not found on the cited page.",
    "no_verified_rates": "No rate survived verification, so nothing was stored.",
    "unit_not_in_vocabulary": "The unit is not one this engine knows.",
    "model_output_incomplete": "The reading stopped before it finished.",
    "missing_rule": "A printed rule was not stored.",
    "unreadable_pages": "No readable page was found for this section.",
    "validation_failed": "The stored draft did not pass validation.",
    "zero_rate_unexpected": "A stored rate is zero where a zero rate was not expected.",
    "verify_marker": "The stored text is marked for a person to check.",
    "unevaluable_formula": "The formula cannot be evaluated.",
    "draft_missing_citation": "A draft row has no citation.",
}

_PAGE_IN_REF = re.compile(r":p(\d+)\b", re.I)
_PAGE_IN_DETAIL = re.compile(r"\b(?:p\.?\s*|page\s+)(\d+)\b", re.I)


class StaffQueueError(ValueError):
    """A staff queue read is not allowed."""


def reason_for(item):
    """Plain sentence for a signal. An unmapped signal keeps its raw name."""
    signal = item.get("signal") or ""
    if signal == "missing_rule" and _mentions_eligibility(item):
        return "The printed eligibility text was not stored."
    sentence = SIGNAL_WORDS.get(signal)
    if sentence:
        return sentence
    return f"{signal} — no plain-language wording for this signal yet"


def kind_words(source_kind):
    return KIND_WORDS.get(source_kind, source_kind or "Item")


def cited_page(item, joined_page=None):
    if joined_page not in (None, ""):
        return str(joined_page)
    for pattern, text in ((_PAGE_IN_REF, item.get("source_ref")), (_PAGE_IN_DETAIL, item.get("detail"))):
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return None


def _mentions_eligibility(item):
    blob = f"{item.get('source_ref') or ''} {item.get('field') or ''}".casefold()
    return "eligibility" in blob


def _rows(cursor):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _index(conn):
    scopes = dict(conn.execute("SELECT utility, scope FROM utility_scopes").fetchall())
    schedules = {}
    for code, utility in conn.execute("SELECT schedule_code, utility FROM tariff_versions"):
        schedules.setdefault(code, set()).add(utility)
    components = dict(conn.execute(
        "SELECT c.id, v.utility FROM tariff_components c "
        "JOIN tariff_versions v ON v.id = c.tariff_version_id"
    ).fetchall())
    component_pages = dict(conn.execute(
        "SELECT id, source_page FROM tariff_components"
    ).fetchall())
    rules = dict(conn.execute(
        "SELECT r.id, v.utility FROM tariff_rules r "
        "JOIN tariff_versions v ON v.id = r.tariff_version_id"
    ).fetchall())
    rule_pages = dict(conn.execute("SELECT id, source_page FROM tariff_rules").fetchall())
    names = sorted(scopes, key=len, reverse=True)
    return scopes, schedules, components, component_pages, rules, rule_pages, names


def utility_for(item, scopes, schedules, components, rules, utility_names):
    """The utility this item belongs to, or None when the stored data does not say."""
    ref = str(item.get("source_ref") or "")
    if item.get("source_kind") == "component" and ref.isdigit() and int(ref) in components:
        return components[int(ref)]
    if item.get("source_kind") == "rule" and ref.isdigit() and int(ref) in rules:
        return rules[int(ref)]
    detail = item.get("detail") or ""
    for name in utility_names:
        if detail.startswith(name + " ") or detail.startswith(name + ":"):
            return name
    owners = schedules.get(item.get("schedule_code") or "", set())
    if len(owners) == 1:
        return next(iter(owners))
    return None


def _present(item, index):
    scopes, schedules, components, component_pages, rules, rule_pages, names = index
    utility = utility_for(item, scopes, schedules, components, rules, names)
    scope = scopes.get(utility) if utility else None
    ref = str(item.get("source_ref") or "")
    joined_page = None
    if item.get("source_kind") == "component" and ref.isdigit():
        joined_page = component_pages.get(int(ref))
    elif item.get("source_kind") == "rule" and ref.isdigit():
        joined_page = rule_pages.get(int(ref))
    page = cited_page(item, joined_page)
    value = item.get("stored_value")
    return {
        "id": item["id"],
        "company": utility,
        "scope": scope,
        "schedule": item.get("schedule_code"),
        "kind": kind_words(item.get("source_kind")),
        "source_kind": item.get("source_kind"),
        "signal": item.get("signal"),
        "reason": reason_for(item),
        "page": page,
        "value": None if value in (None, "") else str(value),
        "field": item.get("field"),
        "source_ref": item.get("source_ref"),
        "detail": item.get("detail"),
        "status": item.get("status"),
    }


def open_queue(conn, account_scopes):
    """Open items whose derived scope is one of account_scopes."""
    allowed = set(account_scopes)
    index = _index(conn)
    visible = []
    for item in _rows(conn.execute(
        "SELECT * FROM parse_review_items WHERE status = 'open' ORDER BY id"
    )):
        presented = _present(item, index)
        if presented["scope"] in allowed:
            visible.append(presented)
    return visible


def item_detail(conn, item_id, account_scopes):
    """One open or closed item, refused when its scope is not among account_scopes."""
    found = conn.execute("SELECT * FROM parse_review_items WHERE id = ?", (item_id,)).fetchone()
    if found is None:
        raise StaffQueueError(f"No queue item {item_id}.")
    names = [column[0] for column in conn.execute(
        "SELECT * FROM parse_review_items WHERE id = ?", (item_id,)).description]
    item = dict(zip(names, found))
    presented = _present(item, _index(conn))
    if presented["scope"] is None or presented["scope"] not in set(account_scopes):
        raise StaffQueueError(SCOPE_REFUSED if presented["scope"] else SCOPE_UNKNOWN)
    return presented
