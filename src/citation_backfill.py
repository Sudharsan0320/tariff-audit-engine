"""Completion Fix 1: store page citations for uncited Appalachian rules, quote-verified (Task 4e pattern).

For each Appalachian rule with citation_status 'uncited', the cached extraction reading for its section is
searched for the same rule (rule type and rule text). A citation is stored only when that cached entry has a
source page and quote AND the quote appears on that page of data/raw/APCo-Tariff-28.pdf (whitespace
normalized; ai_extract.quote_on_page, the established verifier rule). Everything else stays uncited and is
listed with its reason: no_cache (no cached reading for the section), no_cached_rule (the reading lacks the
rule), no_page_or_quote, or quote_not_on_page. Zero model calls: cache and PDF only.

Idempotent: a second run stores nothing. Already-cited rules are checked too, and any disagreement between
a stored page and the verified cache page refuses the whole run, writing nothing. Only source_page and
citation_status of the target rows change.
"""

import json
import re
import sys
from pathlib import Path

try:
    from src import ai_extract, db, store_util as su
except ImportError:
    import ai_extract
    import db
    import store_util as su

ROOT = Path(__file__).resolve().parents[1]
UTILITY = "Appalachian Power Company"
BOOK = ROOT / "data" / "raw" / "APCo-Tariff-28.pdf"
CACHE = ROOT / "data" / "cache" / "ai-extract" / "claude-sonnet-5-5"


class CitationError(RuntimeError):
    """A stored citation disagrees with the verified cache; nothing was written."""


def _code(text):
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def cached_rules(cache_path):
    """{normalized schedule code: [(rule_type, rule_text, page, quote)]} from the reading cache, or {} if absent."""
    if not Path(cache_path).exists():
        return {}
    cache = ai_extract._load_cache(Path(cache_path))
    out = {}
    for key, entry in cache.get("sections", {}).items():
        try:
            payload = ai_extract.parse_model_json(entry.get("raw") or "")
        except Exception:
            continue
        for r in payload.get("rules") or []:
            if isinstance(r, dict):
                out.setdefault(_code(key.split("|")[0]), []).append(
                    (str(r.get("rule_type") or "").strip(), str(r.get("rule_text") or "").strip(),
                     r.get("source_page"), r.get("source_quote")))
    return out


def pdf_page_reader(path=BOOK):
    import pdfplumber

    cache = {}

    def read(page):
        if page not in cache:
            with pdfplumber.open(path) as pdf:
                cache[page] = pdf.pages[page - 1].extract_text() or "" if 1 <= page <= len(pdf.pages) else None
        return cache[page]
    return read


def plan(conn, cache_by_code, read_page):
    rows = su.rows(conn, "SELECT r.id, v.schedule_code, r.rule_type, r.rule_text, r.source_page, r.citation_status "
                         "FROM tariff_rules r JOIN tariff_versions v ON v.id = r.tariff_version_id WHERE v.utility = ? "
                         "ORDER BY r.id", (UTILITY,))
    store, listed, conflicts, agree = [], [], [], 0
    for r in rows:
        entries = cache_by_code.get(_code(r["schedule_code"]))
        verdict, page = None, None
        if entries is None:
            verdict = "no_cache"
        else:
            hits = [e for e in entries if e[0] == r["rule_type"] and e[1] == str(r["rule_text"]).strip()]
            if not hits:
                verdict = "no_cached_rule"
            else:
                for _t, _x, p, quote in hits:
                    if p is None or not quote:
                        verdict = verdict or "no_page_or_quote"
                        continue
                    text = read_page(int(p))
                    if text is not None and ai_extract.quote_on_page(quote, text):
                        verdict, page = "verified", int(p)
                        break
                    verdict = "quote_not_on_page"
        if r["citation_status"] == "cited":
            if verdict == "verified" and r["source_page"] not in (None, page):
                conflicts.append((r["id"], r["source_page"], page))
            else:
                agree += 1
            continue
        if verdict == "verified":
            store.append((r["id"], page, r["schedule_code"], r["rule_type"]))
        else:
            listed.append((r["id"], r["schedule_code"], r["rule_type"], verdict))
    return store, listed, conflicts, agree


def apply(conn, cache_path=CACHE / f"{ai_extract.content_hash(BOOK) if BOOK.exists() else 'none'}.json", read_page=None):
    read_page = read_page or pdf_page_reader()
    store, listed, conflicts, agree = plan(conn, cached_rules(cache_path), read_page)
    if conflicts:
        raise CitationError(f"stored citations disagree with the verified cache: {conflicts[:5]}; nothing written")
    try:
        for rule_id, page, _code_, _type in store:
            conn.execute("UPDATE tariff_rules SET source_page = ?, citation_status = 'cited' WHERE id = ? "
                         "AND citation_status = 'uncited'", (page, rule_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"stored": store, "listed": listed, "already_cited_checked": agree}


def main(argv=None):
    conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"))
    try:
        result = apply(conn)
    finally:
        conn.close()
    print(json.dumps({"stored": len(result["stored"]), "listed": result["listed"],
                      "already_cited_checked": result["already_cited_checked"]}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
