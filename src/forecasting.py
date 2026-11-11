"""B21 forecasting: monthly usage and billed total for the three months after the account's last bill.

Reads only `mart` and `features`; writes only `forecast`. Forecasts are estimates: labelled so on every
row, never an input to billing or audits.

Methods
- seasonal_naive (pure Decimal): a target month's kWh/day is the same calendar month's value from the most
  recent earlier year with a bill; else that calendar month's mean over all stored years; else the account's
  overall mean kWh/day. The rule that fired is stored per target month.
- ols: least squares on kWh/day with a linear time trend (month ordinal from the calendar spine, so missing
  months stay missing) plus calendar-month indicators, numpy only. The fit runs in float; every stored value
  is converted back with Decimal(str(...)) and rounded to cents (kWh to 0.001).
Billed forecast = forecast kWh x the held rate: the latest bill's billed_per_kwh (feature store), stored on
every row. Selection: walk-forward one-step backtest over the last six bills; the lower billed-total MAE
ships, a tie ships the baseline. The band is +/- that MAE: a backtest band, not a confidence interval.
"""

import calendar
import json
import sys
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

try:
    from src import db, feature_pipeline as fp, store_util as su
except ImportError:
    import db
    import feature_pipeline as fp
    import store_util as su

ROOT = Path(__file__).resolve().parents[1]
BACKTEST_POINTS = 6
HORIZON = 3
BASELINE, OLS = "seasonal_naive", "ols"
KWH = Decimal("0.001")
TABLES = ("forecast_runs", "forecast_values")
LABEL = "estimate (forecast), not a billing or audit input"

SQL_BILLS = ("SELECT f.bill_key, a.account_key, a.account_number, dt.year, dt.month, dt.year_month, "
             "f.billing_days FROM mart.fact_bill f JOIN mart.dim_account a ON a.account_key = f.account_key "
             "JOIN mart.dim_date dt ON dt.date_key = f.bill_to_key WHERE a.account_number = ? "
             "ORDER BY dt.calendar_date, f.bill_key")


class ForecastError(RuntimeError):
    pass


def load(conn, account):
    bills = su.rows(conn, SQL_BILLS, (account,))
    feats = fp.frame(conn, account)
    for b in bills:
        b["kpd"] = feats.loc[b["bill_key"], "kwh_per_day"]
        b["rate"] = feats.loc[b["bill_key"], "billed_per_kwh"]
        b["kwh"] = feats.loc[b["bill_key"], "total_kwh"]
        b["billed"] = feats.loc[b["bill_key"], "billed_total"]
        b["ordinal"] = b["year"] * 12 + b["month"]
    return bills


def baseline_kpd(train, year, month):
    """(kWh/day, rule fired) from training bills only."""
    same = [b for b in train if b["month"] == month and b["kpd"] is not None]
    earlier = [b for b in same if b["year"] < year]
    if earlier:
        latest = max(b["year"] for b in earlier)
        vals = [b["kpd"] for b in earlier if b["year"] == latest]
        return sum(vals, Decimal(0)) / len(vals), f"prior_year {latest}"
    if same:
        return sum((b["kpd"] for b in same), Decimal(0)) / len(same), "calendar_month_mean"
    vals = [b["kpd"] for b in train if b["kpd"] is not None]
    return sum(vals, Decimal(0)) / len(vals), "overall_mean"


def ols_kpd(train, year, month):
    import numpy as np

    pts = [b for b in train if b["kpd"] is not None]

    def row(ordinal, m):
        return [1.0, float(ordinal)] + [1.0 if m == k else 0.0 for k in range(2, 13)]

    x = np.array([row(b["ordinal"], b["month"]) for b in pts])
    y = np.array([float(b["kpd"]) for b in pts])
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    pred = float(np.dot(np.array(row(year * 12 + month, month)), coef))
    return Decimal(str(round(pred, 10))), "ols"


METHODS = {BASELINE: baseline_kpd, OLS: ols_kpd}


def backtest(bills, points=BACKTEST_POINTS):
    out = []
    for target in bills[-points:]:
        train = [b for b in bills if (b["ordinal"], b["bill_key"]) < (target["ordinal"], target["bill_key"])]
        assert target not in train and all(b["bill_key"] != target["bill_key"] for b in train)
        held = train[-1]["rate"]
        row = {"bill_key": target["bill_key"], "year_month": target["year_month"],
               "actual_kpd": su.dec(target["kpd"]).quantize(KWH), "actual_billed": su.cents(target["billed"]),
               "train_bills": len(train)}
        for name, fn in METHODS.items():
            kpd, _rule = fn(train, target["year"], target["month"])
            row[f"{name}_kpd"] = kpd.quantize(KWH, rounding=ROUND_HALF_UP)
            row[f"{name}_billed"] = su.cents(kpd * Decimal(target["billing_days"]) * held)
        out.append(row)
    mae = {}
    for name in METHODS:
        mae[f"{name}_kpd"] = (sum((abs(r[f"{name}_kpd"] - r["actual_kpd"]) for r in out), Decimal(0))
                              / len(out)).quantize(KWH, rounding=ROUND_HALF_UP)
        mae[f"{name}_billed"] = su.cents(sum((abs(r[f"{name}_billed"] - r["actual_billed"]) for r in out),
                                             Decimal(0)) / len(out))
    return out, mae


def choose(mae):
    return OLS if mae[f"{OLS}_billed"] < mae[f"{BASELINE}_billed"] else BASELINE


def targets(bills, horizon=HORIZON):
    y, m = bills[-1]["year"], bills[-1]["month"]
    out = []
    for _ in range(horizon):
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((y, m))
    return out


def compute(conn, account):
    bills = load(conn, account)
    if len(bills) <= BACKTEST_POINTS:
        raise ForecastError(f"{len(bills)} bills; the backtest needs more than {BACKTEST_POINTS}.")
    bt, mae = backtest(bills)
    method = choose(mae)
    last = bills[-1]
    held = su.dec(last["rate"])
    band = mae[f"{method}_billed"]
    values = []
    for y, m in targets(bills):
        kpd, rule = METHODS[method](bills, y, m)
        days = calendar.monthrange(y, m)[1]
        kwh = (kpd * days).quantize(KWH, rounding=ROUND_HALF_UP)
        billed = su.cents(kwh * held)
        values.append({"account_key": last["account_key"], "target_year_month": f"{y:04d}-{m:02d}",
                       "days_in_month": days, "forecast_kwh_per_day": kpd.quantize(KWH, rounding=ROUND_HALF_UP),
                       "forecast_kwh": kwh, "forecast_billed": billed, "band_low": billed - band,
                       "band_high": billed + band, "band_label": f"backtest band +/- MAE {band} (not a confidence interval)",
                       "held_rate": held, "rule_fired": rule, "label": LABEL})
    run = {"feature_set_version": fp.FEATURE_SET_VERSION, "method_chosen": method,
           "mae_baseline_kpd": mae[f"{BASELINE}_kpd"], "mae_baseline_billed": mae[f"{BASELINE}_billed"],
           "mae_ols_kpd": mae[f"{OLS}_kpd"], "mae_ols_billed": mae[f"{OLS}_billed"], "held_rate": held,
           "held_rate_bill_key": last["bill_key"],
           "backtest_json": json.dumps(bt, default=str, sort_keys=True),
           "note": ("OLS fit in float inside the fit only; stored values via Decimal(str()) rounded. Held-rate "
                    "assumption: forecast billed = forecast kWh x latest bill's billed_per_kwh.")}
    return run, values, bt, mae


def build(conn, account, paths=None):
    su.attach(conn, {"mart": None, "features": None, "forecast": None, **(paths or {})})
    run, values, bt, mae = compute(conn, account)
    n = su.num_type(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        su.ensure_schema(conn, "forecast")
        for t in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS forecast.{t}")
        conn.execute(f"""CREATE TABLE forecast.forecast_runs (run_id INTEGER NOT NULL, built_at TEXT NOT NULL,
            feature_set_version TEXT NOT NULL, method_chosen TEXT NOT NULL, mae_baseline_kpd {n}, mae_baseline_billed {n},
            mae_ols_kpd {n}, mae_ols_billed {n}, held_rate {n} NOT NULL, held_rate_bill_key INTEGER NOT NULL,
            backtest_json TEXT NOT NULL, note TEXT NOT NULL, PRIMARY KEY (run_id))""")
        conn.execute(f"""CREATE TABLE forecast.forecast_values (run_id INTEGER NOT NULL, account_key INTEGER NOT NULL,
            target_year_month TEXT NOT NULL, days_in_month INTEGER NOT NULL, forecast_kwh_per_day {n},
            forecast_kwh {n} NOT NULL, forecast_billed {n} NOT NULL, band_low {n}, band_high {n}, band_label TEXT NOT NULL,
            held_rate {n} NOT NULL, rule_fired TEXT NOT NULL, label TEXT NOT NULL,
            PRIMARY KEY (run_id, account_key, target_year_month))""")
        su.insert(conn, "forecast.forecast_runs", [{"run_id": 1, "built_at": datetime.now(timezone.utc).isoformat(), **run}])
        su.insert(conn, "forecast.forecast_values", [{"run_id": 1, **v} for v in values])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return run, values, bt, mae


def hashes(conn):
    return {"forecast_runs": su.table_hash(conn, "forecast.forecast_runs", "run_id", exclude=("built_at",)),
            "forecast_values": su.table_hash(conn, "forecast.forecast_values", "run_id, target_year_month")}


def main(argv=None):
    conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"))
    try:
        account = conn.execute("SELECT account_number FROM mart.dim_account ORDER BY account_number").fetchone()[0]
        run, values, bt, mae = build(conn, account)
        print(json.dumps({"method": run["method_chosen"], "mae": mae, "values": values, "hashes": hashes(conn)},
                         default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())


def dag_summary(conn):
    """Airflow entry point: build for the only account in the marts and return a compact record."""
    account = conn.execute("SELECT account_number FROM mart.dim_account ORDER BY account_number").fetchone()[0]
    run, values, _bt, mae = build(conn, account)
    return {"method": run["method_chosen"], "mae": mae, "held_rate": run["held_rate"],
            "values": [(v["target_year_month"], v["forecast_kwh"], v["forecast_billed"], v["rule_fired"]) for v in values],
            "hashes": {k: v[:16] for k, v in hashes(conn).items()}}
