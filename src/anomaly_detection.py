"""B18 ML anomaly scores: an IsolationForest ranking of the account's bills for auditor attention.

Prioritization only, not an audit finding: scores never touch a charge, an expected total or a verdict.
Reads only `features` and `mart`; writes only `ml`. At n = 31 the scores order attention, nothing more;
a score is never a probability or a finding.

Features: the feature store's numeric v1 features plus kwh_per_day_gap (kwh_per_day - trailing_3_kwh_per_day),
less the features EXCLUDED by rule (Task B: bill_year, bill_month, bills_so_far, has_audit — position or coverage,
not behavior), which are removed before the coverage screen and recorded on every run. A feature is used only if at least MIN_COVERAGE of the bills have a value and it varies; remaining NULLs are
imputed with the account median (recorded in the run row). Model: IsolationForest(random_state=42), default
trees. Flags: the top K = max(1, round(10% of bills)). Cross-check: a robust z-score ranking (median/MAD) over
the same features; the top-K overlap is reported as information, not a gate. sklearn works in float inside
the fit; stored scores go through Decimal(str(round(x, 10))).

Runs are history: each build appends a new run_id (scores, run row and its coverage table); earlier runs are never
rewritten. Fewer than MIN_FEATURES qualifying features stops the build before anything is written.
"""

import json
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
MIN_COVERAGE = Decimal("0.8")
FLAG_SHARE = 0.10
SEED = 42
MAD_SCALE = 1.4826
LABEL = "prioritization only, not an audit finding"
DERIVED = "kwh_per_day_gap"
EXCLUDED = ("bill_year", "bill_month", "bills_so_far", "has_audit")
EXCLUSION_REASON = "position or coverage, not behavior"
MIN_FEATURES = 6


class AnomalyStop(RuntimeError):
    """Too few features qualify to score; nothing was written. .coverage holds the table."""

    def __init__(self, message, coverage):
        super().__init__(message)
        self.coverage = coverage


def matrix(conn, account):
    f = fp.frame(conn, account)
    dtypes = dict(conn.execute("SELECT feature_name, data_type FROM features.feature_definitions "
                               "WHERE feature_set_version = ?", (fp.FEATURE_SET_VERSION,)).fetchall())
    numeric = [c for c in f.columns if dtypes.get(c) in ("decimal", "integer")]
    data = {c: list(f[c]) for c in numeric}
    data[DERIVED] = [None if a is None or b is None else a - b
                     for a, b in zip(f["kwh_per_day"], f["trailing_3_kwh_per_day"])]
    return [int(k) for k in f.index], data


def screen(data, n):
    coverage, used, screened = [], [], []
    for name, values in data.items():
        present = [v for v in values if v is not None]
        if name in EXCLUDED:                      # removed by rule before the coverage screen
            coverage.append({"feature": name, "non_null": len(present),
                             "coverage": (Decimal(len(present)) / n).quantize(Decimal("0.001")),
                             "varies": len(set(present)) > 1, "qualified": False,
                             "excluded": f"excluded by rule: {EXCLUSION_REASON}"})
            continue
        share = Decimal(len(present)) / n
        varies = len(set(present)) > 1
        ok = share >= MIN_COVERAGE and varies
        coverage.append({"feature": name, "non_null": len(present), "coverage": share.quantize(Decimal("0.001")),
                         "varies": varies, "qualified": ok, "excluded": None})
        (used if ok else screened).append(name)
    return coverage, used, screened


def compute(conn, account, min_features=MIN_FEATURES):
    import numpy as np
    from sklearn.ensemble import IsolationForest

    bill_keys, data = matrix(conn, account)
    n = len(bill_keys)
    coverage, used, screened = screen(data, n)
    if len(used) < min_features:
        raise AnomalyStop(f"only {len(used)} features qualify after the exclusions (minimum {min_features}); not scored",
                          coverage)
    imputed = {}
    columns = []
    for name in used:
        present = sorted(float(v) for v in data[name] if v is not None)
        median = float(np.median(present))
        missing = sum(1 for v in data[name] if v is None)
        if missing:
            imputed[name] = {"imputed_rows": missing, "median": median}
        columns.append([median if v is None else float(v) for v in data[name]])
    x = np.array(columns).T
    model = IsolationForest(random_state=SEED)
    model.fit(x)
    raw = -model.score_samples(x)                                  # higher = more unusual
    med = np.median(x, axis=0)
    mad = np.median(np.abs(x - med), axis=0) * MAD_SCALE
    z = np.where(mad > 0, (x - med) / np.where(mad > 0, mad, 1), 0.0)
    robust = np.max(np.abs(z), axis=1)
    k = max(1, round(FLAG_SHARE * n))
    order = sorted(range(n), key=lambda i: (-raw[i], bill_keys[i]))
    robust_order = sorted(range(n), key=lambda i: (-robust[i], bill_keys[i]))
    rank = {i: r + 1 for r, i in enumerate(order)}
    robust_rank = {i: r + 1 for r, i in enumerate(robust_order)}
    scores = []
    for i, bill_key in enumerate(bill_keys):
        flagged = rank[i] <= k
        top = sorted(range(len(used)), key=lambda j: (-abs(z[i, j]), used[j]))[:3]
        why = "; ".join(f"{used[j]} = {x[i, j]:.4f} (robust z {z[i, j]:+.2f})" for j in top)
        explanation = (f"{LABEL}. " + (f"Ranked in the top {k}: " if flagged else "Not in the top set; ") + why)
        scores.append({"bill_key": bill_key, "score": Decimal(str(round(float(raw[i]), 10))), "rank": rank[i],
                       "flagged": 1 if flagged else 0, "robust_score": Decimal(str(round(float(robust[i]), 10))),
                       "robust_rank": robust_rank[i], "explanation": explanation})
    top_ml = {bill_keys[i] for i in order[:k]}
    top_robust = {bill_keys[i] for i in robust_order[:k]}
    run = {"feature_set_version": fp.FEATURE_SET_VERSION, "model": "sklearn.ensemble.IsolationForest",
           "parameters": json.dumps({"random_state": SEED, "n_estimators": model.n_estimators}),
           "bills_scored": n, "flag_k": k, "features_used": ",".join(used), "features_screened": ",".join(screened),
           "features_excluded": json.dumps([{"feature": f, "rule": "excluded by rule", "reason": EXCLUSION_REASON}
                                            for f in EXCLUDED if f in data]),
           "imputation_note": json.dumps(imputed, sort_keys=True) if imputed else "none",
           "topk_overlap": f"{len(top_ml & top_robust)} of {k} ({sorted(top_ml)} vs robust {sorted(top_robust)})",
           "label": LABEL}
    return run, scores, coverage


RUNS_DDL = """CREATE TABLE IF NOT EXISTS ml.ml_anomaly_runs (run_id INTEGER NOT NULL, built_at TEXT NOT NULL,
    feature_set_version TEXT NOT NULL, model TEXT NOT NULL, parameters TEXT NOT NULL, bills_scored INTEGER NOT NULL,
    flag_k INTEGER NOT NULL, features_used TEXT NOT NULL, features_screened TEXT NOT NULL,
    imputation_note TEXT NOT NULL, topk_overlap TEXT NOT NULL, label TEXT NOT NULL, PRIMARY KEY (run_id))"""


def _columns(conn, table):
    cursor = conn.execute(f"SELECT * FROM ml.{table} LIMIT 0")
    return {c[0] for c in cursor.description}


def build(conn, account, paths=None, min_features=MIN_FEATURES):
    """Append one new run (never rewriting earlier runs); returns (run, scores, coverage) with run["run_id"]."""
    su.attach(conn, {"mart": None, "features": None, "ml": None, **(paths or {})})
    run, scores, coverage = compute(conn, account, min_features)
    n = su.num_type(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        su.ensure_schema(conn, "ml")
        conn.execute(RUNS_DDL)
        if "features_excluded" not in _columns(conn, "ml_anomaly_runs"):
            conn.execute("ALTER TABLE ml.ml_anomaly_runs ADD COLUMN features_excluded TEXT")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS ml.ml_anomaly_scores (run_id INTEGER NOT NULL, bill_key INTEGER NOT NULL,
            score {n} NOT NULL, rank INTEGER NOT NULL, flagged INTEGER NOT NULL, robust_score {n} NOT NULL,
            robust_rank INTEGER NOT NULL, explanation TEXT NOT NULL, PRIMARY KEY (run_id, bill_key))""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS ml.ml_anomaly_coverage (run_id INTEGER NOT NULL, feature TEXT NOT NULL,
            non_null INTEGER NOT NULL, coverage {n} NOT NULL, varies INTEGER NOT NULL, qualified INTEGER NOT NULL,
            excluded TEXT, PRIMARY KEY (run_id, feature))""")
        run_id = (conn.execute("SELECT MAX(run_id) FROM ml.ml_anomaly_runs").fetchone()[0] or 0) + 1
        su.insert(conn, "ml.ml_anomaly_runs", [{"run_id": run_id, "built_at": datetime.now(timezone.utc).isoformat(), **run}])
        su.insert(conn, "ml.ml_anomaly_scores", [{"run_id": run_id, **s} for s in scores])
        su.insert(conn, "ml.ml_anomaly_coverage", [{"run_id": run_id, **{**c, "varies": int(c["varies"]),
                                                                          "qualified": int(c["qualified"])}} for c in coverage])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {**run, "run_id": run_id}, scores, coverage


def hashes(conn, run_id=None):
    """Content hash of one run (default: the latest), run_id and built_at left out, so two runs can be compared."""
    import hashlib
    run_id = run_id or conn.execute("SELECT MAX(run_id) FROM ml.ml_anomaly_runs").fetchone()[0]
    out = {}
    for table, key in (("ml_anomaly_runs", "run_id"), ("ml_anomaly_scores", "bill_key"), ("ml_anomaly_coverage", "feature")):
        rows = su.rows(conn, f"SELECT * FROM ml.{table} WHERE run_id = ? ORDER BY {key}", (run_id,))
        body = [{k: str(v) for k, v in r.items() if k not in ("run_id", "built_at")} for r in rows]
        out[table] = hashlib.md5(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return out


def dag_summary(conn):
    account = conn.execute("SELECT account_number FROM mart.dim_account ORDER BY account_number").fetchone()[0]
    run, scores, _coverage = build(conn, account)
    flagged = sorted((s for s in scores if s["flagged"]), key=lambda s: s["rank"])
    return {"label": LABEL, "run_id": run["run_id"], "features_used": run["features_used"],
            "features_excluded": [f["feature"] for f in json.loads(run["features_excluded"])], "overlap": run["topk_overlap"],
            "flagged": [(s["rank"], s["bill_key"], s["score"]) for s in flagged],
            "hashes": {k: v[:16] for k, v in hashes(conn).items()}}


def main(argv=None):
    conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"))
    try:
        print(json.dumps(dag_summary(conn), default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
