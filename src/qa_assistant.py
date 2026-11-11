"""B26 tariff Q&A: answers from the verified store only, every claim cited, plain refusal otherwise.

Deterministic: the question type is matched by pattern, entities by stored utility names and schedule codes,
and answers are assembled by template from the retrieved rows only (no model, no embeddings, no vector store;
a template never adds a number, date or name the rows did not return). An ambiguous entity is asked back by
listing the candidates. Unrecognized phrasings are refused with the list of question kinds supported.
A fluent-phrasing model layer is a future step (needs the key and a spend approval).
"""

import json
import re
import sys
from pathlib import Path

try:
    from src import db, store_util as su
except ImportError:
    import db
    import store_util as su

ROOT = Path(__file__).resolve().parents[1]
KINDS = ["rate lookup: 'What is the <charge> for <utility> <schedule> [in <year>]?'",
         "rule lookup: 'What are the rules for <utility> schedule <code>?'",
         "applicability: 'Which schedules does rider <code> apply to?'",
         "schedule inventory: 'What schedules does <utility> have?'",
         "schedule owner: 'Which company has a <code> schedule?'",
         "version lookup: 'What versions does <utility> schedule <code> have?'",
         "bill facts: 'What is bill <n>'s expected total and verdict?'"]
NO_CITATION = "this rule is stored without a page citation (citation_status uncited); no page can be given"


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


class Store:
    def __init__(self, conn):
        self.conn = conn
        self.utilities = [r[0] for r in conn.execute("SELECT DISTINCT utility FROM tariff_versions ORDER BY 1")]
        self.codes = {}
        for code, utility in conn.execute("SELECT DISTINCT schedule_code, utility FROM tariff_versions"):
            self.codes.setdefault(code, set()).add(utility)

    def rows(self, sql, params=()):
        return su.rows(self.conn, sql, params)

    def utility_in(self, q):
        """Stored utility names whose distinctive first word appears in the question."""
        words = set(re.findall(r"[a-z]+", q.lower()))
        return [u for u in self.utilities if re.findall(r"[a-z]+", u.lower())[0] in words]

    def codes_in(self, q):
        """Stored schedule codes named in the question: 'schedule X' / 'rider X', else any token of 2+ chars."""
        found = set()
        for m in re.finditer(r"\b(?:schedule|rider)\s+([A-Za-z0-9][A-Za-z0-9 .&/-]*?)(?=[\s?,.']*(?:$|\s(?:have|has|apply|in|for|of|schedule)\b|\?))", q, re.I):
            target = _norm(m.group(1))
            found |= {c for c in self.codes if _norm(c) in (target, "rider" + target)}
        if not found:
            words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.-]*", q)
            tokens = {_norm(t) for t in words if len(_norm(t)) >= 2} | {_norm(a + b) for a, b in zip(words, words[1:])}
            found = {c for c in self.codes if len(_norm(c)) >= 2 and _norm(c) in tokens}
        return sorted(found, key=lambda c: (-len(c), c))


def _answer(qtype, text, citations, refused=False):
    return {"question_type": qtype, "answer": text, "citations": citations, "refused": refused}


def _refuse(qtype, looked_for, nearby):
    return _answer(qtype, f"Not in the verified store: {looked_for}. {nearby}".strip(), [], refused=True)


def _resolve(store, q, qtype):
    """(utility, code) or an ask-back / refusal answer."""
    utilities = store.utility_in(q)
    codes = store.codes_in(q)
    if not codes:
        held = (f"Schedules held for {utilities[0]}: " + ", ".join(sorted(c for c, us in store.codes.items()
                                                                           if utilities[0] in us)) + "."
                if len(utilities) == 1 else "Stored utilities: " + "; ".join(store.utilities) + ".")
        named = re.search(r"\b(?:schedule|rider)\s+([A-Za-z0-9][A-Za-z0-9.&/-]*)", q, re.I)
        looked = (f"schedule {named.group(1).rstrip('?.,')}" + (f" for {utilities[0]}" if len(utilities) == 1 else "")
                  + " (no stored version)") if named else "no stored schedule code was named"
        return None, _refuse(qtype, looked, held)
    code = codes[0]
    owners = sorted(store.codes[code] & set(utilities)) if utilities else sorted(store.codes[code])
    if not owners and utilities:
        held = ", ".join(sorted(c for c, us in store.codes.items() if utilities[0] in us))
        return None, _refuse(qtype, f"schedule {code} for {utilities[0]}", f"Schedules held for {utilities[0]}: {held}.")
    if len(owners) > 1:
        return None, _answer(qtype, f"Schedule {code} exists for more than one utility: {'; '.join(owners)}. "
                                    "Name the utility to choose.", [], refused=True)
    return (owners[0], code), None


def rate_lookup(store, q):
    entity, stop = _resolve(store, q, "rate lookup")
    if stop:
        return stop
    utility, code = entity
    year = re.search(r"\b(20\d\d)\b", q)
    m = re.search(r"(?:what is|what's|give me)\s+the\s+(.+?)\s+(?:for|of|on)\b", q, re.I)
    charge = (m.group(1) if m else "").strip()
    versions = store.rows("SELECT id, version, effective_from, effective_to, status FROM tariff_versions WHERE utility = ? "
                          "AND schedule_code = ? ORDER BY effective_from", (utility, code))
    if year:
        versions = [v for v in versions if v["effective_from"][:4] <= year.group(1) and
                    (v["effective_to"] is None or v["effective_to"][:4] >= year.group(1))]
    else:
        versions = [v for v in versions if v["status"] == "active"]
    if not versions:
        return _refuse("rate lookup", f"a {'version in ' + year.group(1) if year else 'active version'} of {utility} {code}", "")
    words = [w for w in re.findall(r"[a-z]+", charge.lower()) if w not in ("the", "a", "an", "rate", "charge")] if charge else []
    out, cites = [], []
    for v in versions:
        comps = store.rows("SELECT id, component_type, billing_category, variant, rate, unit, source_page FROM tariff_components "
                           "WHERE tariff_version_id = ? ORDER BY id", (v["id"],))
        hits = [c for c in comps if all(w in f"{c['component_type']} {c['billing_category'] or ''}".lower() for w in words)]
        for c in hits:
            out.append(f"{c['billing_category'] or c['component_type']}: {c['rate']} {c['unit']} (version {v['version']}, "
                       f"effective {v['effective_from']}, page {c['source_page']})")
            cites.append({"table": "tariff_components", "id": c["id"], "source_page": c["source_page"]})
    if not out:
        labels = sorted({c["billing_category"] or c["component_type"] for v in versions for c in store.rows(
            "SELECT component_type, billing_category FROM tariff_components WHERE tariff_version_id = ?", (v["id"],))})
        return _refuse("rate lookup", f"a component matching '{charge}' in {utility} {code}",
                       "Components it does hold: " + "; ".join(labels[:12]) + ".")
    return _answer("rate lookup", f"{utility} schedule {code}: " + "; ".join(out) + ".", cites)


def rule_lookup(store, q):
    entity, stop = _resolve(store, q, "rule lookup")
    if stop:
        return stop
    utility, code = entity
    rules = store.rows("SELECT r.id, r.rule_type, r.rule_text, r.source_page, r.citation_status, v.effective_from "
                       "FROM tariff_rules r JOIN tariff_versions v ON v.id = r.tariff_version_id WHERE v.utility = ? "
                       "AND v.schedule_code = ? AND v.status = 'active' ORDER BY r.id", (utility, code))
    if not rules:
        return _refuse("rule lookup", f"stored rules for {utility} {code}", "")
    parts, cites = [], []
    for r in rules:
        where = f"page {r['source_page']}" if r["source_page"] else NO_CITATION
        parts.append(f"[{r['rule_type']}] {r['rule_text']} ({where})")
        cites.append({"table": "tariff_rules", "id": r["id"], "source_page": r["source_page"],
                      "note": None if r["source_page"] else NO_CITATION})
    return _answer("rule lookup", f"{utility} schedule {code} rules: " + " ".join(parts), cites)


def applicability(store, q):
    entity, stop = _resolve(store, q, "applicability")
    if stop:
        return stop
    utility, code = entity
    structured = store.rows("SELECT s.id, s.applies_to_schedule, s.source_page FROM structured_tariff_rules s JOIN tariff_versions v "
                            "ON v.id = s.tariff_version_id WHERE v.utility = ? AND v.schedule_code = ? AND s.rule_type = "
                            "'rider_applicability'", (utility, code))
    if structured:
        return _answer("applicability", f"{code} applies to: " + ", ".join(s["applies_to_schedule"] for s in structured) + ".",
                       [{"table": "structured_tariff_rules", "id": s["id"], "source_page": s["source_page"]} for s in structured])
    comps = store.rows("SELECT c.billing_category, MIN(c.id) AS id, MIN(c.source_page) AS source_page FROM tariff_components c "
                       "JOIN tariff_versions v ON v.id = c.tariff_version_id WHERE v.utility = ? AND v.schedule_code = ? "
                       "AND v.status = 'active' AND v.schedule_kind = 'rider' AND c.billing_category IN "
                       "(SELECT schedule_code FROM tariff_versions WHERE utility = ?) GROUP BY c.billing_category ORDER BY 1",
                       (utility, code, utility))
    if comps:
        return _answer("applicability", f"No structured applicability rule is stored for {code}; its stored rate rows name "
                                        f"these {utility} schedules: " + ", ".join(c["billing_category"] for c in comps) + ".",
                       [{"table": "tariff_components", "id": c["id"], "source_page": c["source_page"]} for c in comps])
    return _refuse("applicability", f"an applicability rule or per-schedule rate rows for {utility} {code}", "")


def inventory(store, q):
    utilities = store.utility_in(q)
    if len(utilities) != 1:
        return _refuse("schedule inventory", "a single stored utility name", "Stored utilities: " + "; ".join(store.utilities) + ".")
    rows = store.rows("SELECT schedule_kind, book_label, schedule_code, MIN(id) AS id FROM tariff_versions WHERE utility = ? "
                      "GROUP BY 1, 2, 3 ORDER BY 2, 1, 3", (utilities[0],))
    groups = {}
    for r in rows:
        groups.setdefault((r["book_label"], r["schedule_kind"]), []).append(r["schedule_code"])
    text = "; ".join(f"{book} — {kind}: {', '.join(codes)}" for (book, kind), codes in groups.items())
    return _answer("schedule inventory", f"{utilities[0]} holds {len(rows)} schedule codes. {text}.",
                   [{"table": "tariff_versions", "id": r["id"], "book_label": r["book_label"]} for r in rows])


def owner(store, q):
    codes = store.codes_in(q)
    if not codes:
        return _refuse("schedule owner", "a stored schedule code", "")
    code = codes[0]
    rows = store.rows("SELECT utility, MIN(id) AS id FROM tariff_versions WHERE schedule_code = ? GROUP BY utility ORDER BY 1", (code,))
    return _answer("schedule owner", f"Schedule {code} is stored for: " + "; ".join(r["utility"] for r in rows) + ".",
                   [{"table": "tariff_versions", "id": r["id"]} for r in rows])


def versions(store, q):
    entity, stop = _resolve(store, q, "version lookup")
    if stop:
        return stop
    utility, code = entity
    rows = store.rows("SELECT id, version, effective_from, effective_to, status, book_label FROM tariff_versions WHERE utility = ? "
                      "AND schedule_code = ? ORDER BY effective_from, id", (utility, code))
    parts = [f"effective {r['effective_from']} to {r['effective_to'] or 'open'} ({r['status']}, version {r['version']}, "
             f"{r['book_label']})" for r in rows]
    return _answer("version lookup", f"{utility} schedule {code} has {len(rows)} stored version"
                   f"{'s' if len(rows) != 1 else ''}: " + "; ".join(parts) + ".",
                   [{"table": "tariff_versions", "id": r["id"]} for r in rows])


def bill_facts(store, q):
    n = int(re.search(r"\bbill\s*#?\s*(\d+)", q, re.I).group(1))
    rows = store.rows("SELECT bill_key, billed_total, expected_total, delta, audit_run_id, matched_lines, mismatched_lines "
                      "FROM mart.fact_bill WHERE bill_key = ?", (n,))
    if not rows:
        return _refuse("bill facts", f"bill {n}", f"Stored bills: {store.conn.execute('SELECT COUNT(*) FROM mart.fact_bill').fetchone()[0]}.")
    b = rows[0]
    if b["audit_run_id"] is None:
        return _answer("bill facts", f"Bill {n}: printed total ${b['billed_total']}. It has no audit run, so there is no "
                                     "expected total or verdict.", [{"table": "mart.fact_bill", "id": n}])
    verdict = "matched" if not b["mismatched_lines"] else f"{b['mismatched_lines']} mismatched lines"
    return _answer("bill facts", f"Bill {n}: printed total ${b['billed_total']}; expected total ${b['expected_total']} "
                                 f"(in-scope delta ${b['delta']}); verdict: {verdict} ({b['matched_lines']} lines MATCH).",
                   [{"table": "mart.fact_bill", "id": n}, {"table": "audit_runs", "id": b["audit_run_id"]}])


ROUTES = [
    (re.compile(r"\bbill\s*#?\s*\d+", re.I), bill_facts),
    (re.compile(r"\bversions?\b", re.I), versions),
    (re.compile(r"\bwhich (?:company|utility|utilities|companies)\b", re.I), owner),
    (re.compile(r"\bapply|applies|applicab", re.I), applicability),
    (re.compile(r"\brules?\b", re.I), rule_lookup),
    (re.compile(r"\bwhat schedules\b|\bschedules does\b|\binventory\b", re.I), inventory),
    (re.compile(r"\b(?:charge|rate|price|fee)\b", re.I), rate_lookup),
]


def answer(question, conn=None):
    own = conn is None
    conn = conn or db.connect(db.default_target(ROOT / "data" / "tariffs.db"), readonly=True)
    try:
        store = Store(conn)
        for pattern, fn in ROUTES:
            if pattern.search(question):
                return fn(store, question)
        return _answer("unrecognized", "I can only answer these kinds of questions from the verified store: "
                       + " | ".join(KINDS), [], refused=True)
    finally:
        if own:
            conn.close()


def main(argv=None):
    question = " ".join(argv if argv is not None else sys.argv[1:])
    print(json.dumps(answer(question), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
