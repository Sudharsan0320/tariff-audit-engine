"""B20 analytics marts: a star schema in schema `mart`, rebuilt from the operational tables, reconciled to the cent.

The build reads the operational tables with SELECT only and writes only inside `mart`. It is a full
refresh in one transaction: every mart table except `mart_build_log` is dropped and rebuilt, the
reconciliation runs, and the transaction commits only when every check passes. Any unresolvable
dimension key or failed check raises `MartBuildError` and rolls back, so nothing is written.

Exactness: amounts and rates stay `Decimal` end to end. Operational floats (bills.total_billed,
bill_charges.amount, kWh, tariff_components.rate) are converted once with Decimal(str(value)), the
B4 rule, and never used in arithmetic; audit amounts are stored as decimal text and parsed as Decimal.
On Postgres the measure columns are `DECIMAL` (exact numeric; the db shim's DDL translation rewrites
the keyword NUMERIC to DOUBLE PRECISION, so NUMERIC is deliberately not used). On SQLite (the test
fixtures) `mart` is an attached database and measures are stored as decimal text.

Current-run rule: for each bill, its audit lines come from the latest audit run that covers it
(latest created_at, then highest id). Bills with no run carry NULL audit measures, never zeros.

Keys: dim_schedule / dim_component / fact keys reuse the operational ids; dim_account and
dim_utility keys are ordinals over their sorted natural keys; dim_date's key is YYYYMMDD.
A NULL schedule / utility / component key means the source names none (a bill no loaded version
covers, a charge no audit line priced); a key the source names but the dimension lacks fails the build.
"""

import hashlib
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

try:
    from src import db
except ImportError:
    import db

ROOT = Path(__file__).resolve().parents[1]
CENT = Decimal("0.01")
ZERO = Decimal("0")

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
SEASON_RANGE = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)_(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)$")

TABLES = ("dim_date", "dim_utility", "dim_account", "dim_schedule", "dim_component",
          "fact_bill", "fact_charge", "fact_audit_line")
MATCH = "MATCH"
OUT_OF_SCOPE = "OUT-OF-SCOPE"


class MartBuildError(RuntimeError):
    """The build or reconciliation failed; nothing was written."""


def _sqlite(conn):
    return isinstance(conn, sqlite3.Connection)


def dec(value):
    """Exact decimal from a stored value (B4: Decimal(str(value))); None stays None."""
    if value is None or value == "":
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _param(conn, value):
    if isinstance(value, Decimal) and _sqlite(conn):
        return str(value)
    return value


def _ddl(conn):
    num = "TEXT" if _sqlite(conn) else "DECIMAL"
    return {
        "dim_date": f"""(date_key INTEGER NOT NULL, calendar_date TEXT NOT NULL, year INTEGER NOT NULL,
            month INTEGER NOT NULL, year_month TEXT NOT NULL, rate_seasons TEXT, PRIMARY KEY (date_key))""",
        "dim_utility": """(utility_key INTEGER NOT NULL, company TEXT NOT NULL, state TEXT NOT NULL,
            book_label TEXT, PRIMARY KEY (utility_key))""",
        "dim_account": """(account_key INTEGER NOT NULL, account_number TEXT NOT NULL, utility TEXT, state TEXT,
            customer_name TEXT, service_address TEXT, meter_number TEXT, customer_class TEXT, voltage TEXT,
            delivery_phase TEXT, turn_on_date TEXT, current_rate_raw TEXT, PRIMARY KEY (account_key))""",
        "dim_schedule": """(schedule_key INTEGER NOT NULL, utility_key INTEGER NOT NULL, schedule_code TEXT NOT NULL,
            schedule_kind TEXT, book_label TEXT, version INTEGER, effective_from TEXT, effective_to TEXT,
            status TEXT, PRIMARY KEY (schedule_key))""",
        "dim_component": f"""(component_key INTEGER NOT NULL, schedule_key INTEGER NOT NULL, component_type TEXT,
            billing_category TEXT, unit TEXT, variant TEXT, billing_class TEXT, season TEXT, tier_min {num},
            tier_max {num}, tier_unit TEXT, rate {num}, PRIMARY KEY (component_key))""",
        "fact_bill": f"""(bill_key INTEGER NOT NULL, account_key INTEGER NOT NULL, utility_key INTEGER,
            schedule_key INTEGER, billed_schedule_code TEXT, bill_from_key INTEGER NOT NULL,
            bill_to_key INTEGER NOT NULL, billing_days INTEGER, kwh {num}, demand_kw {num},
            billed_total {num} NOT NULL, audit_run_id INTEGER, billed_in_scope {num}, expected_total {num},
            delta {num}, matched_lines INTEGER, mismatched_lines INTEGER, out_of_scope_lines INTEGER,
            PRIMARY KEY (bill_key))""",
        "fact_charge": f"""(charge_key INTEGER NOT NULL, bill_key INTEGER NOT NULL, account_key INTEGER NOT NULL,
            bill_to_key INTEGER NOT NULL, component_key INTEGER, schedule_key INTEGER, line_label TEXT,
            disposition TEXT, determinant_type TEXT, determinant {num}, rate {num}, amount {num} NOT NULL,
            PRIMARY KEY (charge_key))""",
        "fact_audit_line": f"""(audit_line_key INTEGER NOT NULL, audit_run_id INTEGER NOT NULL,
            bill_key INTEGER NOT NULL, charge_key INTEGER, component_key INTEGER, schedule_key INTEGER,
            line_label TEXT, disposition TEXT, verdict TEXT, billed_amount {num}, expected_amount {num},
            delta {num}, PRIMARY KEY (audit_line_key))""",
    }


LOG_DDL = """(build_id INTEGER NOT NULL, built_at TEXT NOT NULL, source_counts TEXT NOT NULL,
    mart_counts TEXT NOT NULL, reconciliation TEXT NOT NULL, passed INTEGER NOT NULL, PRIMARY KEY (build_id))"""


def attach(conn, mart_path=None):
    """SQLite only: attach the `mart` database (Postgres uses a real schema)."""
    if _sqlite(conn):
        names = {row[1] for row in conn.execute("PRAGMA database_list").fetchall()}
        if "mart" not in names:
            conn.execute("ATTACH DATABASE ? AS mart", (str(mart_path or ":memory:"),))


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


# ---------------------------------------------------------------- source reads (SELECT only)
def read_source(conn):
    src = {
        "versions": _rows(conn, "SELECT id, state, utility, schedule_code, version, effective_from, effective_to, "
                                "status, schedule_kind, book_label FROM tariff_versions ORDER BY id"),
        "components": _rows(conn, "SELECT id, tariff_version_id, component_type, billing_category, unit, variant, "
                                  "billing_class, season, tier_min, tier_max, tier_unit, rate FROM tariff_components "
                                  "ORDER BY id"),
        "accounts": _rows(conn, "SELECT * FROM accounts ORDER BY account_number"),
        "bills": _rows(conn, "SELECT id, account_number, bill_from, bill_to, billing_days, schedule_code, "
                             "total_consumption_kwh, demand_kw, total_billed FROM bills ORDER BY id"),
        "charges": _rows(conn, "SELECT id, bill_id, line_label, amount FROM bill_charges ORDER BY id"),
        "runs": _rows(conn, "SELECT id, bill_id, mapping_version, created_at, report_json FROM audit_runs ORDER BY id"),
        "lines": _rows(conn, "SELECT id, audit_run_id, bill_charge_id, line_label, disposition, verdict, "
                             "tariff_component_id, billed_amount, expected_amount, delta, detail FROM audit_lines "
                             "ORDER BY id"),
    }
    return src


def current_runs(runs):
    """bill_id -> the latest run covering it (latest created_at, then highest id)."""
    chosen = {}
    for run in runs:
        best = chosen.get(run["bill_id"])
        if best is None or (str(run["created_at"]), run["id"]) > (str(best["created_at"]), best["id"]):
            chosen[run["bill_id"]] = run
    return chosen


def _season_codes(components):
    codes = set()
    for component in components:
        season = str(component["season"] or "").strip().lower()
        if SEASON_RANGE.match(season):
            codes.add(season)
    return sorted(codes)


def _in_range(month, code):
    start, end = (MONTHS[part] for part in code.split("_"))
    return start <= month <= end if start <= end else (month >= start or month <= end)


def _date_key(text):
    return int(str(text).replace("-", ""))


# ---------------------------------------------------------------- build rows
def build_rows(src):
    def fail(message):
        raise MartBuildError(message)

    versions = {v["id"]: v for v in src["versions"]}
    components = {c["id"]: c for c in src["components"]}
    charges = {c["id"]: c for c in src["charges"]}

    # dim_utility: one row per (company, state, book label)
    utility_keys = {key: i for i, key in enumerate(sorted(
        {(v["utility"], v["state"], v["book_label"] or "") for v in src["versions"]}), start=1)}
    dim_utility = [{"utility_key": k, "company": u, "state": s, "book_label": b or None}
                   for (u, s, b), k in utility_keys.items()]

    def utility_key_of(version):
        return utility_keys[(version["utility"], version["state"], version["book_label"] or "")]

    dim_schedule = [{
        "schedule_key": v["id"], "utility_key": utility_key_of(v), "schedule_code": v["schedule_code"],
        "schedule_kind": v["schedule_kind"], "book_label": v["book_label"], "version": v["version"],
        "effective_from": v["effective_from"], "effective_to": v["effective_to"], "status": v["status"],
    } for v in src["versions"]]

    dim_component = []
    for c in src["components"]:
        if c["tariff_version_id"] not in versions:
            fail(f"component {c['id']} names version {c['tariff_version_id']}, which does not exist")
        dim_component.append({
            "component_key": c["id"], "schedule_key": c["tariff_version_id"], "component_type": c["component_type"],
            "billing_category": c["billing_category"], "unit": c["unit"], "variant": c["variant"],
            "billing_class": c["billing_class"], "season": c["season"], "tier_min": dec(c["tier_min"]),
            "tier_max": dec(c["tier_max"]), "tier_unit": c["tier_unit"], "rate": dec(c["rate"]),
        })

    # current runs and their lines
    runs = current_runs(src["runs"])
    run_ids = {run["id"] for run in runs.values()}
    lines = [line for line in src["lines"] if line["audit_run_id"] in run_ids]
    run_bill = {run["id"]: bill_id for bill_id, run in runs.items()}
    run_version = {}
    for bill_id, run in runs.items():
        report = json.loads(run["report_json"])
        version_id = (report.get("tariff_version") or {}).get("id")
        if version_id is None or version_id not in versions:
            fail(f"audit run {run['id']} names tariff version {version_id!r}, which does not resolve")
        run_version[run["id"]] = version_id

    # dim_account: utility/state from the versions its bills' current runs priced against
    account_numbers = [a["account_number"] for a in src["accounts"]]
    account_keys = {number: i for i, number in enumerate(sorted(account_numbers), start=1)}
    account_util = {}
    for bill in src["bills"]:
        run = runs.get(bill["id"])
        if run:
            v = versions[run_version[run["id"]]]
            account_util.setdefault(bill["account_number"], set()).add((v["utility"], v["state"]))
    dim_account = []
    for a in src["accounts"]:
        utils = account_util.get(a["account_number"], set())
        if len(utils) > 1:
            fail(f"account {a['account_number']} was audited against more than one utility: {sorted(utils)}")
        utility, state = next(iter(utils)) if utils else (None, None)
        dim_account.append({
            "account_key": account_keys[a["account_number"]], "account_number": a["account_number"],
            "utility": utility, "state": state, **{k: a.get(k) for k in (
                "customer_name", "service_address", "meter_number", "customer_class", "voltage",
                "delivery_phase", "turn_on_date", "current_rate_raw")}})

    # dim_date: calendar spine over the bills' date range
    if src["bills"]:
        first = min(date.fromisoformat(b["bill_from"]) for b in src["bills"])
        last = max(date.fromisoformat(b["bill_to"]) for b in src["bills"])
    else:
        first = last = None
    seasons = _season_codes(src["components"])
    dim_date = []
    day = first
    while day is not None and day <= last:
        covering = [code for code in seasons if _in_range(day.month, code)]
        dim_date.append({"date_key": int(day.strftime("%Y%m%d")), "calendar_date": day.isoformat(),
                         "year": day.year, "month": day.month, "year_month": day.strftime("%Y-%m"),
                         "rate_seasons": ",".join(covering) or None})
        day += timedelta(days=1)
    date_keys = {row["date_key"] for row in dim_date}

    # fact_audit_line
    fact_audit_line = []
    line_by_charge = {}
    for line in lines:
        cid = line["tariff_component_id"]
        if cid is not None and cid not in components:
            fail(f"audit line {line['id']} names component {cid}, which does not resolve")
        if line["bill_charge_id"] is not None and line["bill_charge_id"] not in charges:
            fail(f"audit line {line['id']} names bill charge {line['bill_charge_id']}, which does not resolve")
        bill_id = run_bill[line["audit_run_id"]]
        fact_audit_line.append({
            "audit_line_key": line["id"], "audit_run_id": line["audit_run_id"], "bill_key": bill_id,
            "charge_key": line["bill_charge_id"], "component_key": cid,
            "schedule_key": components[cid]["tariff_version_id"] if cid is not None else None,
            "line_label": line["line_label"], "disposition": line["disposition"], "verdict": line["verdict"],
            "billed_amount": dec(line["billed_amount"]), "expected_amount": dec(line["expected_amount"]),
            "delta": dec(line["delta"]),
        })
        if line["bill_charge_id"] is not None:
            line_by_charge[line["bill_charge_id"]] = line

    # fact_bill
    bills = {b["id"]: b for b in src["bills"]}
    fact_bill = []
    for b in src["bills"]:
        if b["account_number"] not in account_keys:
            fail(f"bill {b['id']} names account {b['account_number']}, which does not resolve")
        for edge in ("bill_from", "bill_to"):
            if _date_key(b[edge]) not in date_keys:
                fail(f"bill {b['id']} {edge} {b[edge]} is outside the date spine")
        run = runs.get(b["id"])
        row = {"bill_key": b["id"], "account_key": account_keys[b["account_number"]], "utility_key": None,
               "schedule_key": None, "billed_schedule_code": b["schedule_code"],
               "bill_from_key": _date_key(b["bill_from"]), "bill_to_key": _date_key(b["bill_to"]),
               "billing_days": b["billing_days"], "kwh": dec(b["total_consumption_kwh"]),
               "demand_kw": dec(b["demand_kw"]), "billed_total": dec(b["total_billed"]), "audit_run_id": None,
               "billed_in_scope": None, "expected_total": None, "delta": None,
               "matched_lines": None, "mismatched_lines": None, "out_of_scope_lines": None}
        if run:
            mine = [l for l in fact_audit_line if l["audit_run_id"] == run["id"]]
            in_scope = [l for l in mine if l["verdict"] != OUT_OF_SCOPE]
            billed = sum((l["billed_amount"] or ZERO for l in in_scope), ZERO)
            expected = sum((l["expected_amount"] or ZERO for l in in_scope), ZERO)
            version = versions[run_version[run["id"]]]
            row.update({
                "schedule_key": version["id"], "utility_key": utility_key_of(version), "audit_run_id": run["id"],
                "billed_in_scope": billed, "expected_total": expected, "delta": billed - expected,
                "matched_lines": sum(1 for l in mine if l["verdict"] == MATCH),
                "mismatched_lines": sum(1 for l in mine if l["verdict"] not in (MATCH, OUT_OF_SCOPE)),
                "out_of_scope_lines": sum(1 for l in mine if l["verdict"] == OUT_OF_SCOPE),
            })
        fact_bill.append(row)

    # fact_charge
    fact_charge = []
    for c in src["charges"]:
        bill = bills.get(c["bill_id"])
        if bill is None:
            fail(f"bill charge {c['id']} names bill {c['bill_id']}, which does not resolve")
        line = line_by_charge.get(c["id"])
        detail = json.loads(line["detail"]) if line and line.get("detail") else {}
        cid = line["tariff_component_id"] if line else None
        fact_charge.append({
            "charge_key": c["id"], "bill_key": c["bill_id"], "account_key": account_keys[bill["account_number"]],
            "bill_to_key": _date_key(bill["bill_to"]), "component_key": cid,
            "schedule_key": components[cid]["tariff_version_id"] if cid is not None else None,
            "line_label": c["line_label"], "disposition": line["disposition"] if line else None,
            "determinant_type": detail.get("determinant_type"), "determinant": dec(detail.get("determinant_value")),
            "rate": dec(detail.get("rate")), "amount": dec(c["amount"]),
        })

    return {"dim_date": dim_date, "dim_utility": dim_utility, "dim_account": dim_account,
            "dim_schedule": dim_schedule, "dim_component": dim_component, "fact_bill": fact_bill,
            "fact_charge": fact_charge, "fact_audit_line": fact_audit_line}, runs


# ---------------------------------------------------------------- reconciliation
def _q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def reconcile(conn, src=None):
    """Check every mart identity against the source. Returns {passed, checks, ...}; reads only."""
    attach(conn)
    src = src or read_source(conn)
    runs = current_runs(src["runs"])
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    source_totals = {b["id"]: dec(b["total_billed"]) for b in src["bills"]}
    mart_bills = {r[0]: dec(r[1]) for r in _q(conn, "SELECT bill_key, billed_total FROM mart.fact_bill")}
    check("fact_bill count equals source bills", len(mart_bills) == len(source_totals),
          {"mart": len(mart_bills), "source": len(source_totals)})
    diffs = [abs((mart_bills.get(bid) or ZERO) - total) for bid, total in source_totals.items()]
    largest = max(diffs, default=ZERO)
    check("every bill's billed total equals its source total", set(mart_bills) == set(source_totals) and largest == 0,
          {"largest_difference": str(largest.quantize(CENT))})

    charge_count = _q(conn, "SELECT COUNT(*) FROM mart.fact_charge")[0][0]
    check("fact_charge count equals source charges", charge_count == len(src["charges"]),
          {"mart": charge_count, "source": len(src["charges"])})
    per_bill = {}
    for bill_key, amount in _q(conn, "SELECT bill_key, amount FROM mart.fact_charge"):
        per_bill[bill_key] = per_bill.get(bill_key, ZERO) + dec(amount)
    charge_diffs = [abs(per_bill.get(bid, ZERO) - total) for bid, total in mart_bills.items()]
    largest_charge = max(charge_diffs, default=ZERO)
    check("fact_charge per-bill sums equal fact_bill billed totals", largest_charge == 0,
          {"largest_difference": str(largest_charge.quantize(CENT))})

    run_ids = {run["id"] for run in runs.values()}
    selected_lines = sum(1 for line in src["lines"] if line["audit_run_id"] in run_ids)
    line_count = _q(conn, "SELECT COUNT(*) FROM mart.fact_audit_line")[0][0]
    check("fact_audit_line count equals the selected current runs' lines", line_count == selected_lines,
          {"mart": line_count, "selected_runs": sorted(run_ids), "source_lines": selected_lines})

    audit_totals = {}
    mismatches = []
    for bill_id, run in sorted(runs.items()):
        totals = json.loads(run["report_json"])["like_for_like_totals"]
        rows = _q(conn, "SELECT verdict, billed_amount, expected_amount FROM mart.fact_audit_line "
                        "WHERE audit_run_id = ?", (run["id"],))
        expected = sum((dec(r[2]) or ZERO for r in rows if r[0] != OUT_OF_SCOPE), ZERO)
        billed = sum((dec(r[1]) or ZERO for r in rows if r[0] != OUT_OF_SCOPE), ZERO)
        fb = _q(conn, "SELECT expected_total, billed_in_scope, delta FROM mart.fact_bill WHERE bill_key = ?", (bill_id,))
        audit_totals[bill_id] = {"expected": str(expected), "billed_in_scope": str(billed),
                                 "report_expected": totals["expected"], "report_billed": totals["billed"],
                                 "report_delta": totals["delta"]}
        if (expected != dec(totals["expected"]) or billed != dec(totals["billed"])
                or not fb or dec(fb[0][0]) != expected or dec(fb[0][1]) != billed
                or dec(fb[0][2]) != dec(totals["delta"])):
            mismatches.append(bill_id)
    check("fact_audit_line per-bill sums equal the audit totals (and fact_bill)", not mismatches,
          {"per_bill": audit_totals, "mismatched_bills": mismatches})

    uncovered_nonnull = _q(conn, "SELECT COUNT(*) FROM mart.fact_bill WHERE audit_run_id IS NULL AND "
                                 "(expected_total IS NOT NULL OR delta IS NOT NULL OR matched_lines IS NOT NULL)")[0][0]
    check("uncovered bills carry NULL audit measures", uncovered_nonnull == 0, {"violations": uncovered_nonnull})

    fk_sql = {
        "fact_bill.account_key": "SELECT COUNT(*) FROM mart.fact_bill f LEFT JOIN mart.dim_account d ON d.account_key = f.account_key WHERE d.account_key IS NULL",
        "fact_bill.utility_key": "SELECT COUNT(*) FROM mart.fact_bill f LEFT JOIN mart.dim_utility d ON d.utility_key = f.utility_key WHERE f.utility_key IS NOT NULL AND d.utility_key IS NULL",
        "fact_bill.schedule_key": "SELECT COUNT(*) FROM mart.fact_bill f LEFT JOIN mart.dim_schedule d ON d.schedule_key = f.schedule_key WHERE f.schedule_key IS NOT NULL AND d.schedule_key IS NULL",
        "fact_bill.bill_from_key": "SELECT COUNT(*) FROM mart.fact_bill f LEFT JOIN mart.dim_date d ON d.date_key = f.bill_from_key WHERE d.date_key IS NULL",
        "fact_bill.bill_to_key": "SELECT COUNT(*) FROM mart.fact_bill f LEFT JOIN mart.dim_date d ON d.date_key = f.bill_to_key WHERE d.date_key IS NULL",
        "fact_charge.bill_key": "SELECT COUNT(*) FROM mart.fact_charge f LEFT JOIN mart.fact_bill d ON d.bill_key = f.bill_key WHERE d.bill_key IS NULL",
        "fact_charge.account_key": "SELECT COUNT(*) FROM mart.fact_charge f LEFT JOIN mart.dim_account d ON d.account_key = f.account_key WHERE d.account_key IS NULL",
        "fact_charge.bill_to_key": "SELECT COUNT(*) FROM mart.fact_charge f LEFT JOIN mart.dim_date d ON d.date_key = f.bill_to_key WHERE d.date_key IS NULL",
        "fact_charge.component_key": "SELECT COUNT(*) FROM mart.fact_charge f LEFT JOIN mart.dim_component d ON d.component_key = f.component_key WHERE f.component_key IS NOT NULL AND d.component_key IS NULL",
        "fact_charge.schedule_key": "SELECT COUNT(*) FROM mart.fact_charge f LEFT JOIN mart.dim_schedule d ON d.schedule_key = f.schedule_key WHERE f.schedule_key IS NOT NULL AND d.schedule_key IS NULL",
        "fact_audit_line.bill_key": "SELECT COUNT(*) FROM mart.fact_audit_line f LEFT JOIN mart.fact_bill d ON d.bill_key = f.bill_key WHERE d.bill_key IS NULL",
        "fact_audit_line.charge_key": "SELECT COUNT(*) FROM mart.fact_audit_line f LEFT JOIN mart.fact_charge d ON d.charge_key = f.charge_key WHERE f.charge_key IS NOT NULL AND d.charge_key IS NULL",
        "fact_audit_line.component_key": "SELECT COUNT(*) FROM mart.fact_audit_line f LEFT JOIN mart.dim_component d ON d.component_key = f.component_key WHERE f.component_key IS NOT NULL AND d.component_key IS NULL",
        "fact_audit_line.schedule_key": "SELECT COUNT(*) FROM mart.fact_audit_line f LEFT JOIN mart.dim_schedule d ON d.schedule_key = f.schedule_key WHERE f.schedule_key IS NOT NULL AND d.schedule_key IS NULL",
        "dim_component.schedule_key": "SELECT COUNT(*) FROM mart.dim_component f LEFT JOIN mart.dim_schedule d ON d.schedule_key = f.schedule_key WHERE d.schedule_key IS NULL",
        "dim_schedule.utility_key": "SELECT COUNT(*) FROM mart.dim_schedule f LEFT JOIN mart.dim_utility d ON d.utility_key = f.utility_key WHERE d.utility_key IS NULL",
    }
    orphans = {name: _q(conn, sql)[0][0] for name, sql in fk_sql.items()}
    check("every fact and dimension foreign key resolves", not any(orphans.values()), {"orphans": orphans})

    return {"passed": all(c["passed"] for c in checks), "checks": checks,
            "current_runs": {str(bill_id): run["id"] for bill_id, run in sorted(runs.items())}}


# ---------------------------------------------------------------- build
def mart_counts(conn):
    return {table: _q(conn, f"SELECT COUNT(*) FROM mart.{table}")[0][0] for table in TABLES}


def source_counts(src):
    return {"bills": len(src["bills"]), "bill_charges": len(src["charges"]), "audit_runs": len(src["runs"]),
            "audit_lines": len(src["lines"]), "tariff_versions": len(src["versions"]),
            "tariff_components": len(src["components"]), "accounts": len(src["accounts"])}


def build(conn, mart_path=None):
    """Full refresh of `mart` in one transaction; commits only when reconciliation passes."""
    attach(conn, mart_path)
    src = read_source(conn)
    rows, _runs = build_rows(src)
    ddl = _ddl(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _sqlite(conn):
            conn.execute("CREATE SCHEMA IF NOT EXISTS mart")
        for table in reversed(TABLES):
            conn.execute(f"DROP TABLE IF EXISTS mart.{table}")
        conn.execute(f"CREATE TABLE IF NOT EXISTS mart.mart_build_log {LOG_DDL}")
        for table in TABLES:
            conn.execute(f"CREATE TABLE mart.{table} {ddl[table]}")
            if rows[table]:
                columns = list(rows[table][0].keys())
                sql = (f"INSERT INTO mart.{table} ({', '.join(columns)}) "
                       f"VALUES ({', '.join('?' for _ in columns)})")
                conn.executemany(sql, [tuple(_param(conn, row[c]) for c in columns) for row in rows[table]])
        report = reconcile(conn, src)
        if not report["passed"]:
            failed = [c["check"] for c in report["checks"] if not c["passed"]]
            raise MartBuildError("Reconciliation failed; nothing was written: " + "; ".join(failed)
                                 + "\n" + json.dumps(report, indent=2, default=str))
        counts = mart_counts(conn)
        build_id = (_q(conn, "SELECT COALESCE(MAX(build_id), 0) FROM mart.mart_build_log")[0][0] or 0) + 1
        conn.execute(
            "INSERT INTO mart.mart_build_log (build_id, built_at, source_counts, mart_counts, reconciliation, passed) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (build_id, datetime.now(timezone.utc).isoformat(), json.dumps(source_counts(src), sort_keys=True),
             json.dumps(counts, sort_keys=True), json.dumps(report, sort_keys=True, default=str), 1))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"build_id": build_id, "source_counts": source_counts(src), "mart_counts": counts, "reconciliation": report}


def _norm(value):
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value == value.to_integral() else str(value.normalize())
    return value


def content_hashes(conn):
    """sha256 of each mart table's rows in key order (the build log excluded: it grows by design)."""
    attach(conn)
    hashes = {}
    for table in TABLES:
        cursor = conn.execute(f"SELECT * FROM mart.{table} ORDER BY 1")
        digest = hashlib.sha256()
        for row in cursor.fetchall():
            digest.update(json.dumps([_norm(dec(v)) if isinstance(v, (Decimal,)) else v for v in row],
                                     default=str).encode())
            digest.update(b"\n")
        hashes[table] = digest.hexdigest()
    return hashes


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Build or reconcile the B20 analytics marts.")
    parser.add_argument("--reconcile-only", action="store_true")
    args = parser.parse_args(argv)
    conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"))
    try:
        result = reconcile(conn) if args.reconcile_only else build(conn)
    except MartBuildError as error:
        print(f"FAIL: {error}")
        return 1
    finally:
        conn.close()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
