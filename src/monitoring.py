"""B24 monitoring: the system's own health and feedback signals, written to schema `monitor`.

Checks (thresholds are the named constants below): data staleness (newest bill period vs today), mart and
feature currency (their builds cover the newest bill), parse-review queue depth by status and by reason,
audit coverage, repeat extraction failures (a schedule with REPEAT_MIN or more open queue items sharing one
reason), and open-queue growth since the previous monitor run.

One run writes its findings in one transaction, to the `monitor` tables only. Nothing is delivered anywhere:
findings are read from `monitor.monitor_findings`. (Outbox delivery went with the staff console, removed
2026-11-11; `monitor_runs` keeps its outbox_rows / outbox_note columns for history, and new runs write 0 and
DELIVERY_NOTE.)
"""

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from src import db, store_util as su
except ImportError:
    import db
    import store_util as su

ROOT = Path(__file__).resolve().parents[1]
STALE_WARN_DAYS = 35
STALE_ALERT_DAYS = 70
REPEAT_MIN = 3
GROWTH_WARN = 10
OK, WARN, ALERT = "ok", "warn", "alert"
DELIVERY_NOTE = "no delivery: findings are recorded in the monitor tables only"

# Plain sentence per queue signal (moved here from the removed console's queue view).
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


def reason_for(item):
    """Plain sentence for a signal. An unmapped signal keeps its raw name."""
    signal = item.get("signal") or ""
    blob = f"{item.get('source_ref') or ''} {item.get('field') or ''}".casefold()
    if signal == "missing_rule" and "eligibility" in blob:
        return "The printed eligibility text was not stored."
    return SIGNAL_WORDS.get(signal) or f"{signal} — no plain-language wording for this signal yet"


def utility_index(conn):
    """(schedules, components, rules, utility names) from the stored tariffs: no scope table needed."""
    schedules = {}
    for code, utility in conn.execute("SELECT schedule_code, utility FROM tariff_versions"):
        schedules.setdefault(code, set()).add(utility)
    components = dict(conn.execute("SELECT c.id, v.utility FROM tariff_components c "
                                   "JOIN tariff_versions v ON v.id = c.tariff_version_id").fetchall())
    rules = dict(conn.execute("SELECT r.id, v.utility FROM tariff_rules r "
                              "JOIN tariff_versions v ON v.id = r.tariff_version_id").fetchall())
    names = sorted({u for owners in schedules.values() for u in owners}, key=len, reverse=True)
    return schedules, components, rules, names


def utility_for(item, index):
    """The utility a queue item belongs to, or None when the stored data does not say."""
    schedules, components, rules, names = index
    ref = str(item.get("source_ref") or "")
    if item.get("source_kind") == "component" and ref.isdigit() and int(ref) in components:
        return components[int(ref)]
    if item.get("source_kind") == "rule" and ref.isdigit() and int(ref) in rules:
        return rules[int(ref)]
    detail = item.get("detail") or ""
    for name in names:
        if detail.startswith(name + " ") or detail.startswith(name + ":"):
            return name
    owners = schedules.get(item.get("schedule_code") or "", set())
    return next(iter(owners)) if len(owners) == 1 else None


def _table_exists(conn, schema, table):
    if su.is_sqlite(conn):
        try:
            conn.execute(f"SELECT 1 FROM {schema}.{table} LIMIT 0")
            return True
        except Exception:
            return False
    return conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
                        (schema, table)).fetchone() is not None


def staleness_status(days):
    return ALERT if days > STALE_ALERT_DAYS else WARN if days > STALE_WARN_DAYS else OK


def check_staleness(conn, today):
    newest = conn.execute("SELECT MAX(bill_to) FROM bills").fetchone()[0]
    if newest is None:
        return [dict(check="data_staleness", status=WARN, measured="no bills", threshold="", message="No bills are stored.")]
    days = (today - date.fromisoformat(newest)).days
    out = [dict(check="data_staleness", status=staleness_status(days), measured=f"{days} days",
                threshold=f"warn > {STALE_WARN_DAYS}, alert > {STALE_ALERT_DAYS}",
                message=f"The newest bill period ended {newest}, {days} days before {today}.")]
    for schema, sql, name in (("mart", "SELECT MAX(d.calendar_date) FROM mart.fact_bill f JOIN mart.dim_date d ON d.date_key = f.bill_to_key", "mart_currency"),
                              ("features", "SELECT COUNT(DISTINCT bill_key) FROM features.feature_values", "features_currency")):
        if not _table_exists(conn, schema, "fact_bill" if schema == "mart" else "feature_values"):
            out.append(dict(check=name, status=WARN, measured="not built", threshold="built",
                            message=f"The {schema} schema has not been built on this database."))
            continue
        value = conn.execute(sql).fetchone()[0]
        if schema == "mart":
            ok = value == newest
            msg = f"The marts reach bill period end {value}; the newest stored bill ends {newest}."
        else:
            bills = conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
            ok = value == bills
            msg = f"The feature store covers {value} bills of {bills} stored."
        out.append(dict(check=name, status=OK if ok else WARN, measured=str(value), threshold="covers newest", message=msg))
    return out


def check_queue(conn):
    out = []
    for status, n in conn.execute("SELECT status, COUNT(*) FROM parse_review_items GROUP BY status ORDER BY status"):
        out.append(dict(check=f"queue_depth:{status}", status=OK, measured=str(n), threshold="", message=f"{n} queue items are {status}."))
    for signal, n in conn.execute("SELECT signal, COUNT(*) FROM parse_review_items WHERE status = 'open' GROUP BY signal ORDER BY signal"):
        out.append(dict(check=f"queue_reason:{signal}", status=OK, measured=str(n), threshold="",
                        message=f"{n} open: {reason_for({'signal': signal})}"))
    return out


def check_audit_coverage(conn):
    audited = conn.execute("SELECT COUNT(DISTINCT bill_id) FROM audit_runs").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
    return [dict(check="audit_coverage", status=OK, measured=f"{audited}/{total}", threshold="",
                 message=f"{audited} of {total} bills have an audit run.")]


def check_repeat_failures(conn):
    index = utility_index(conn)
    groups = {}
    for item in su.rows(conn, "SELECT * FROM parse_review_items WHERE status = 'open'"):
        utility = utility_for(item, index)
        groups.setdefault((utility, item["schedule_code"], item["signal"]), []).append(item)
    out = []
    for (utility, schedule, signal), items in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]), kv[0][2])):
        if len(items) >= REPEAT_MIN:
            out.append(dict(check="repeat_failure", status=ALERT, measured=str(len(items)), threshold=f">= {REPEAT_MIN}",
                            utility=utility, message=f"{utility or 'Unknown company'} schedule {schedule}: {len(items)} open "
                            f"items for the same reason: {reason_for({'signal': signal})}"))
    if not out:
        out.append(dict(check="repeat_failure", status=OK, measured="0", threshold=f">= {REPEAT_MIN}",
                        message="No schedule has repeated open items for one reason."))
    return out


def check_growth(conn, open_now):
    prev = conn.execute("SELECT queue_open FROM monitor.monitor_runs ORDER BY run_id DESC LIMIT 1").fetchone() \
        if _table_exists(conn, "monitor", "monitor_runs") else None
    if prev is None:
        return [dict(check="queue_growth", status=OK, measured="first run", threshold=f"> {GROWTH_WARN}",
                     message="First monitor run: no previous run to compare against.")]
    growth = open_now - prev[0]
    return [dict(check="queue_growth", status=WARN if growth > GROWTH_WARN else OK, measured=str(growth),
                 threshold=f"> {GROWTH_WARN}", message=f"Open queue items changed by {growth:+d} since the previous run ({prev[0]} to {open_now}).")]


def run(conn, today=None, paths=None):
    su.attach(conn, {"mart": None, "features": None, "monitor": None, **(paths or {})})
    today = today or date.today()
    open_now = conn.execute("SELECT COUNT(*) FROM parse_review_items WHERE status = 'open'").fetchone()[0]
    findings = (check_staleness(conn, today) + check_queue(conn) + check_audit_coverage(conn)
                + check_repeat_failures(conn) + check_growth(conn, open_now))
    conn.execute("BEGIN IMMEDIATE")
    try:
        su.ensure_schema(conn, "monitor")
        conn.execute("""CREATE TABLE IF NOT EXISTS monitor.monitor_runs (run_id INTEGER NOT NULL, run_at TEXT NOT NULL,
            as_of TEXT NOT NULL, queue_open INTEGER NOT NULL, findings INTEGER NOT NULL, alerts INTEGER NOT NULL,
            outbox_rows INTEGER NOT NULL, outbox_note TEXT NOT NULL, PRIMARY KEY (run_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS monitor.monitor_findings (finding_id INTEGER NOT NULL, run_id INTEGER NOT NULL,
            check_name TEXT NOT NULL, status TEXT NOT NULL, measured TEXT, threshold TEXT, message TEXT NOT NULL,
            PRIMARY KEY (finding_id))""")
        run_id = (conn.execute("SELECT COALESCE(MAX(run_id), 0) FROM monitor.monitor_runs").fetchone()[0] or 0) + 1
        next_id = (conn.execute("SELECT COALESCE(MAX(finding_id), 0) FROM monitor.monitor_findings").fetchone()[0] or 0) + 1
        for i, f in enumerate(findings):
            f["finding_id"] = next_id + i
            su.insert(conn, "monitor.monitor_findings", [{"finding_id": f["finding_id"], "run_id": run_id,
                      "check_name": f["check"], "status": f["status"], "measured": f["measured"],
                      "threshold": f["threshold"], "message": f["message"]}])
        alerts = sum(1 for f in findings if f["status"] == ALERT)
        su.insert(conn, "monitor.monitor_runs", [{"run_id": run_id, "run_at": datetime.now(timezone.utc).isoformat(),
                  "as_of": today.isoformat(), "queue_open": open_now, "findings": len(findings), "alerts": alerts,
                  "outbox_rows": 0, "outbox_note": DELIVERY_NOTE}])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"run_id": run_id, "queue_open": open_now, "alerts": alerts, "findings": [(f["check"], f["status"], f["measured"]) for f in findings]}


def main(argv=None):
    conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"))
    try:
        print(json.dumps(run(conn), default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
