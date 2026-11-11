"""B25 extraction evaluation harness and run ledger (schema `eval`).

Ledger: `eval.extraction_runs`, one row per extraction run. Backfill rows come only from the cache artifacts
(data/cache/ai-extract*/) and the figures recorded in docs/tariff-extraction-design.md; a figure that cannot be
sourced is NULL and the note says so. Approximate figures are never hardened into precise values.

Harness: replay one book's cached readings through the CURRENT pipeline (packet_pipeline.run_book: rates,
rules and structured verification) into a fresh empty-schema scratch database, then compare per section with
a comparison database holding the stored result (a clone of the primary; never the primary itself). Zero model
calls is enforced: ANTHROPIC_API_KEY is removed from the environment, ai_extract.api_key is patched to None
(it also reads .env), and the client passed to run_book raises if it is ever called. Each difference is tagged
`expected: <change>` when it matches KNOWN_CHANGES, else `unexplained` and reported in full. The harness never
edits the comparison database's tariff tables.
"""

import json
import os
import subprocess
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse, urlunparse

try:
    from src import ai_extract, db, store_util as su
except ImportError:
    import ai_extract
    import db
    import store_util as su

ROOT = Path(__file__).resolve().parents[1]
SCRATCH_PREFIX = "tariff_b25_"
BOOKS = {
    "Appalachian Power Company": ("APCo-Tariff-28.pdf", "Virginia S.C.C. Tariff No. 28"),
    "Pepco": ("Pepco-DC-Tariff-2026-10-01.pdf", "P.S.C. of D.C. No. 1"),
}
SKIPS = {"Virginia Electric and Power Company": (
    "Amendment No. 12", "Dominion's live set came from the hand-written parsers and its AI re-read is held; "
    "there is no AI cache for the live set, so there is nothing to replay (named skip, not an error).")}

KNOWN_CHANGES = [
    {"name": "12h-fix2 month-range acceptance", "date": "2026-11-10",
     "match": lambda d: d["kind"] == "structured" and "demand_determination" in d["detail"]},
    {"name": "12h-fix3 SBS evaluator field-name enforcement", "date": "2026-11-10",
     "match": lambda d: d["kind"] == "structured" and d["schedule"] == "SBS"},
    {"name": "12f readable() word-content rule", "date": "2026-11-10",
     "match": lambda d: d["kind"] == "queue" and "unreadable_pages" in d["detail"]},
    {"name": "12e/12f chunker changes", "date": "2026-11-10",
     "match": lambda d: d["kind"] == "section"},
]


class NeverCalledClient:
    """Stands in for the model client: any use is a bug, so it raises."""

    def __getattr__(self, name):
        raise AssertionError(f"model client used ({name}); the harness must make zero model calls")

    def __call__(self, *args, **kwargs):
        raise AssertionError("model client called; the harness must make zero model calls")


@contextmanager
def no_model_key():
    saved_env = os.environ.pop("ANTHROPIC_API_KEY", None)
    saved_fn = ai_extract.api_key
    ai_extract.api_key = lambda: None
    try:
        yield
    finally:
        ai_extract.api_key = saved_fn
        if saved_env is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved_env


# ---------------------------------------------------------------- scratch database
def _with_db(url, name):
    parts = urlparse(url)
    return urlunparse(parts._replace(path="/" + name))


def create_scratch(base_url, name):
    if not name.startswith(SCRATCH_PREFIX):
        raise ValueError(f"refusing to create {name}: harness scratch names start with {SCRATCH_PREFIX}")
    import psycopg

    with psycopg.connect(_with_db(base_url, "postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 LOCALE 'C' ENCODING 'UTF8'")
    url = _with_db(base_url, name)
    env = {k: v for k, v in os.environ.items() if k not in ("TARIFF_DB_PATH", "ANTHROPIC_API_KEY")}
    env.update(TARIFF_DB_URL=url, PYTHONPATH=str(ROOT))
    subprocess.run([sys.executable, str(ROOT / "src" / "create_db.py")], cwd=ROOT, env=env, check=True,
                   capture_output=True)
    return url


# ---------------------------------------------------------------- stored result of one book
def stored(conn, utility):
    def q(sql):
        return su.rows(conn, sql, (utility,))

    versions = q("SELECT schedule_code, effective_from, status, schedule_kind FROM tariff_versions WHERE utility = ?")
    comps = q("SELECT v.schedule_code, v.effective_from, c.component_type, c.billing_category, c.unit, c.variant, "
              "c.rate, c.source_page FROM tariff_components c JOIN tariff_versions v ON v.id = c.tariff_version_id "
              "WHERE v.utility = ?")
    rules = q("SELECT v.schedule_code, v.effective_from, r.rule_type, r.rule_text, r.citation_status FROM tariff_rules r "
              "JOIN tariff_versions v ON v.id = r.tariff_version_id WHERE v.utility = ?")
    structured = q("SELECT v.schedule_code, v.effective_from, s.rule_type, s.parameters_json, s.source_page "
                   "FROM structured_tariff_rules s JOIN tariff_versions v ON v.id = s.tariff_version_id WHERE v.utility = ?")
    queue = su.rows(conn, "SELECT schedule_code, signal FROM parse_review_items WHERE source_kind = 'tariff_book_section' "
                          "AND detail LIKE ?", (utility + " %",))

    def key(row, fields):
        return tuple(str(row[f]) for f in fields)

    return {
        "version": Counter(key(r, ("schedule_code", "effective_from", "status", "schedule_kind")) for r in versions),
        "component": Counter(key(r, ("schedule_code", "effective_from", "component_type", "billing_category", "unit",
                                     "variant", "rate", "source_page")) for r in comps),
        "rule": Counter(key(r, ("schedule_code", "effective_from", "rule_type", "rule_text")) for r in rules),
        "rule_citation": Counter(key(r, ("schedule_code", "rule_type", "citation_status")) for r in rules),
        "structured": Counter(key(r, ("schedule_code", "effective_from", "rule_type", "parameters_json", "source_page"))
                              for r in structured),
        "queue": Counter(key(r, ("schedule_code", "signal")) for r in queue),
    }


def tag(diff):
    for change in KNOWN_CHANGES:
        if change["match"](diff):
            return f"expected: {change['name']} ({change['date']})"
    return "unexplained"


def compare(primary, replay):
    kinds = {}
    diffs = []
    sections_p = {k[0] for k in primary["version"]} | {k[0] for k in primary["queue"]}
    sections_r = {k[0] for k in replay["version"]} | {k[0] for k in replay["queue"]}
    for kind in ("version", "component", "rule", "rule_citation", "structured", "queue"):
        p, r = primary[kind], replay[kind]
        both = sum((p & r).values())
        only_r, only_p = r - p, p - r
        kinds[kind] = {"both": both, "only_replay": sum(only_r.values()), "only_primary": sum(only_p.values())}
        for side, counter in (("only_replay", only_r), ("only_primary", only_p)):
            for k, n in sorted(counter.items()):
                d = {"kind": kind, "side": side, "schedule": k[0], "detail": " | ".join(k[1:])[:240], "count": n}
                d["tag"] = tag(d)
                diffs.append(d)
    for code in sorted(sections_p ^ sections_r):
        d = {"kind": "section", "side": "only_replay" if code in sections_r else "only_primary", "schedule": code,
             "detail": "section present on one side only", "count": 1}
        d["tag"] = tag(d)
        diffs.append(d)
    disagreeing = {d["schedule"] for d in diffs}
    sections = sorted(sections_p | sections_r)
    return {"sections": len(sections), "sections_agreeing": sum(1 for s in sections if s not in disagreeing),
            "by_kind": kinds, "differences": diffs,
            "unexplained": [d for d in diffs if d["tag"] == "unexplained"]}


def evaluate(compare_conn, base_url, utility, scratch_name=None):
    """Replay `utility`'s book into a fresh scratch and compare with compare_conn's stored result."""
    from src import packet_pipeline as pp

    if utility in SKIPS:
        return {"utility": utility, "skipped": SKIPS[utility][1]}
    book, label = BOOKS[utility]
    name = scratch_name or SCRATCH_PREFIX + utility.split()[0].lower() + "_replay"
    url = create_scratch(base_url, name)
    with no_model_key():
        result = pp.run_book(str(ROOT / "data" / "raw" / book), url, client=NeverCalledClient())
    with db.connect(url, readonly=True) as replay_conn:
        replay = stored(replay_conn, utility)
        totals = {t: replay_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("tariff_versions", "tariff_components", "tariff_rules", "structured_tariff_rules")}
        queue = Counter(r[0] for r in replay_conn.execute(
            "SELECT signal FROM parse_review_items WHERE source_kind = 'tariff_book_section'").fetchall())
    primary = stored(compare_conn, utility)
    diff = compare(primary, replay)
    return {"utility": utility, "book_label": label, "scratch": name, "model_calls": result["model_calls"],
            "structured_calls": result["structured_calls"], "packets": len(result["table"]),
            "replay_totals": totals, "replay_queue": dict(sorted(queue.items())), **diff}


# ---------------------------------------------------------------- ledger
DOC_ROWS = [
    {"utility": "Appalachian Power Company", "book_label": "Virginia S.C.C. Tariff No. 28", "source": "backfill_historical",
     "model": "claude-fable-5-1", "call_count": 9, "cost_low": None, "cost_high": None, "sections_read": 9,
     "components_stored": 0, "rules_stored": 0, "structured_rules_stored": None, "queue_by_reason": None,
     "cache": "ai-extract/8b2240fc…json",
     "source_note": "Design doc 11B witness: nine sections completed and cached; the tenth call was rejected HTTP 400 "
                    "(credit balance); nothing written. Cost: no figure recorded (NULL). Call count and sections "
                    "cross-checked against the cache file (9 entries, model claude-fable-5-1)."},
    {"utility": "Appalachian Power Company", "book_label": "Virginia S.C.C. Tariff No. 28", "source": "backfill_historical",
     "model": "claude-sonnet-5-5", "call_count": 46, "cost_low": None, "cost_high": None, "sections_read": 46,
     "components_stored": 573, "rules_stored": 153, "structured_rules_stored": None,
     "queue_by_reason": {"no_verified_rates": 1, "rate_not_on_page": 1, "unreadable_pages": 2,
                         "unit_not_in_vocabulary": 2, "model_output_incomplete": 1},
     "cache": "ai-extract/claude-sonnet-5-5/8b2240fc…json",
     "source_note": "Design doc 11C Sonnet run (46 model calls, 10 then 36) and Task 2g/2h (573 components, 153 rules, "
                    "7 section queue items). Cost: the doc records only an approximate '≈$1.64' (12e), so it is not "
                    "stored as a precise value (NULL). Cache file: 46 entries, 45 end_turn and 1 max_tokens."},
    {"utility": "Appalachian Power Company", "book_label": "Virginia S.C.C. Tariff No. 28", "source": "backfill_historical",
     "model": "claude-sonnet-5-5", "call_count": 44, "cost_low": Decimal("0.90"), "cost_high": Decimal("1.57"),
     "sections_read": 44, "components_stored": None, "rules_stored": None, "structured_rules_stored": 44,
     "queue_by_reason": {"structured_parameter_not_printed": 1},
     "cache": "ai-extract-structured/claude-sonnet-5-5/8b2240fc…json",
     "source_note": "Design doc 12h structured reading: 41 live calls plus 3 from a stopped attempt = 44 sections; "
                    "45 proposed, 44 stored, 1 queued. Cost: the doc gives two estimates, about $0.90 and about $1.57, "
                    "stored as that range. Three later single SBS re-reads (12h-fix, fix2, fix3) are not included."},
    {"utility": "Pepco", "book_label": "P.S.C. of D.C. No. 1", "source": "backfill_historical",
     "model": "claude-sonnet-5-5", "call_count": 87, "cost_low": Decimal("2.49"), "cost_high": Decimal("3.10"),
     "sections_read": 44, "components_stored": 319, "rules_stored": 215, "structured_rules_stored": 6,
     "queue_by_reason": {"rate_year_missing": 48, "rate_not_on_page": 7, "quote_not_found": 6, "no_verified_rates": 4,
                         "unit_not_in_vocabulary": 2, "model_output_incomplete": 1},
     "cache": "ai-extract/claude-sonnet-5-5/81d817fe…json + ai-extract-structured/…/81d817fe…json",
     "source_note": "Design doc 12i Pepco run: 45 reading calls (one replaced SOS attempt) plus 42 structured calls; "
                    "62 versions, 319 components, 215 rules, 6 structured rows, 68 queue items. Cost: the doc gives about "
                    "$2.49 (token pricing) and about $3.10 (call scaling), stored as that range."},
]


def cache_checks():
    """Cross-check the backfill against the cache artifacts with ai_extract's own reader."""
    out = []
    for path in sorted((ROOT / "data" / "cache").glob("ai-extract*/**/*.json")):
        cache = ai_extract._load_cache(path)
        sections = cache.get("sections", {})
        models = sorted({s.get("model") for s in sections.values()})
        stops = Counter(s.get("stop_reason") for s in sections.values())
        unreadable = 0
        for entry in sections.values():
            try:
                ai_extract.parse_model_json(entry.get("raw") or "")
            except Exception:
                unreadable += 1
        out.append({"cache": str(path.relative_to(ROOT / "data" / "cache")), "sections": len(sections), "models": models,
                    "stop_reasons": dict(stops), "unreadable_cache": unreadable,
                    "input_tokens": sum((s.get("usage") or {}).get("input_tokens", 0) for s in sections.values()),
                    "output_tokens": sum((s.get("usage") or {}).get("output_tokens", 0) for s in sections.values())})
    return out


def replay_row(result):
    if "skipped" in result:
        label = SKIPS[result["utility"]][0]
        return {"utility": result["utility"], "book_label": label, "source": "cache_replay", "model": None,
                "call_count": 0, "cost_low": None, "cost_high": None, "sections_read": None, "components_stored": None,
                "rules_stored": None, "structured_rules_stored": None, "queue_by_reason": None,
                "source_note": "skip: " + result["skipped"]}
    t = result["replay_totals"]
    return {"utility": result["utility"], "book_label": result["book_label"], "source": "cache_replay",
            "model": "cache (claude-sonnet-5-5 readings)", "call_count": result["model_calls"] + result["structured_calls"],
            "cost_low": Decimal("0"), "cost_high": Decimal("0"), "sections_read": result["packets"],
            "components_stored": t["tariff_components"], "rules_stored": t["tariff_rules"],
            "structured_rules_stored": t["structured_tariff_rules"], "queue_by_reason": result["replay_queue"],
            "source_note": (f"B25 harness replay into scratch {result['scratch']} with the key removed and a never-called "
                            f"client; {result['sections_agreeing']} of {result['sections']} sections agree with the stored "
                            f"result; {len(result['unexplained'])} unexplained differences.")}


def write_ledger(conn, rows):
    n = su.num_type(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        su.ensure_schema(conn, "eval")
        conn.execute("DROP TABLE IF EXISTS eval.extraction_runs")
        conn.execute(f"""CREATE TABLE eval.extraction_runs (run_id INTEGER NOT NULL, recorded_at TEXT NOT NULL,
            utility TEXT NOT NULL, book_label TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN
            ('cache_replay', 'backfill_historical', 'live')), model TEXT, call_count INTEGER, cost_low {n}, cost_high {n},
            sections_read INTEGER, components_stored INTEGER, rules_stored INTEGER, structured_rules_stored INTEGER,
            queue_by_reason TEXT, source_note TEXT NOT NULL, PRIMARY KEY (run_id))""")
        now = datetime.now(timezone.utc).isoformat()
        su.insert(conn, "eval.extraction_runs", [
            {"run_id": i, "recorded_at": now, **{k: v for k, v in r.items() if k != "cache"},
             "queue_by_reason": json.dumps(r["queue_by_reason"], sort_keys=True) if r.get("queue_by_reason") else None}
            for i, r in enumerate(rows, start=1)])
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def dag_summary(conn):
    """Airflow entry point: replay Pepco against the guarded target (a primary clone) and record the ledger there."""
    base = os.environ["TARIFF_DB_URL"]          # already validated by the DAG's target guard
    results = [evaluate(conn, base, "Pepco"), evaluate(conn, base, "Virginia Electric and Power Company")]
    write_ledger(conn, DOC_ROWS + [replay_row(r) for r in results])
    r = results[0]
    return {"pepco": {k: r[k] for k in ("model_calls", "structured_calls", "sections", "sections_agreeing", "by_kind")},
            "unexplained": len(r["unexplained"]), "dominion": results[1]["skipped"][:60]}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Replay cached books and compare with a stored result.")
    parser.add_argument("--compare-db", required=True, help="database holding the stored result (a primary clone)")
    parser.add_argument("--utility", action="append", default=None)
    args = parser.parse_args(argv)
    base = os.environ["TARIFF_DB_URL"]
    with db.connect(_with_db(base, args.compare_db), readonly=True) as conn:
        for utility in args.utility or list(BOOKS) + list(SKIPS):
            print(json.dumps(evaluate(conn, base, utility), default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
