"""Stage 12 packets + coordinator over the existing AI extraction.

One shared reading per schedule section (the cached model response) feeds two separable passes:

  rates pass  -> verifies and stores components     (ai_extract._verify_component, unchanged)
  rules pass  -> verifies and stores rules          (ai_extract._verify_rule, unchanged)

The coordinator runs both passes per packet, stores drafts, and activates a schedule version only
when both sides pass. A failed side leaves the version a draft and queues that side's items; it
can re-run alone from the same cached reading. A reading that is
not in the cache is read live when a client or key exists, otherwise ReadingMissing is raised.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

try:
    from src import ai_extract, db, document_intake, parse_review
    from src import activate_version as activation
    from src import validate_draft
    from src import structured_proposals
except ImportError:
    import ai_extract
    import db
    import document_intake
    import parse_review
    import activate_version as activation
    import validate_draft
    import structured_proposals

RATES, RULES = "rates", "rules"
SIDES = (RATES, RULES)
SOURCE_KIND = ai_extract.SOURCE_KIND


class ReadingMissing(Exception):
    """The shared reading is not cached. No model call is made; the caller must report it."""


class PacketError(ValueError):
    """A packet or side cannot be run (unknown schedule, version not a draft, ...)."""


@dataclass
class Packet:
    """One schedule section and its shared reading."""
    schedule_code: str
    designation: str
    kind: str
    page_numbers: list
    section: dict
    cache_key: str
    reading_sha: str = ""
    skip: str = None          # why no usable reading exists (section-level)
    queued: list = field(default_factory=list)   # queue items for a skip
    payload: dict = None
    disposition: str = None
    effective: str = None     # ISO date printed on the section, once admitted (single-year packets)
    admission_queue: list = field(default_factory=list)
    years: list = field(default_factory=list)   # [{label, effective_from, effective_to}] once admitted
    multi_year: bool = False  # the reading declared 2+ rate years: one version per verified year


@dataclass
class Book:
    state: str
    utility: str
    version_number: int
    packets: list
    unreadable_pages: list
    model_calls: int = 0
    digest: str = ""


@dataclass
class SideResult:
    side: str
    passed: bool
    rows: list = field(default_factory=list)
    queued: list = field(default_factory=list)
    detail: str = ""


# ---------------------------------------------------------------- packets
def read_packets(source, cache_dir=None, only=None, client=None, prompt=None):
    """Chunk the book and attach each section's reading.

    A cached reading is replayed. A missing one is read live through `ai_extract.read_section`
    (same prompt and response handling as the fused path) and written through to the cache, but
    only when a client is passed or a key is configured; otherwise ReadingMissing is raised.
    `prompt` is a callable returning the system prompt, evaluated once on the first live call.
    """
    source = Path(source)
    pages = ai_extract.read_pages(source)
    detection = document_intake.identify_document(source)
    if detection["doc_kind"] != document_intake.TARIFF or not detection["utility"] or not detection["state"]:
        raise ai_extract.BookRefused("the source is not an identified utility tariff")
    match = re.search(r"(\d+)", detection["version_label"] or "")
    if not match:
        for page in pages[:6]:
            match = re.search(r"TARIFF\s+NO\.?\s*(\d+)", page["text"], re.I)
            if match:
                break
    if not match:
        raise ai_extract.BookRefused("the book prints no tariff or amendment number to store as its version")
    sections, unreadable = ai_extract.chunk_pages(pages)
    if not sections:
        raise ai_extract.BookRefused("the book contains no recognisable schedule sections")

    digest = ai_extract.content_hash(source)
    cache_file = ai_extract._cache_path(cache_dir or ai_extract.CACHE_DIR, digest)
    cache = ai_extract._load_cache(cache_file)
    packets, model_calls, system = [], 0, None
    for section in sections:
        code = section["schedule_code"]
        if only and code not in only:
            continue
        base = dict(schedule_code=code, designation=section["designation"], kind=section["kind"],
                    page_numbers=list(section["page_numbers"]), section=section, cache_key="")
        if not section["page_numbers"] or not any(ai_extract.readable(p["text"]) for p in section["pages"]):
            reason = (f"no readable page carries {section['designation']}; "
                      f"unreadable PDF pages: {unreadable or 'none'}")
            packets.append(Packet(**base, skip="unreadable", queued=[
                ai_extract._queue(code, "pages", reason, "unreadable_pages", reason)]))
            continue
        cache_key = f"{code}|{section['page_numbers'][0]}-{section['page_numbers'][-1]}"
        base["cache_key"] = cache_key
        cached = cache["sections"].get(cache_key)
        if cached and cached.get("model") not in (None, ai_extract.MODEL):
            cached = None
        if not cached:
            if client is None:
                key = ai_extract.api_key()
                if not key:
                    raise ReadingMissing(
                        f"no cached reading for section {cache_key} and no model client or key is available")
                client = ai_extract.AnthropicExtractor(key)
            if system is None:
                system = prompt() if prompt else ai_extract._system_prompt(
                    {"component_type": [], "rule_type": [], "unit": sorted(ai_extract.VALID_UNITS)},
                    rate_years=True)
            raw, stop, delta = ai_extract.read_section(section, system, client, rate_years=True)
            model_calls += 1
            cached = {"raw": raw, "stop_reason": stop, "usage": delta, "model": ai_extract.MODEL}
            cache["sections"][cache_key] = cached
            ai_extract._save_cache(cache_file, cache)
        raw, stop = cached.get("raw", ""), cached.get("stop_reason")
        base["reading_sha"] = hashlib.sha256((raw or "").encode("utf-8")).hexdigest()
        if stop not in ("end_turn", None):
            reason = f"model output stopped at {stop}"
            packets.append(Packet(**base, skip="incomplete", queued=[
                ai_extract._queue(code, "response", stop or "", "model_output_incomplete", reason)]))
            continue
        try:
            payload = ai_extract.parse_model_json(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            reason = "model output was not the JSON object required"
            packets.append(Packet(**base, skip="unreadable_output", queued=[
                ai_extract._queue(code, "response", "", "model_output_unreadable", reason)]))
            continue
        packet = Packet(**base, payload=payload)
        admit(packet)
        packets.append(packet)
    return Book(detection["state"], detection["utility"], int(match.group(1)), packets, unreadable, model_calls, digest)


def _printed_iso(value, pages):
    """(iso date, failure) for a date string that must be a real date printed on the section's pages."""
    parsed = ai_extract.iso_date(value) if value else None
    try:
        day = date.fromisoformat(parsed) if parsed else None
    except ValueError:
        day = None
    if day is None:
        return None, ("effective_date_unparsed", "the printed date could not be read as a date")
    if not any(ai_extract.printed_date_on_page(day, text) for text in pages.values()):
        return None, ("quote_not_found", "the printed date is not on this section's pages")
    return day.isoformat(), None


def _admit_rate_years(packet, pages):
    """Multi-year reading: each year's effective_from (and effective_to) must be a date printed on the pages."""
    code, queue, seen = packet.schedule_code, packet.admission_queue, set()
    for index, item in enumerate(packet.payload.get("rate_years")):
        label = str(item.get("label") or "").strip() if isinstance(item, dict) else ""
        if not label or label in seen:
            queue.append(ai_extract._queue(code, f"rate_year:{index}", label, "model_output_unreadable",
                                           "a rate year needs a unique printed label", "year", index))
            continue
        seen.add(label)
        start, failure = _printed_iso(item.get("effective_from"), pages)
        if failure:
            queue.append(ai_extract._queue(code, f"rate_year_start:{label}", str(item.get("effective_from")),
                                           failure[0], f"rate year {label}: {failure[1]}", "year", index))
            continue
        end = None
        if item.get("effective_to"):
            end, failure = _printed_iso(item.get("effective_to"), pages)
            if failure or end < start:
                queue.append(ai_extract._queue(
                    code, f"rate_year_end:{label}", str(item.get("effective_to")), failure[0] if failure
                    else "effective_date_unparsed", f"rate year {label}: the printed end date is not usable; "
                    "the version is stored without an end date", "year", index))
                end = None
        packet.years.append({"label": label, "effective_from": start, "effective_to": end})
    packet.years.sort(key=lambda y: y["effective_from"])
    starts = [y["effective_from"] for y in packet.years]
    if len(starts) != len(set(starts)):
        queue.append(ai_extract._queue(code, "rate_years", ",".join(starts), "model_output_unreadable",
                                       "two rate years start on the same date; none is stored", "year"))
        packet.years = []


def admit(packet):
    """Reading-level gate shared by both sides: disposition and the printed effective date.

    A reading that declares 2+ rate_years is multi-year: the section-level date is not required;
    each verified year becomes its own version (the rate-year decision).
    """
    payload, code, pages = packet.payload, packet.schedule_code, ai_extract._page_map(packet.section)
    queue = packet.admission_queue
    packet.disposition = payload.get("disposition")
    if packet.disposition not in ("rates", "cross_reference_only"):
        queue.append(ai_extract._queue(code, "disposition", str(packet.disposition), "model_output_unreadable",
                                       "disposition must be rates or cross_reference_only"))
        return
    declared = payload.get("rate_years")
    if isinstance(declared, list) and len(declared) >= 2:
        packet.multi_year = True
        _admit_rate_years(packet, pages)
        return
    printed = payload.get("effective_date")
    parsed = ai_extract.iso_date(printed)
    try:
        effective = date.fromisoformat(parsed) if parsed else None
    except ValueError:
        effective = None
    if effective is None:
        queue.append(ai_extract._queue(code, "effective_date", str(printed), "effective_date_unparsed",
                                       "the printed effective date could not be read as a date"))
        return
    if not any(ai_extract.printed_date_on_page(effective, text) for text in pages.values()):
        queue.append(ai_extract._queue(code, "effective_date", str(printed), "quote_not_found",
                                       "the printed effective date is not on this section's pages"))
        return
    packet.effective = effective.isoformat()
    packet.years = [{"label": None, "effective_from": packet.effective, "effective_to": None}]


# ---------------------------------------------------------------- the two passes
def rates_pass(packet, vocab, new_values):
    """Verify components from the shared reading. Passes when at least one component is verified
    (or the section prices nothing, disposition cross_reference_only)."""
    code, pages = packet.schedule_code, ai_extract._page_map(packet.section)
    result = SideResult(RATES, True)
    if packet.disposition != "rates":
        result.detail = "nothing to price (cross reference only)"
        return result
    for index, item in enumerate(packet.payload.get("components") or []):
        if not isinstance(item, dict):
            continue
        row, failure = ai_extract._verify_component(item, pages, vocab, new_values)
        if failure:
            result.queued.append(ai_extract._queue(code, failure["field"], failure["value"], failure["signal"],
                                                   failure["detail"], "component", index))
            continue
        if packet.multi_year:
            label = str(item.get("rate_year") or "").strip() or None
            if label not in {y["label"] for y in packet.years}:
                reason = ("the component carries no rate_year" if label is None
                          else f"rate_year {label!r} is not a verified rate year of this section")
                result.queued.append(ai_extract._queue(code, "rate_year", label or "", "rate_year_missing",
                                                       f"{reason}; it is stored in no version", "component", index))
                continue
            row["rate_year"] = label
        result.rows.append(row)
    if not result.rows:
        result.passed = False
        result.detail = "no component survived verification"
        result.queued.append(ai_extract._queue(code, "components", "0", "no_verified_rates",
                                               "no component survived verification; nothing was stored"))
    return result


def rules_pass(packet, vocab, new_values):
    """Verify rules from the same shared reading. A section with no rules passes."""
    code, pages = packet.schedule_code, ai_extract._page_map(packet.section)
    result = SideResult(RULES, True)
    for index, item in enumerate(packet.payload.get("rules") or []):
        if not isinstance(item, dict):
            continue
        row, failure = ai_extract._verify_rule(item, pages, vocab, new_values)
        if failure:
            result.queued.append(ai_extract._queue(code, failure["field"], failure["value"], failure["signal"],
                                                   failure["detail"], "rule", index))
        elif row:
            result.rows.append(row)
    return result


PASSES = {RATES: rates_pass, RULES: rules_pass}


# ---------------------------------------------------------------- storage (one function per side)
def store_rates(conn, version_id, rows):
    seen, stored = set(), 0
    for c in rows:
        key = (c["component_type"], c["billing_category"], str(c["rate"]), c["unit"], c["variant"])
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            "INSERT INTO tariff_components(tariff_version_id, component_type, billing_category, "
            "rate, formula, unit, variant, source_page, source_cell) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (version_id, c["component_type"], c["billing_category"], float(c["rate"]), c["unit"],
             c["variant"], c["source_page"], c["source_cell"]))
        stored += 1
    return stored


def store_rules(conn, version_id, rows):
    seen, stored = set(), 0
    for r in rows:
        key = (r["rule_type"], r["rule_text"])
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            "INSERT INTO tariff_rules(tariff_version_id, rule_type, rule_text, source_page, citation_status) "
            "VALUES (?, ?, ?, ?, ?)",
            (version_id, r["rule_type"], r["rule_text"], r["source_page"], r["citation_status"]))
        stored += 1
    return stored


STORE = {RATES: store_rates, RULES: store_rules}
TABLE = {RATES: "tariff_components", RULES: "tariff_rules"}


def clear_side(conn, version_id, side):
    conn.execute(f"DELETE FROM {TABLE[side]} WHERE tariff_version_id = ?", (version_id,))


def ensure_gate_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS packet_gates ("
        "tariff_version_id INTEGER NOT NULL, side TEXT NOT NULL, passed INTEGER NOT NULL, "
        "stored_rows INTEGER NOT NULL, detail TEXT, reading_sha TEXT, "
        "PRIMARY KEY (tariff_version_id, side))")


def record_gate(conn, version_id, side, passed, stored_rows, detail, reading_sha):
    conn.execute("DELETE FROM packet_gates WHERE tariff_version_id = ? AND side = ?", (version_id, side))
    conn.execute(
        "INSERT INTO packet_gates(tariff_version_id, side, passed, stored_rows, detail, reading_sha) "
        "VALUES (?, ?, ?, ?, ?, ?)", (version_id, side, 1 if passed else 0, stored_rows, detail, reading_sha))


def _queue_items(conn, utility, items):
    return sum(
        1 for i in items if parse_review.queue_item(
            conn, SOURCE_KIND, i["source_ref"], i["schedule_code"], i["field"], str(i["value"])[:500],
            i["signal"], f"{utility} {i['schedule_code']}: {i['detail']}"))


def _side_of_failure(text):
    if text.startswith("Component"):
        return RATES
    if text.startswith("Rule"):
        return RULES
    return None


def _finish(conn, version_id, schedule_code, utility, reading_sha):
    """Draft validation attributed to sides, then activate iff both persisted sides pass."""
    failures = validate_draft.validate_drafts(conn, version_id)
    added = 0
    for side in SIDES:
        mine = [f for f in failures if _side_of_failure(f) in (side, None)]
        if mine:
            row = conn.execute("SELECT stored_rows, detail FROM packet_gates WHERE tariff_version_id = ? "
                               "AND side = ?", (version_id, side)).fetchone()
            record_gate(conn, version_id, side, False, row[0] if row else 0, mine[0][:200], reading_sha)
            for failure in mine:
                added += int(parse_review.queue_item(
                    conn, SOURCE_KIND, f"version {version_id}", schedule_code, f"validation:{side}",
                    failure[:200], "validation_failed",
                    f"{utility} {schedule_code} stays a draft ({side} side): {failure}"))
    conn.commit()
    gates = dict(conn.execute("SELECT side, passed FROM packet_gates WHERE tariff_version_id = ?",
                              (version_id,)).fetchall())
    if all(gates.get(side) == 1 for side in SIDES):
        activation.activate_with_alert(conn, version_id)
        return "activated", added
    return "held", added


# ---------------------------------------------------------------- structured stage
def _structured_stage(target, book, units, client, structured_cache_dir):
    """Separate structured reading per section, verified before storage. Never blocks rates or rules.

    Live calls are made only for versions created in this run and only when a client or key exists;
    versions that already existed replay from the structured cache. Returns the run summary.
    """
    summary = {"stage": "skipped", "reasons": [], "calls": 0, "proposed": 0, "stored": 0, "queued": 0}
    if not units:
        summary["reasons"].append("no version was created or found")
        return summary
    if client is None:
        key = ai_extract.api_key()
        client = ai_extract.AnthropicExtractor(key) if key else None
    cache_file = ai_extract._cache_path(structured_cache_dir or structured_proposals.CACHE_DIR, book.digest)
    cache = ai_extract._load_cache(cache_file)
    reasons, used = set(), False
    with db.connect(target) as conn:
        for row, packet, version_id, is_new in units:
            entry = cache["sections"].get(packet.cache_key)
            if entry and entry.get("model") not in (None, ai_extract.MODEL):
                entry = None
            components = [dict(zip(("component_type", "unit", "variant", "rate"), r)) for r in conn.execute(
                "SELECT component_type, unit, variant, rate FROM tariff_components WHERE tariff_version_id = ?",
                (version_id,)).fetchall()]
            if entry is None:
                if not is_new:
                    reasons.add("a version that already existed has no cached structured reading")
                    continue
                if client is None:
                    reasons.add("no model client or API key is available")
                    continue
                pairs = sorted(
                    {(c["component_type"], c["variant"]) for c in components},
                    key=lambda pair: (pair[0] or "", pair[1] or ""))
                system = structured_proposals.system_prompt(
                    sorted({c["component_type"] for c in components if c["component_type"]}), pairs)
                try:
                    raw, stop, delta = structured_proposals.read_structured(packet.section, system, client)
                except Exception as error:
                    reasons.add(f"structured reading failed: {type(error).__name__}")
                    continue
                summary["calls"] += 1
                entry = {"raw": raw, "stop_reason": stop, "usage": delta, "model": ai_extract.MODEL}
                cache["sections"][packet.cache_key] = entry
                ai_extract._save_cache(cache_file, cache)
            used = True
            proposals, error = structured_proposals.parse_proposals(entry.get("raw", ""), entry.get("stop_reason"))
            label = next((y["label"] for y in packet.years if y["effective_from"] == row.get("effective_from")), None)
            if error:
                item = ai_extract._queue(packet.schedule_code, "structured_reading", "",
                                         structured_proposals.SIG_READING, error, "structured")
                item["source_ref"] += f":v{version_id}"
                queued = [item]
                rows_ok, applicable = [], 0
            else:
                rows_ok, queued, applicable = structured_proposals.verify_proposals(
                    proposals, packet.section, components, version_id, rate_year=label, multi_year=packet.multi_year)
            conn.execute("BEGIN IMMEDIATE")
            try:
                stored, conflicts = structured_proposals.store_proposals(conn, version_id, packet.schedule_code, rows_ok)
                new_queue = _queue_items(conn, book.utility, queued + conflicts)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            row["structured_proposed"] = applicable
            row["structured_stored"] = stored
            row["structured_queued"] = len(queued) + len(conflicts)
            summary["proposed"] += applicable
            summary["stored"] += stored
            summary["queued"] += new_queue
    if used:
        summary["stage"] = "ran"
    summary["reasons"] = sorted(reasons)
    return summary


# ---------------------------------------------------------------- coordinator
def run_book(source, target, cache_dir=None, client=None, structured_cache_dir=None):
    """Read the book (cache, then live for missing sections), run both passes, activate where both pass.

    A section whose reading declares 2+ rate years yields one version per verified year; the section's
    text rules and structured proposals attach to every year-version. A structured stage then proposes
    and verifies structured rules (it can be skipped without affecting anything above).
    """
    def prompt():
        with db.connect(target, readonly=True) as conn:
            return ai_extract._system_prompt(ai_extract.vocabulary(conn), rate_years=True)

    book = read_packets(source, cache_dir=cache_dir, client=client, prompt=prompt)
    state, utility = book.state, book.utility
    table, created, units = [], [], []
    new_values = {"component_type": set(), "rule_type": set(), "unit": set()}
    with db.connect(target) as conn:
        ensure_gate_table(conn)
        conn.commit()
        vocab = ai_extract.vocabulary(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for packet in book.packets:
                code = packet.schedule_code
                row = {"schedule": code, "rates": "fail", "rules": "fail", "outcome": "", "queued": 0,
                       "components": 0, "rules_stored": 0, "signals": [], "structured_proposed": 0,
                       "structured_stored": 0, "structured_queued": 0}
                if packet.skip:
                    row["queued"] = _queue_items(conn, utility, packet.queued)
                    row["outcome"] = f"not stored: {packet.skip}"
                    row["signals"] = [i["signal"] for i in packet.queued]
                    table.append(row)
                    continue
                rates = PASSES[RATES](packet, vocab, new_values)
                rules = PASSES[RULES](packet, vocab, new_values)
                items = packet.admission_queue + rates.queued + rules.queued
                row["signals"] = [i["signal"] for i in items]
                row["rates"] = "pass" if rates.passed else "fail"
                row["rules"] = "pass" if rules.passed else "fail"
                if not packet.years or not rates.passed:
                    row["queued"] = _queue_items(conn, utility, items)
                    row["outcome"] = "not stored: " + ("reading gate" if not packet.years else "rates side failed")
                    if not packet.years:
                        row["rates"] = row["rules"] = "fail"
                    table.append(row)
                    continue
                kind = "informational" if packet.kind == "exhibit" else packet.kind
                first = True
                for year in packet.years:
                    unit = dict(row, signals=list(row["signals"]))
                    label = year["label"]
                    if packet.multi_year:
                        unit["rate_year"] = label
                        unit["effective_from"] = year["effective_from"]
                    year_rows = [r for r in rates.rows if not packet.multi_year or r.get("rate_year") == label]
                    unit["queued"] = _queue_items(conn, utility, items) if first else 0
                    first = False
                    table.append(unit)
                    if packet.multi_year and not year_rows:
                        unit["rates"] = "fail"
                        unit["outcome"] = f"not stored: no verified rates for rate year {label}"
                        unit["queued"] += _queue_items(conn, utility, [ai_extract._queue(
                            code, f"components:{label}", "", "no_verified_rates",
                            f"no component verified for rate year {label}; no version is created", "year")])
                        continue
                    identity = (state, utility, code, book.version_number, year["effective_from"])
                    found = conn.execute(
                        "SELECT id FROM tariff_versions WHERE state = ? AND utility = ? AND schedule_code = ? "
                        "AND version = ? AND effective_from = ? ORDER BY id DESC LIMIT 1", identity).fetchone()
                    if found:
                        conn.execute("UPDATE tariff_versions SET schedule_kind = ? WHERE id = ?", (kind, found[0]))
                        unit["outcome"] = f"existing version {found[0]}; unchanged"
                        units.append((unit, packet, found[0], False))
                        continue
                    cursor = conn.execute(
                        "INSERT INTO tariff_versions(state, utility, schedule_code, version, effective_from, "
                        "effective_to, status, schedule_kind) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)",
                        (*identity, year["effective_to"], kind))
                    version_id = cursor.lastrowid
                    year_side = SideResult(RATES, True, year_rows, [], rates.detail)
                    for side, result in ((RATES, year_side), (RULES, rules)):
                        stored = STORE[side](conn, version_id, result.rows)
                        record_gate(conn, version_id, side, result.passed, stored, result.detail, packet.reading_sha)
                        unit["components" if side == RATES else "rules_stored"] = stored
                    unit["version_id"] = version_id
                    created.append((unit, code, packet.reading_sha))
                    units.append((unit, packet, version_id, True))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        for row, code, sha in created:
            row["outcome"], extra = _finish(conn, row["version_id"], code, utility, sha)
            row["queued"] += extra
    structured = _structured_stage(target, book, units, client, structured_cache_dir)
    return {"state": state, "utility": utility, "tariff_number": book.version_number, "model_calls": book.model_calls,
            "structured_calls": structured["calls"], "structured": structured,
            "table": table, "unreadable_pages": book.unreadable_pages,
            "new_vocabulary": {k: sorted(v) for k, v in new_values.items()}}


def rerun_side(source, target, schedule_code, side, cache_dir=None, rate_year=None):
    """Re-run ONE side of one packet from the same cached reading; the other side is not touched."""
    if side not in SIDES:
        raise PacketError(f"side must be one of {SIDES}")
    book = read_packets(source, cache_dir=cache_dir, only={schedule_code})
    if not book.packets:
        raise PacketError(f"no packet for schedule {schedule_code}")
    packet = book.packets[0]
    if packet.skip or not packet.years:
        raise PacketError(f"{schedule_code} has no admitted reading to re-run")
    years = [y for y in packet.years if rate_year in (None, y["label"])]
    if len(years) != 1:
        raise PacketError(f"{schedule_code} has {len(packet.years)} rate years; name exactly one with rate_year")
    effective = years[0]["effective_from"]
    with db.connect(target) as conn:
        ensure_gate_table(conn)
        version = conn.execute(
            "SELECT id, status FROM tariff_versions WHERE state = ? AND utility = ? AND schedule_code = ? "
            "AND version = ? AND effective_from = ? ORDER BY id DESC LIMIT 1",
            (book.state, book.utility, schedule_code, book.version_number, effective)).fetchone()
        if version is None:
            raise PacketError(f"{schedule_code} has no stored version to re-run into")
        version_id, status = version
        if status != "draft":
            raise PacketError(f"version {version_id} is {status}; only a draft side can be re-run")
        prior = conn.execute("SELECT reading_sha FROM packet_gates WHERE tariff_version_id = ? AND side = ?",
                             (version_id, side)).fetchone()
        vocab = ai_extract.vocabulary(conn)
        new_values = {"component_type": set(), "rule_type": set(), "unit": set()}
        result = PASSES[side](packet, vocab, new_values)
        if side == RATES and packet.multi_year:
            result.rows = [r for r in result.rows if r.get("rate_year") == years[0]["label"]]
            result.passed = bool(result.rows)
        conn.execute("BEGIN IMMEDIATE")
        try:
            clear_side(conn, version_id, side)
            stored = STORE[side](conn, version_id, result.rows) if result.passed or result.rows else 0
            record_gate(conn, version_id, side, result.passed, stored, result.detail, packet.reading_sha)
            added = _queue_items(conn, book.utility, result.queued)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        outcome, extra = _finish(conn, version_id, schedule_code, book.utility, packet.reading_sha)
    return {"schedule": schedule_code, "side": side, "version_id": version_id, "model_calls": book.model_calls,
            "same_reading": prior is not None and prior[0] == packet.reading_sha,
            "reading_sha": packet.reading_sha, "stored_rows": stored, "side_passed": result.passed,
            "queued_new": added + extra, "outcome": outcome}
