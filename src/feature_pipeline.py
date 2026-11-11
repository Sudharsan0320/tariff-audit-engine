"""B27 shared, versioned feature pipeline: per-bill features from the B20 marts into schema `features`.

Reads only the `mart` schema (MART_TABLES); writes only the `features` schema. One feature-set version
is built in one transaction: that version's registry and values are replaced, other versions' rows are
never touched, and a build-log row records the checks. The registry rows come from FEATURES, the same
definitions the code computes with.

Point in time: a bill's features use only that bill and the account's strictly earlier bills, ordered by
bill end date then bill key. Appending a later bill can therefore never change an earlier bill's values.
The audit features describe the bill's own current audit run (the B20 current-run rule).

Exactness: values are Decimal end to end. Sums and differences are exact; quotients (rates, means,
percentages) are quantized to 10 decimal places with ROUND_HALF_UP, the one rounding rule here.
On Postgres values are stored as DECIMAL (exact numeric); on SQLite (the test fixtures) as text.
"""

import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

try:
    from src import db
except ImportError:
    import db

ROOT = Path(__file__).resolve().parents[1]
FEATURE_SET_VERSION = "v1"
PLACES = Decimal("1E-10")
HUNDRED = Decimal("100")
ZERO = Decimal("0")
OUT_OF_SCOPE = "OUT-OF-SCOPE"
MATCH = "MATCH"
MART_TABLES = ("mart.dim_account", "mart.dim_date", "mart.fact_bill", "mart.fact_charge", "mart.fact_audit_line")
TABLES = ("feature_definitions", "feature_values", "feature_build_log")


class FeatureBuildError(RuntimeError):
    """The build failed its checks; nothing was written."""


def _f(name, dtype, description, reads, null_rule):
    return {"feature_name": name, "data_type": dtype, "description": description,
            "mart_tables": ",".join(reads), "null_rule": null_rule}


FB, FC, FA, DD = "mart.fact_bill", "mart.fact_charge", "mart.fact_audit_line", "mart.dim_date"
NEVER = "never NULL"
UNAUDITED = "NULL when the bill has no current audit run"
FIRST = "NULL for the account's first bill"
FEATURES = [
    _f("billed_total", "decimal", "Printed billed total of the bill.", [FB], NEVER),
    _f("expected_total", "decimal", "Expected in-scope total from the bill's current audit run.", [FB], UNAUDITED),
    _f("delta_amount", "decimal", "In-scope billed minus expected, from the current audit run.", [FB], UNAUDITED),
    _f("delta_pct", "decimal", "delta_amount / expected_total x 100.", [FB],
       UNAUDITED + "; NULL when expected_total is 0"),
    _f("total_kwh", "decimal", "Total kWh of the bill.", [FB], "NULL when the bill stores no kWh"),
    _f("days_in_period", "integer", "Billing days of the bill.", [FB], "NULL when the bill stores no billing days"),
    _f("kwh_per_day", "decimal", "total_kwh / days_in_period.", [FB],
       "NULL when total_kwh or days_in_period is NULL, or days_in_period is 0"),
    _f("billed_per_kwh", "decimal", "billed_total / total_kwh.", [FB], "NULL when total_kwh is NULL or 0"),
    _f("bill_year", "integer", "Calendar year of the bill's end date.", [FB, DD], NEVER),
    _f("bill_month", "integer", "Calendar month of the bill's end date.", [FB, DD], NEVER),
    _f("season_codes", "text", "Stored month-range season codes covering the bill's end month.", [FB, DD],
       "NULL when no stored season code covers the month"),
    _f("days_since_prev_bill", "integer", "Days from the previous bill's end date to this bill's end date.",
       [FB, DD], FIRST),
    _f("kwh_per_day_change_pct", "decimal", "(kwh_per_day - previous kwh_per_day) / previous kwh_per_day x 100.",
       [FB], FIRST + "; NULL when either kwh_per_day is NULL or the previous is 0"),
    _f("trailing_3_kwh_per_day", "decimal", "Mean kwh_per_day over up to three strictly earlier bills.", [FB],
       FIRST + "; earlier bills with NULL kwh_per_day are skipped"),
    _f("trailing_3_billed_total", "decimal", "Mean billed_total over up to three strictly earlier bills.", [FB],
       FIRST),
    _f("bills_so_far", "integer", "Number of the account's bills up to and including this one.", [FB], NEVER),
    _f("has_audit", "integer", "1 when the bill has a current audit run, else 0.", [FB], NEVER),
    _f("verdict_match_rate", "decimal", "MATCH lines / in-scope (non out-of-scope) current audit lines.", [FA],
       UNAUDITED + "; NULL when there are no in-scope lines"),
    _f("mismatched_lines", "integer", "Current audit lines whose verdict is neither MATCH nor OUT-OF-SCOPE.", [FA],
       UNAUDITED),
    _f("max_abs_line_delta", "decimal", "Largest absolute line delta among the current audit lines.", [FA],
       UNAUDITED + "; NULL when no line carries a delta"),
    _f("charges_classified_pct", "decimal",
       "Share (x 100) of the bill's charged amount whose charge line has a known tariff component.", [FC],
       "NULL when the bill's charged amount sums to 0"),
]
FEATURE_NAMES = [f["feature_name"] for f in FEATURES]


def _sqlite(conn):
    return isinstance(conn, sqlite3.Connection)


def dec(value):
    if value is None or value == "":
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def q10(value):
    return None if value is None else value.quantize(PLACES, rounding=ROUND_HALF_UP)


def ratio(numerator, denominator, scale=Decimal("1")):
    if numerator is None or denominator is None or denominator == 0:
        return None
    return q10(numerator * scale / denominator)


def attach(conn, mart_path=None, features_path=None):
    """SQLite only: attach `mart` and `features` (Postgres uses real schemas)."""
    if _sqlite(conn):
        names = {row[1] for row in conn.execute("PRAGMA database_list").fetchall()}
        if "mart" not in names:
            conn.execute("ATTACH DATABASE ? AS mart", (str(mart_path or ":memory:"),))
        if "features" not in names:
            conn.execute("ATTACH DATABASE ? AS features", (str(features_path or ":memory:"),))


# ---------------------------------------------------------------- reads (mart only)
SQL_BILLS = ("SELECT a.account_key, a.account_number, f.bill_key, f.billed_total, f.expected_total, f.delta, "
             "f.kwh, f.billing_days, f.audit_run_id, d.calendar_date, d.year, d.month, d.rate_seasons "
             "FROM mart.fact_bill f JOIN mart.dim_account a ON a.account_key = f.account_key "
             "JOIN mart.dim_date d ON d.date_key = f.bill_to_key ORDER BY a.account_key, d.calendar_date, f.bill_key")
SQL_LINES = "SELECT bill_key, verdict, delta FROM mart.fact_audit_line ORDER BY audit_line_key"
SQL_CHARGES = "SELECT bill_key, component_key, amount FROM mart.fact_charge ORDER BY charge_key"


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def read_marts(conn):
    return {"bills": _rows(conn, SQL_BILLS), "lines": _rows(conn, SQL_LINES), "charges": _rows(conn, SQL_CHARGES)}


# ---------------------------------------------------------------- compute
def compute(marts):
    """{(account_key, bill_key): {feature: value}} using only each bill and its strictly earlier bills."""
    lines, charges = {}, {}
    for line in marts["lines"]:
        lines.setdefault(line["bill_key"], []).append(line)
    for charge in marts["charges"]:
        charges.setdefault(charge["bill_key"], []).append(charge)
    by_account = {}
    for bill in marts["bills"]:
        by_account.setdefault(bill["account_key"], []).append(bill)

    out = {}
    for account_key, bills in by_account.items():
        history = []  # computed rows of strictly earlier bills, in order
        for bill in bills:
            billed = dec(bill["billed_total"])
            kwh = dec(bill["kwh"])
            days = bill["billing_days"]
            kpd = ratio(kwh, Decimal(days)) if days is not None else None
            audited = bill["audit_run_id"] is not None
            expected = dec(bill["expected_total"]) if audited else None
            delta = dec(bill["delta"]) if audited else None
            prev = history[-1] if history else None
            earlier3 = history[-3:]
            kpd3 = [h["kwh_per_day"] for h in earlier3 if h["kwh_per_day"] is not None]
            end = date.fromisoformat(bill["calendar_date"])

            mine = lines.get(bill["bill_key"], [])
            in_scope = [l for l in mine if l["verdict"] != OUT_OF_SCOPE]
            line_deltas = [abs(dec(l["delta"])) for l in mine if dec(l["delta"]) is not None]
            charged = sum((dec(c["amount"]) for c in charges.get(bill["bill_key"], [])), ZERO)
            classified = sum((dec(c["amount"]) for c in charges.get(bill["bill_key"], [])
                              if c["component_key"] is not None), ZERO)

            row = {
                "billed_total": billed,
                "expected_total": expected,
                "delta_amount": delta,
                "delta_pct": ratio(delta, expected, HUNDRED) if audited else None,
                "total_kwh": kwh,
                "days_in_period": days,
                "kwh_per_day": kpd,
                "billed_per_kwh": ratio(billed, kwh),
                "bill_year": bill["year"],
                "bill_month": bill["month"],
                "season_codes": bill["rate_seasons"],
                "days_since_prev_bill": (end - prev["_end"]).days if prev else None,
                "kwh_per_day_change_pct": (ratio(kpd - prev["kwh_per_day"], prev["kwh_per_day"], HUNDRED)
                                           if prev and kpd is not None and prev["kwh_per_day"] is not None
                                           else None),
                "trailing_3_kwh_per_day": q10(sum(kpd3, ZERO) / len(kpd3)) if kpd3 else None,
                "trailing_3_billed_total": (q10(sum((h["billed_total"] for h in earlier3), ZERO) / len(earlier3))
                                            if earlier3 else None),
                "bills_so_far": len(history) + 1,
                "has_audit": 1 if audited else 0,
                "verdict_match_rate": (ratio(Decimal(sum(1 for l in in_scope if l["verdict"] == MATCH)),
                                             Decimal(len(in_scope))) if audited and in_scope else None),
                "mismatched_lines": (sum(1 for l in mine if l["verdict"] not in (MATCH, OUT_OF_SCOPE))
                                     if audited else None),
                "max_abs_line_delta": max(line_deltas) if audited and line_deltas else None,
                "charges_classified_pct": ratio(classified, charged, HUNDRED),
            }
            assert list(row) == FEATURE_NAMES
            out[(account_key, bill["bill_key"])] = row
            history.append(dict(row, _end=end))
    return out


# ---------------------------------------------------------------- store
def _ddl(conn):
    num = "TEXT" if _sqlite(conn) else "DECIMAL"
    return {
        "feature_definitions": """(feature_name TEXT NOT NULL, feature_set_version TEXT NOT NULL,
            description TEXT NOT NULL, data_type TEXT NOT NULL, mart_tables TEXT NOT NULL, null_rule TEXT NOT NULL,
            PRIMARY KEY (feature_name, feature_set_version))""",
        "feature_values": f"""(account_key INTEGER NOT NULL, bill_key INTEGER NOT NULL, feature_name TEXT NOT NULL,
            feature_set_version TEXT NOT NULL, numeric_value {num}, text_value TEXT,
            PRIMARY KEY (bill_key, feature_name, feature_set_version))""",
        "feature_build_log": """(build_id INTEGER NOT NULL, feature_set_version TEXT NOT NULL, built_at TEXT NOT NULL,
            bill_count INTEGER NOT NULL, feature_count INTEGER NOT NULL, row_count INTEGER NOT NULL,
            checks TEXT NOT NULL, PRIMARY KEY (build_id))""",
    }


def _value_columns(conn, feature, value):
    dtype = next(f["data_type"] for f in FEATURES if f["feature_name"] == feature)
    if value is None:
        return None, None
    if dtype == "text":
        return None, str(value)
    number = Decimal(value)
    return (str(number) if _sqlite(conn) else number), None


def build(conn, version=FEATURE_SET_VERSION, mart_path=None, features_path=None):
    """Build one feature-set version in one transaction; other versions' rows are never touched."""
    attach(conn, mart_path, features_path)
    marts = read_marts(conn)
    computed = compute(marts)
    ddl = _ddl(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _sqlite(conn):
            conn.execute("CREATE SCHEMA IF NOT EXISTS features")
        for table in TABLES:
            conn.execute(f"CREATE TABLE IF NOT EXISTS features.{table} {ddl[table]}")
        conn.execute("DELETE FROM features.feature_values WHERE feature_set_version = ?", (version,))
        conn.execute("DELETE FROM features.feature_definitions WHERE feature_set_version = ?", (version,))
        conn.executemany(
            "INSERT INTO features.feature_definitions (feature_name, feature_set_version, description, data_type, "
            "mart_tables, null_rule) VALUES (?, ?, ?, ?, ?, ?)",
            [(f["feature_name"], version, f["description"], f["data_type"], f["mart_tables"], f["null_rule"])
             for f in FEATURES])
        rows = []
        for (account_key, bill_key), values in sorted(computed.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            for feature in FEATURE_NAMES:
                numeric, text = _value_columns(conn, feature, values[feature])
                rows.append((account_key, bill_key, feature, version, numeric, text))
        conn.executemany(
            "INSERT INTO features.feature_values (account_key, bill_key, feature_name, feature_set_version, "
            "numeric_value, text_value) VALUES (?, ?, ?, ?, ?, ?)", rows)
        checks = run_checks(conn, version, marts, computed)
        if not all(c["passed"] for c in checks):
            raise FeatureBuildError("Feature checks failed; nothing was written: "
                                    + json.dumps(checks, default=str))
        build_id = (conn.execute("SELECT COALESCE(MAX(build_id), 0) FROM features.feature_build_log")
                    .fetchone()[0] or 0) + 1
        conn.execute(
            "INSERT INTO features.feature_build_log (build_id, feature_set_version, built_at, bill_count, "
            "feature_count, row_count, checks) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (build_id, version, datetime.now(timezone.utc).isoformat(), len(computed), len(FEATURES), len(rows),
             json.dumps(checks, sort_keys=True, default=str)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"build_id": build_id, "version": version, "bills": len(computed), "features": len(FEATURES),
            "rows": len(rows), "checks": checks}


def run_checks(conn, version, marts, computed):
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    registry = sorted(r[0] for r in conn.execute(
        "SELECT feature_name FROM features.feature_definitions WHERE feature_set_version = ?", (version,)).fetchall())
    check("registry equals the computed feature set", registry == sorted(FEATURE_NAMES),
          {"registry": len(registry), "computed": len(FEATURE_NAMES)})
    count = conn.execute("SELECT COUNT(*) FROM features.feature_values WHERE feature_set_version = ?",
                         (version,)).fetchone()[0]
    check("one value row per bill per feature", count == len(marts["bills"]) * len(FEATURE_NAMES),
          {"rows": count, "bills": len(marts["bills"]), "features": len(FEATURE_NAMES)})
    stored = {r[0]: dec(r[1]) for r in conn.execute(
        "SELECT bill_key, numeric_value FROM features.feature_values WHERE feature_set_version = ? "
        "AND feature_name = 'billed_total'", (version,)).fetchall()}
    source = {b["bill_key"]: dec(b["billed_total"]) for b in marts["bills"]}
    check("billed_total features equal mart.fact_bill to the cent", stored == source,
          {"sum_features": str(sum(stored.values(), ZERO)), "sum_mart": str(sum(source.values(), ZERO))})
    unaudited_bad = sum(1 for (_a, _b), v in computed.items() if v["has_audit"] == 0 and any(
        v[f] is not None for f in ("expected_total", "delta_amount", "delta_pct", "verdict_match_rate",
                                   "mismatched_lines", "max_abs_line_delta")))
    check("unaudited bills carry NULL audit features", unaudited_bad == 0, {"violations": unaudited_bad})
    first_bad = sum(1 for v in computed.values() if v["bills_so_far"] == 1 and any(
        v[f] is not None for f in ("days_since_prev_bill", "kwh_per_day_change_pct", "trailing_3_kwh_per_day",
                                   "trailing_3_billed_total")))
    check("first bills carry NULL history features", first_bad == 0, {"violations": first_bad})
    return checks


# ---------------------------------------------------------------- read accessor
def frame(conn, account_number, version=FEATURE_SET_VERSION):
    """Bill-by-feature pandas DataFrame (index bill_key, columns in registry order) for one account."""
    import pandas as pd

    attach(conn)
    rows = conn.execute(
        "SELECT v.bill_key, v.feature_name, v.numeric_value, v.text_value FROM features.feature_values v "
        "JOIN mart.dim_account a ON a.account_key = v.account_key "
        "WHERE a.account_number = ? AND v.feature_set_version = ? ORDER BY v.bill_key", (account_number, version)
    ).fetchall()
    dtypes = {r[0]: r[1] for r in conn.execute(
        "SELECT feature_name, data_type FROM features.feature_definitions WHERE feature_set_version = ?",
        (version,)).fetchall()}
    data = {}
    for bill_key, feature, numeric, text in rows:
        if dtypes.get(feature) == "text":
            value = text
        elif numeric is None:
            value = None
        elif dtypes.get(feature) == "integer":
            value = int(Decimal(str(numeric)))
        else:
            value = dec(numeric)
        data.setdefault(bill_key, {})[feature] = value
    ordered = [f for f in FEATURE_NAMES if f in dtypes] or list(dtypes)
    # Built from row lists with dtype=object so Decimal / int / None stay as stored (DataFrame.from_dict
    # would coerce ints and None to float / NaN even with dtype=object).
    keys = sorted(data)
    return pd.DataFrame([[data[k].get(f) for f in ordered] for k in keys], index=pd.Index(keys, name="bill_key"),
                        columns=ordered, dtype=object)


def content_hashes(conn, version=None):
    attach(conn)
    out = {}
    for table, order in (("feature_definitions", "feature_name, feature_set_version"),
                         ("feature_values", "bill_key, feature_name, feature_set_version")):
        where = " WHERE feature_set_version = ?" if version else ""
        rows = conn.execute(f"SELECT * FROM features.{table}{where} ORDER BY {order}",
                            (version,) if version else ()).fetchall()
        digest = hashlib.sha256()
        for row in rows:
            digest.update(json.dumps([str(dec(v).normalize()) if isinstance(v, Decimal) else v for v in row],
                                     default=str).encode() + b"\n")
        out[table] = digest.hexdigest()
    return out


def main(argv=None):
    conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"))
    try:
        result = build(conn)
    except FeatureBuildError as error:
        print(f"FAIL: {error}")
        return 1
    finally:
        conn.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
