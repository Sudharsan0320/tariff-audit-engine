"""B23 segmentation: deterministic, readable rules over the B27 features (not clustering).

Reads only `mart` and `features`; writes only `segments`. Every bill gets a label on every dimension; a
NULL feature changes the label (an explicit NULL label), never drops the bill. Labels describe this
account's own bills only.

Rule set v1
- usage_band / cost_band: terciles of the account's own kwh_per_day / billed_total. Cut points are the
  values at sorted positions ceil(n/3) and ceil(2n/3) (1-based); low <= cut_low < mid <= cut_high < high.
  With fewer than MIN_BILLS_FOR_TERCILES bills every bill is `insufficient_history`. NULL value: `no_value`.
- season: the bill's stored season_codes joined exactly as stored (multi-code seasons are kept as joined;
  never reduced to an invented single season). NULL: `no_season_code`.
- audit_state: `unaudited` (has_audit 0) / `audited_mismatch` (mismatched_lines > 0) / `audited_match`.
- trend: `no_previous_bill` for the first bill; `usage_shift_up` / `usage_shift_down` when
  kwh_per_day_change_pct exceeds +/-SHIFT_PCT; `steady` otherwise; `no_change_value` when NULL later on.
- primary: the dimension labels joined with " / ".
"""

import json
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

try:
    from src import db, feature_pipeline as fp, store_util as su
except ImportError:
    import db
    import feature_pipeline as fp
    import store_util as su

ROOT = Path(__file__).resolve().parents[1]
VERSION = "v1"
MIN_BILLS_FOR_TERCILES = 6
SHIFT_PCT = Decimal("20")
DIMENSIONS = ("usage_band", "season", "cost_band", "audit_state", "trend", "primary")
RULES = {
    "usage_band": "terciles of kwh_per_day; cut points = sorted values at positions ceil(n/3), ceil(2n/3)",
    "season": "stored season_codes joined exactly as stored (multi-code kept joined)",
    "cost_band": "terciles of billed_total; cut points = sorted values at positions ceil(n/3), ceil(2n/3)",
    "audit_state": "has_audit=0 -> unaudited; mismatched_lines>0 -> audited_mismatch; else audited_match",
    "trend": "first bill -> no_previous_bill; kwh_per_day_change_pct > +20 -> usage_shift_up; < -20 -> usage_shift_down; else steady",
    "primary": "usage_band / season / cost_band / audit_state / trend",
}


def terciles(values):
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n < MIN_BILLS_FOR_TERCILES:
        return None, None, f"insufficient_history (n={n} < {MIN_BILLS_FOR_TERCILES})"
    return vals[math.ceil(n / 3) - 1], vals[math.ceil(2 * n / 3) - 1], f"terciles (n={n})"


def band(value, low, high, rule):
    if value is None:
        return "no_value"
    if low is None:
        return "insufficient_history"
    return "low" if value <= low else "mid" if value <= high else "high"


def compute(conn, account):
    f = fp.frame(conn, account)
    account_key = conn.execute("SELECT account_key FROM mart.dim_account WHERE account_number = ?",
                               (account,)).fetchone()[0]
    ulow, uhigh, urule = terciles(list(f["kwh_per_day"]))
    clow, chigh, crule = terciles(list(f["billed_total"]))
    labels = []
    for bill_key, r in f.iterrows():
        change = r["kwh_per_day_change_pct"]
        trend = ("no_previous_bill" if r["bills_so_far"] == 1 else "no_change_value" if change is None
                 else "usage_shift_up" if change > SHIFT_PCT else "usage_shift_down" if change < -SHIFT_PCT
                 else "steady")
        audit = ("unaudited" if not r["has_audit"] else
                 "audited_mismatch" if (r["mismatched_lines"] or 0) > 0 else "audited_match")
        row = {"usage_band": band(r["kwh_per_day"], ulow, uhigh, urule), "season": r["season_codes"] or "no_season_code",
               "cost_band": band(r["billed_total"], clow, chigh, crule), "audit_state": audit, "trend": trend}
        row["primary"] = " / ".join(row[d] for d in DIMENSIONS[:-1])
        labels += [{"bill_key": int(bill_key), "account_key": account_key, "dimension": d, "label": row[d],
                    "segment_version": VERSION} for d in DIMENSIONS]
    fired = {"usage_band": (ulow, uhigh, urule), "cost_band": (clow, chigh, crule)}
    definitions = [{"dimension": d, "segment_version": VERSION, "rule_text": RULES[d],
                    "cut_low": fired.get(d, (None,))[0], "cut_high": fired.get(d, (None, None))[1],
                    "rule_fired": fired[d][2] if d in fired else "rule as written"} for d in DIMENSIONS]
    return definitions, labels, len(f)


def build(conn, account, paths=None):
    su.attach(conn, {"mart": None, "features": None, "segments": None, **(paths or {})})
    definitions, labels, bills = compute(conn, account)
    n = su.num_type(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        su.ensure_schema(conn, "segments")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS segments.segment_definitions (dimension TEXT NOT NULL,
            segment_version TEXT NOT NULL, rule_text TEXT NOT NULL, cut_low {n}, cut_high {n}, rule_fired TEXT NOT NULL,
            PRIMARY KEY (dimension, segment_version))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS segments.bill_segments (bill_key INTEGER NOT NULL,
            account_key INTEGER NOT NULL, dimension TEXT NOT NULL, label TEXT NOT NULL, segment_version TEXT NOT NULL,
            PRIMARY KEY (bill_key, dimension, segment_version))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS segments.segment_build_log (build_id INTEGER NOT NULL,
            segment_version TEXT NOT NULL, built_at TEXT NOT NULL, bills INTEGER NOT NULL, rows INTEGER NOT NULL,
            PRIMARY KEY (build_id))""")
        for t in ("segment_definitions", "bill_segments"):
            conn.execute(f"DELETE FROM segments.{t} WHERE segment_version = ?", (VERSION,))
        su.insert(conn, "segments.segment_definitions", definitions)
        su.insert(conn, "segments.bill_segments", labels)
        build_id = (conn.execute("SELECT COALESCE(MAX(build_id), 0) FROM segments.segment_build_log").fetchone()[0] or 0) + 1
        su.insert(conn, "segments.segment_build_log", [{"build_id": build_id, "segment_version": VERSION,
                  "built_at": datetime.now(timezone.utc).isoformat(), "bills": bills, "rows": len(labels)}])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return definitions, labels


def hashes(conn):
    return {"segment_definitions": su.table_hash(conn, "segments.segment_definitions", "dimension, segment_version"),
            "bill_segments": su.table_hash(conn, "segments.bill_segments", "bill_key, dimension, segment_version")}


def dag_summary(conn):
    account = conn.execute("SELECT account_number FROM mart.dim_account ORDER BY account_number").fetchone()[0]
    definitions, labels = build(conn, account)
    counts = {}
    for l in labels:
        if l["dimension"] != "primary":
            counts.setdefault(l["dimension"], {}).setdefault(l["label"], 0)
            counts[l["dimension"]][l["label"]] += 1
    return {"cuts": {d["dimension"]: [d["cut_low"], d["cut_high"], d["rule_fired"]] for d in definitions if d["cut_low"]},
            "counts": counts, "hashes": {k: v[:16] for k, v in hashes(conn).items()}}


def main(argv=None):
    conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"))
    try:
        print(json.dumps(dag_summary(conn), default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
