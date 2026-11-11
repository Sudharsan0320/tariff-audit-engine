"""Read-only catalog of stored tariff versions: company -> book -> grouped schedules -> versions.

Every grouping and title comes from stored columns: `utility` and `state`, the printed `book_label`
(with its `book_evidence`), `schedule_kind`, `schedule_code`, `status` and the effective dates.
There are no company-name conditionals and no hardcoded titles here, so a new company or a new
book appears on its own. Nothing in this module writes.

A schedule entry is one schedule code inside one book; its versions are that code's rows in that
book (Pepco's two rate years are two versions of one entry). An entry's current status is the
status of its latest version (latest effective date, then highest id). The status filter keeps an
entry when at least one of its versions has the chosen status.
"""

KINDS = ("schedule", "rider", "informational")
GROUP_TITLES = {"schedule": "Schedules", "rider": "Riders", "informational": "Informational"}
STATUSES = ("active", "draft", "superseded")


class CatalogNotFound(LookupError):
    """No stored version matches the requested company, book or schedule."""


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _versions(conn, where="", params=()):
    return _rows(
        conn,
        "SELECT v.id, v.state, v.utility, v.book_label, v.book_evidence, v.schedule_code, v.version, "
        "v.effective_from, v.effective_to, v.status, v.schedule_kind, "
        "(SELECT COUNT(*) FROM tariff_components c WHERE c.tariff_version_id = v.id) AS components, "
        "(SELECT COUNT(*) FROM tariff_rules r WHERE r.tariff_version_id = v.id) AS rules "
        f"FROM tariff_versions v {where} ORDER BY v.utility, v.book_label, v.schedule_code, v.effective_from, v.id",
        params,
    )


def _kind_counts(versions):
    counts = {kind: 0 for kind in KINDS}
    for version in versions:
        if version["schedule_kind"] in counts:
            counts[version["schedule_kind"]] += 1
    return counts


def _status_counts(versions):
    counts = {status: 0 for status in STATUSES}
    for version in versions:
        counts[version["status"]] = counts.get(version["status"], 0) + 1
    return counts


def _span(versions):
    starts = sorted(v["effective_from"] for v in versions if v["effective_from"])
    return {"from": starts[0] if starts else None, "to": starts[-1] if starts else None}


def _book_summary(utility, label, versions):
    evidence = sorted({v["book_evidence"] for v in versions if v["book_evidence"]})
    return {
        "utility": utility,
        "book_label": label,
        "book_evidence": evidence[0] if len(evidence) == 1 else evidence,
        "versions": len(versions),
        "kinds": _kind_counts(versions),
        "statuses": _status_counts(versions),
        "effective_span": _span(versions),
        "schedules": len({v["schedule_code"] for v in versions}),
    }


def companies(conn):
    """One card per company, built from the stored rows."""
    by_company = {}
    for version in _versions(conn):
        by_company.setdefault(version["utility"], []).append(version)
    cards = []
    for utility in sorted(by_company):
        versions = by_company[utility]
        books = {}
        for version in versions:
            books.setdefault(version["book_label"], []).append(version)
        cards.append({
            "utility": utility,
            "states": sorted({v["state"] for v in versions}),
            "versions": len(versions),
            "kinds": _kind_counts(versions),
            "statuses": _status_counts(versions),
            "effective_span": _span(versions),
            "books": [{"book_label": label, "versions": len(rows)} for label, rows in sorted(books.items(),
                      key=lambda item: (item[0] is None, item[0] or ""))],
        })
    return cards


def books(conn, utility):
    """The company's book or books."""
    versions = _versions(conn, "WHERE v.utility = ?", (utility,))
    if not versions:
        raise CatalogNotFound(f"No stored versions for {utility}.")
    grouped = {}
    for version in versions:
        grouped.setdefault(version["book_label"], []).append(version)
    return {
        "utility": utility,
        "states": sorted({v["state"] for v in versions}),
        "books": [_book_summary(utility, label, rows) for label, rows in sorted(
            grouped.items(), key=lambda item: (item[0] is None, item[0] or ""))],
    }


def _entry(code, versions):
    ordered = sorted(versions, key=lambda v: (v["effective_from"] or "", v["id"]))
    latest = ordered[-1]
    return {
        "schedule_code": code,
        "schedule_kind": latest["schedule_kind"],
        "versions": len(ordered),
        "latest_effective_from": latest["effective_from"],
        "current_status": latest["status"],
        "statuses": _status_counts(ordered),
    }


def book(conn, utility, book_label, status=None):
    """One book's schedule entries grouped by stored kind, optionally filtered by version status."""
    if status not in (None, "", "all") and status not in STATUSES:
        raise ValueError(f"status must be one of all, {', '.join(STATUSES)}")
    versions = _versions(conn, "WHERE v.utility = ? AND v.book_label = ?", (utility, book_label))
    if not versions:
        raise CatalogNotFound(f"No stored versions for {utility}, {book_label}.")
    by_code = {}
    for version in versions:
        by_code.setdefault(version["schedule_code"], []).append(version)
    entries = [_entry(code, rows) for code, rows in sorted(by_code.items())]
    shown = [e for e in entries if status in (None, "", "all") or e["statuses"].get(status, 0) > 0]
    groups = []
    for kind in KINDS:
        members = [e for e in shown if e["schedule_kind"] == kind]
        groups.append({"kind": kind, "title": GROUP_TITLES[kind], "entries": members,
                       "versions": sum(e["versions"] for e in members)})
    summary = _book_summary(utility, book_label, versions)
    summary.update({"status_filter": status or "all", "groups": groups,
                    "entries_total": len(entries), "entries_shown": len(shown)})
    return summary


def schedule(conn, utility, book_label, schedule_code):
    """One schedule's versions in one book, with component and rule counts."""
    versions = _versions(conn, "WHERE v.utility = ? AND v.book_label = ? AND v.schedule_code = ?",
                         (utility, book_label, schedule_code))
    if not versions:
        raise CatalogNotFound(f"No stored versions of {schedule_code} in {utility}, {book_label}.")
    entry = _entry(schedule_code, versions)
    entry.update({
        "utility": utility,
        "book_label": book_label,
        "rows": [{key: v[key] for key in ("id", "version", "effective_from", "effective_to", "status",
                                          "components", "rules")}
                 for v in sorted(versions, key=lambda v: (v["effective_from"] or "", v["id"]))],
    })
    return entry


def _squash(text):
    return "".join(ch for ch in str(text).casefold() if ch.isalnum())


def search(conn, q):
    """Schedule entries whose code contains the query (case, spaces and punctuation ignored)."""
    needle = _squash(q)
    if not needle:
        return []
    found = {}
    for version in _versions(conn):
        if needle in _squash(version["schedule_code"]):
            key = (version["utility"], version["book_label"], version["schedule_code"])
            found.setdefault(key, []).append(version)
    results = []
    for (utility, label, code), rows in sorted(found.items(), key=lambda item: (item[0][2], item[0][0], item[0][1] or "")):
        entry = _entry(code, rows)
        entry.update({"utility": utility, "book_label": label})
        results.append(entry)
    return results
