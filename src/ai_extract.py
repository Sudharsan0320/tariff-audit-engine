"""AI extraction adapter for an electric utility tariff book (Stage 11B).

A hand-written adapter wins where one exists (Dominion). This module is the route for every
other detected tariff: it splits the book into schedule sections, asks an injectable model
client for strict JSON one schedule at a time, and stores a component only when deterministic
code can find both the quoted text and the rate on the cited page. The model is never asked
to confirm its own output. Identical bytes are cached by content hash and model id, so a re-run of the same model
does not call it again, and a response cached for a different model is not reused.

The model id is the current Sonnet model the account can call. A missing key, a billing
refusal, and any other API failure each refuse the book with their own reason and write
no rows.
"""

import hashlib
import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber

try:
    from src import db
except ImportError:
    import db

if __package__:
    from . import parse_review
    from .validate_draft import VALID_UNITS
else:
    import parse_review
    from validate_draft import VALID_UNITS

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "cache" / "ai-extract"

# Current Sonnet model on the account's model list (newer than claude-sonnet-5).
MODEL = "claude-sonnet-5-5"
NOT_CONFIGURED = "AI extraction not configured"
BILLING = "AI extraction billing"
UNAVAILABLE = "AI extraction unavailable"
SOURCE_KIND = "tariff_book_section"

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+")
SHEET = re.compile(
    r"Sheet(?:\s+No\.)?\s+((?:NBP[\s-]*)?\d+[A-Z]?(?:\s*\.?\s*-\s*\d+)?)", re.I)
HEADING = re.compile(r"^(SCHEDULE|OPTIONAL RIDER|NBP-?\s*RIDER|RIDER)\s+(\S.*?)\s*$", re.I)
EXHIBIT = re.compile(r"^EXHIBIT OF APPLICABLE RIDERS\b", re.I)
INDEX_START = re.compile(r"^(Schedule|Optional Rider|Rider|NBP-Rider|Underground|Exhibit|Towns|Terms)\b")
INDEX_TAIL = re.compile(
    r"((?:NBP-)?\d+[A-Z]?(?:\s*\.?\s*-\s*\d+)?)(?:\s+thru\s+((?:NBP-)?\d+[A-Z]?(?:-\d+)?))?"
    r"(?:\s+(\d+A-\d+\s+thru\s+\d+A-\d+))?\s*$")
FOOTER = re.compile(r"^(Issued:|Pursuant to|Dated:|Case[: ]|ACCEPTED FOR FILING|PUBLIC UTILITY REGULATION)")


class ExtractionUnavailable(Exception):
    """The model cannot be called. The book is saved and no rows are written.

    `reason` is not_configured, billing, or unavailable.
    """

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


def classify_api_failure(error):
    """Map an SDK or network exception to billing or unavailable. Never reads the key."""
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    message = ""
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict):
            message = str(err.get("message") or "")
    blob = f"{type(error).__name__} {message}".lower()
    billing_words = ("credit balance", "credit", "billing", "payment", "balance is too low")
    if status in (400, 402) and any(word in blob for word in billing_words):
        return "billing"
    if "credit balance" in blob or "balance is too low" in blob:
        return "billing"
    return "unavailable"


class BookRefused(ValueError):
    """The source is not a tariff book this adapter can read."""


def api_key():
    """ANTHROPIC_API_KEY from the environment or the gitignored .env. Never logged."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() == "ANTHROPIC_API_KEY":
                    key = value.strip().strip('"').strip("'")
                    break
    key = (key or "").strip()
    return key or None


class AnthropicExtractor:
    """Official Anthropic SDK client. `complete` returns (text, stop_reason)."""

    def __init__(self, key):
        import anthropic
        self._client = anthropic.Anthropic(api_key=key)
        self.calls = 0
        self.usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        self.model = MODEL

    def complete(self, system, user):
        self.calls += 1
        try:
            message = self._client.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as error:
            kind = classify_api_failure(error)
            message = BILLING if kind == "billing" else UNAVAILABLE
            raise ExtractionUnavailable(message, kind) from None
        usage = message.usage
        for field in self.usage:
            self.usage[field] += int(getattr(usage, field, 0) or 0)
        texts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
        return "\n".join(texts), message.stop_reason


def content_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def squash(text):
    return " ".join(str(text).split())


def read_pages(path):
    pages = []
    try:
        with pdfplumber.open(path) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                pages.append({"n": number, "text": page.extract_text() or ""})
    except Exception as error:
        raise BookRefused(f"the source could not be read as a PDF: {type(error).__name__}") from None
    if not pages:
        raise BookRefused("the source has no pages")
    return pages


def readable(text):
    if len(text.strip()) < 40:
        return False
    control = sum(1 for char in text if ord(char) < 32 and char not in "\r\n\t")
    return control / len(text) < 0.05 and text.count("(cid:") < 5


def normalize_sheet(raw):
    text = re.sub(r"\s+", "", raw.upper())
    text = text.replace(".-", "-")
    if text.startswith("NBP"):
        text = "NBP-" + text[3:].lstrip("-")
    return text


def sheet_of(text):
    match = SHEET.search("\n".join(text.splitlines()[:6]))
    return normalize_sheet(match.group(1)) if match else None


def sheet_prefix(sheet):
    if sheet.startswith("NBP-"):
        match = re.search(r"\d+", sheet)
        return "NBP-" + match.group(0) if match else sheet
    return sheet.split("-")[0]


def heading_of(text):
    for line in text.splitlines()[:15]:
        line = line.strip()
        match = HEADING.match(line)
        if match:
            kind = "rider" if "RIDER" in match.group(1).upper() else "schedule"
            return kind, match.group(2).strip()
        if EXHIBIT.match(line):
            return "exhibit", "Exhibit of Applicable Riders"
    return None, None


def schedule_code(designation, kind=None):
    if kind == "exhibit":
        return "EXHIBIT"
    text = designation.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s*-\s*", "-", text).replace(".", "")
    text = re.sub(r"\s+", "", text).strip("-")
    return text or None


def index_entries(pages):
    """Table-of-contents rows. This book prints sheet ranges, not PDF page ranges."""
    lines = []
    for page in pages:
        sheet = sheet_of(page["text"]) if readable(page["text"]) else None
        if not sheet or not sheet.startswith("1-"):
            continue
        for line in page["text"].splitlines():
            line = line.strip()
            if not line or line.startswith(("APPALACHIAN", "VA. S.C.C.", "INDEX", "STANDARD SERVICE")):
                continue
            if FOOTER.match(line):
                continue
            lines.append(re.sub(r"\.{3,}|…", " ", line).strip())
    entries, parsed = [], []
    for line in lines:
        if not line:
            continue
        if INDEX_START.match(line):
            entries.append(line)
        elif entries:
            entries[-1] += " " + line
    for text in entries:
        match = INDEX_TAIL.search(text)
        if not match:
            continue
        head = text[:match.start()].strip()
        kind_match = re.match(r"^(Schedule|Optional Rider|NBP-Rider|Rider)\s+(.*)$", head)
        if not kind_match:
            continue
        tokens = kind_match.group(2).split()
        if not tokens:
            continue
        designation = [tokens[0]]
        for token in tokens[1:]:
            previous = designation[-1]
            if token in "-–" or previous in "-–" or previous.endswith(("-", "–")) or token.startswith(("-", "–")) \
                    or not any(char.islower() for char in token):
                designation.append(token)
            else:
                break
        name = " ".join(tokens[len(designation):])
        start = normalize_sheet(re.sub(r"[\s.]", "", match.group(1)).replace("--", "-"))
        extra = []
        if match.group(3):
            extra_sheet = normalize_sheet(match.group(3).split()[0])
            extra.append(sheet_prefix(extra_sheet))
        parsed.append({
            "kind": "schedule" if kind_match.group(1) == "Schedule" else "rider",
            "designation": " ".join(designation),
            "name": name,
            "start_sheet": start,
            "extra_prefixes": extra,
        })
    return parsed


def _section(kind, designation, name, pages):
    code = schedule_code(designation, kind)
    numbers = [page["n"] for page in pages]
    return {
        "kind": kind,
        "designation": designation,
        "name": name or "",
        "schedule_code": code,
        "pages": pages,
        "page_numbers": numbers,
    }


def chunk_pages(pages):
    """Schedule sections from the table of contents, else from schedule headings."""
    unreadable = [page["n"] for page in pages if not readable(page["text"])]
    index = index_entries(pages)
    if len(index) >= 3:
        by_prefix = {}
        for page in pages:
            if not readable(page["text"]):
                continue
            sheet = sheet_of(page["text"])
            if sheet:
                by_prefix.setdefault(sheet_prefix(sheet), []).append(page)
        sections, used = [], set()
        for entry in index:
            prefixes = [sheet_prefix(entry["start_sheet"]), *entry.get("extra_prefixes", [])]
            group = []
            for prefix in prefixes:
                group.extend(by_prefix.get(prefix, []))
            group.sort(key=lambda page: page["n"])
            kind, designation = entry["kind"], entry["designation"]
            if group:
                found_kind, found = heading_of(group[0]["text"])
                if found:
                    kind = found_kind if found_kind != "exhibit" else kind
                    designation = found
            section = _section(kind, designation, entry["name"], group)
            if section["schedule_code"]:
                sections.append(section)
                used.update(section["page_numbers"])
        exhibit_pages = []
        for page in pages:
            if page["n"] in used or not readable(page["text"]):
                continue
            kind, designation = heading_of(page["text"])
            if kind == "exhibit":
                exhibit_pages.append(page)
        if exhibit_pages:
            sections.append(_section("exhibit", "Exhibit of Applicable Riders", "", exhibit_pages))
        return sections, unreadable
    return _heading_sections(pages), unreadable


def _heading_sections(pages):
    sections, current = [], None
    for page in pages:
        if not readable(page["text"]):
            if current is not None:
                current["pages"].append(page)
                current["page_numbers"].append(page["n"])
            continue
        kind, designation = heading_of(page["text"])
        if kind and designation:
            current = _section(kind, designation, "", [page])
            if current["schedule_code"]:
                sections.append(current)
            continue
        if current is not None:
            current["pages"].append(page)
            current["page_numbers"].append(page["n"])
    return sections


def sections_of(path):
    """Deterministic section list (no model): schedule_code and PDF page numbers."""
    pages = read_pages(path)
    sections, unreadable = chunk_pages(pages)
    return [{"schedule_code": s["schedule_code"], "designation": s["designation"], "kind": s["kind"],
             "page_numbers": list(s["page_numbers"]), "name": s["name"]} for s in sections], unreadable


def numbers_on(text):
    found = []
    for match in NUMBER.finditer(text or ""):
        try:
            found.append(Decimal(match.group(0).replace(",", "")))
        except InvalidOperation:
            continue
    return found


def rate_on_page(rate, text):
    """True when `rate` dollars, or the same amount in cents, is a number on the page."""
    cents = rate * Decimal(100)
    accepted = {rate, cents}
    if rate < 0:
        accepted |= {-rate, -cents}
    return any(number in accepted for number in numbers_on(text))


def quote_on_page(quote, text):
    if not isinstance(quote, str) or not quote.strip():
        return False
    return squash(quote) in squash(text)


def iso_date(text):
    cleaned = re.sub(r"^effective:?\s*", "", squash(text), flags=re.I)
    match = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s*(\d{4})", cleaned)
    if match:
        month = MONTHS.get(match.group(1).lower()[:3])
        day = int(match.group(2))
        if month and 1 <= day <= 31:
            return f"{int(match.group(3)):04d}-{month:02d}-{day:02d}"
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", cleaned)
    return iso.group(0) if iso else None


def vocabulary(conn):
    def distinct(sql):
        return sorted({row[0] for row in conn.execute(sql) if row[0]})
    units = sorted(set(distinct(
        "SELECT DISTINCT unit FROM tariff_components WHERE unit IS NOT NULL")) | set(VALID_UNITS))
    return {
        "component_type": distinct("SELECT DISTINCT component_type FROM tariff_components"),
        "rule_type": distinct("SELECT DISTINCT rule_type FROM tariff_rules"),
        "unit": units,
    }


def _system_prompt(vocab):
    def listing(values):
        return ", ".join(values) if values else "(none stored yet; use a short snake_case value)"
    return (
        "You extract one electric-tariff schedule from the pages given. "
        "Reply with one JSON object and no other text. "
        "Rates are decimal strings in dollars: a value printed in cents is divided by 100. "
        "source_quote must be copied verbatim from the cited page (it is checked against that page). "
        "source_page is the page number labeling that page in the input. "
        "disposition is \"rates\" when the pages state prices, or \"cross_reference_only\" when they "
        "only point at other schedules and state no price of their own. "
        "component_type, rule_type, and unit must be one of the stored values when one fits. "
        "Use a new short snake_case value only when none fits. "
        f"component_type values: {listing(vocab['component_type'])}. "
        f"rule_type values: {listing(vocab['rule_type'])}. "
        f"unit values: {listing(vocab['unit'])}. "
        "variant is generation, distribution, total, on_peak, or off_peak when the page prints that "
        "split, otherwise null. Do not invent a rate that is not printed."
    )


def _user_prompt(section):
    lines = [
        f"Section heading: {section['designation']}",
        f"Printed name: {section['name'] or 'not printed in the index'}",
        "JSON keys: designation, name, effective_date, disposition, "
        "components (printed_label, component_type, rate, unit, variant, source_page, source_quote), "
        "rules (rule_type, rule_text, source_page, source_quote).",
    ]
    for page in section["pages"]:
        lines.append(f"--- page {page['n']} ---")
        lines.append(page["text"])
    return "\n".join(lines)


def parse_model_json(text):
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(body[start:end + 1])
    if not isinstance(value, dict):
        raise json.JSONDecodeError("object required", body, 0)
    return value


def _decimal(value):
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return None


def _cache_path(cache_dir, digest, model=None):
    """One file per document hash per model, so another model's responses are not replayed."""
    model_name = model or MODEL
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", model_name)
    directory = Path(cache_dir) / safe
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{digest}.json"


def _load_cache(path):
    if not path.exists():
        return {"sections": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sections": {}}
    data.setdefault("sections", {})
    return data


def _save_cache(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _page_map(section):
    return {page["n"]: page["text"] for page in section["pages"]}


def _verify_payload(payload, section, vocab, new_values):
    """Return (components, rules, queued items). Queued items are not data."""
    pages = _page_map(section)
    joined = "\n".join(pages.values())
    queued = []
    code = section["schedule_code"]
    disposition = payload.get("disposition")
    if disposition not in ("rates", "cross_reference_only"):
        queued.append(_queue(code, "disposition", str(disposition), "model_output_unreadable",
                             "disposition must be rates or cross_reference_only"))
        return [], [], queued, None
    printed_date = payload.get("effective_date")
    if not quote_on_page(printed_date, joined):
        queued.append(_queue(code, "effective_date", str(printed_date), "quote_not_found",
                             "the printed effective date is not on this section's pages"))
        return [], [], queued, None
    effective = iso_date(printed_date)
    if effective is None:
        queued.append(_queue(code, "effective_date", str(printed_date), "effective_date_unparsed",
                             "the printed effective date could not be read as a date"))
        return [], [], queued, None

    components, rules = [], []
    if disposition == "rates":
        for index, item in enumerate(payload.get("components") or []):
            if not isinstance(item, dict):
                continue
            result, failure = _verify_component(item, pages, vocab, new_values)
            if failure:
                queued.append(_queue(code, failure["field"], failure["value"], failure["signal"],
                                     failure["detail"], "component", index))
            else:
                components.append(result)
    for index, item in enumerate(payload.get("rules") or []):
        if not isinstance(item, dict):
            continue
        result, failure = _verify_rule(item, pages, vocab, new_values)
        if failure:
            queued.append(_queue(code, failure["field"], failure["value"], failure["signal"],
                                 failure["detail"], "rule", index))
        elif result:
            rules.append(result)
    return components, rules, queued, effective


def _verify_component(item, pages, vocab, new_values):
    quote = item.get("source_quote")
    try:
        page_number = int(item.get("source_page"))
    except (TypeError, ValueError):
        page_number = None
    text = pages.get(page_number)
    if text is None or not quote_on_page(quote, text):
        return None, {"field": "source_quote", "value": str(quote), "signal": "quote_not_found",
                      "detail": f"source quote not on page {page_number}"}
    rate = _decimal(item.get("rate"))
    if rate is None or not rate_on_page(rate, text):
        return None, {"field": "rate", "value": str(item.get("rate")), "signal": "rate_not_on_page",
                      "detail": f"rate {item.get('rate')!r} is not a number printed on page {page_number}"}
    unit = item.get("unit")
    if unit not in vocab["unit"]:
        return None, {"field": "unit", "value": str(unit), "signal": "unit_not_in_vocabulary",
                      "detail": f"unit {unit!r} is not a stored unit"}
    component_type = str(item.get("component_type") or "").strip()
    if not component_type:
        return None, {"field": "component_type", "value": "", "signal": "model_output_unreadable",
                      "detail": "component_type is empty"}
    if component_type not in vocab["component_type"]:
        new_values["component_type"].add(component_type)
    variant = item.get("variant")
    variant = str(variant).strip() if variant else None
    return {
        "component_type": component_type,
        "billing_category": squash(item.get("printed_label") or "") or None,
        "rate": rate,
        "unit": unit,
        "variant": variant or None,
        "source_page": page_number,
        "source_cell": squash(quote),
    }, None


def _verify_rule(item, pages, vocab, new_values):
    quote = item.get("source_quote")
    try:
        page_number = int(item.get("source_page"))
    except (TypeError, ValueError):
        page_number = None
    text = pages.get(page_number)
    rule_type = str(item.get("rule_type") or "").strip()
    rule_text = str(item.get("rule_text") or "").strip()
    if text is None or not quote_on_page(quote, text):
        return None, {"field": "source_quote", "value": str(quote), "signal": "quote_not_found",
                      "detail": f"rule quote not on page {page_number}"}
    if not rule_type or not rule_text:
        return None, {"field": "rule_text", "value": rule_text, "signal": "model_output_unreadable",
                      "detail": "rule_type and rule_text are required"}
    if rule_type not in vocab["rule_type"]:
        new_values["rule_type"].add(rule_type)
    return (rule_type, rule_text), None


def _queue(schedule_code, field, value, signal, detail, bucket="section", index=None):
    suffix = "" if index is None else f":{index}"
    return {
        "schedule_code": schedule_code,
        "field": field,
        "value": value,
        "signal": signal,
        "detail": detail,
        "bucket": bucket,
        "source_ref": f"{schedule_code}:{bucket}:{field}:{signal}{suffix}",
    }


def _tally(items):
    counts = {}
    for item in items:
        counts[item["signal"]] = counts.get(item["signal"], 0) + 1
    return counts


def parse_book(source, target, client=None, cache_dir=None):
    """Write draft versions for one tariff book. Raises instead of writing partial rows.

    `client` is injectable (`complete(system, user) -> (text, stop_reason)`). With no client
    the Anthropic SDK is used, and a missing key or an API failure raises ExtractionUnavailable
    before any commit.
    """
    if __package__:
        from . import document_intake
    else:
        import document_intake

    source = Path(source)
    pages = read_pages(source)
    detection = document_intake.identify_document(source)
    if detection["doc_kind"] != document_intake.TARIFF or not detection["utility"] or not detection["state"]:
        raise BookRefused("the source is not an identified utility tariff")
    version_match = re.search(r"(\d+)", detection["version_label"] or "")
    if not version_match:
        for page in pages[:6]:
            found = re.search(r"TARIFF\s+NO\.?\s*(\d+)", page["text"], re.I)
            if found:
                version_match = found
                break
    if not version_match:
        raise BookRefused("the book prints no tariff or amendment number to store as its version")
    version_number = int(version_match.group(1))
    state, utility = detection["state"], detection["utility"]

    sections, unreadable = chunk_pages(pages)
    if not sections:
        raise BookRefused("the book contains no recognisable schedule sections")

    digest = content_hash(source)
    cache_file = _cache_path(cache_dir or CACHE_DIR, digest)
    cache = _load_cache(cache_file)
    with db.connect(target, readonly=True) as conn:
        known = vocabulary(conn)
    new_values = {"component_type": set(), "rule_type": set(), "unit": set()}
    prepared = []
    usage_fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    usage = {field: 0 for field in usage_fields}
    model_calls = 0
    declined = []
    prompt = _system_prompt(known)

    def extractor():
        nonlocal client
        if client is None:
            key = api_key()
            if not key:
                raise ExtractionUnavailable(NOT_CONFIGURED, "not_configured")
            client = AnthropicExtractor(key)
        return client

    for section in sections:
        code = section["schedule_code"]
        if not section["page_numbers"] or not any(readable(page["text"]) for page in section["pages"]):
            reason = (f"no readable page carries {section['designation']}; "
                      f"unreadable PDF pages: {unreadable or 'none'}")
            declined.append({"schedule_code": code, "reason": reason, "signal": "unreadable_pages"})
            prepared.append({"section": section, "skip": "unreadable", "queued": [
                _queue(code, "pages", reason, "unreadable_pages", reason)]})
            continue
        cache_key = f"{code}|{section['page_numbers'][0]}-{section['page_numbers'][-1]}"
        cached = cache["sections"].get(cache_key)
        if cached and cached.get("model") not in (None, MODEL):
            cached = None
        if cached:
            raw, stop = cached.get("raw", ""), cached.get("stop_reason")
            for field in usage_fields:
                usage[field] += int((cached.get("usage") or {}).get(field, 0) or 0)
        else:
            caller = extractor()
            before = dict(getattr(caller, "usage", {}) or {})
            raw, stop = caller.complete(prompt, _user_prompt(section))
            model_calls += 1
            after = getattr(caller, "usage", None) or {}
            delta = {field: max(0, int(after.get(field, 0) or 0) - int(before.get(field, 0) or 0))
                     for field in usage_fields}
            cache["sections"][cache_key] = {"raw": raw, "stop_reason": stop, "usage": delta, "model": MODEL}
            _save_cache(cache_file, cache)
            for field in usage_fields:
                usage[field] += delta[field]
        if stop not in ("end_turn", None):
            reason = f"model output stopped at {stop}"
            declined.append({"schedule_code": code, "reason": reason, "signal": "model_output_incomplete"})
            prepared.append({"section": section, "skip": "incomplete", "queued": [
                _queue(code, "response", stop or "", "model_output_incomplete", reason)]})
            continue
        try:
            payload = parse_model_json(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            reason = "model output was not the JSON object required"
            declined.append({"schedule_code": code, "reason": reason, "signal": "model_output_unreadable"})
            prepared.append({"section": section, "skip": "unreadable_output", "queued": [
                _queue(code, "response", "", "model_output_unreadable", reason)]})
            continue
        prepared.append({"section": section, "payload": payload, "cache_key": cache_key})

    with db.connect(target) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            created, existing, queued_items = [], [], []
            per_schedule = {}
            verified_components = verified_rules = 0
            for item in prepared:
                section = item["section"]
                code = section["schedule_code"]
                if "payload" not in item:
                    queued_items.extend(item["queued"])
                    per_schedule[code] = {"verified_components": 0, "verified_rules": 0,
                                          "queued": _tally(item["queued"]), "stored": False}
                    continue
                components, rules, queued, effective = _verify_payload(
                    item["payload"], section, known, new_values)
                queued_items.extend(queued)
                disposition = item["payload"].get("disposition")
                if effective is None or (disposition == "rates" and not components):
                    if effective is not None and disposition == "rates" and not components:
                        extra = _queue(code, "components", "0", "no_verified_rates",
                                       "no component survived verification; nothing was stored")
                        queued_items.append(extra)
                        queued = queued + [extra]
                        declined.append({"schedule_code": code, "reason": extra["detail"],
                                         "signal": "no_verified_rates"})
                    per_schedule[code] = {"verified_components": 0, "verified_rules": 0,
                                          "queued": _tally(queued), "stored": False,
                                          "pages": section["page_numbers"]}
                    continue
                identity = (state, utility, code, version_number, effective)
                found = conn.execute(
                    "SELECT id FROM tariff_versions WHERE state = ? AND utility = ? AND schedule_code = ? "
                    "AND version = ? AND effective_from = ? ORDER BY id DESC LIMIT 1", identity).fetchone()
                if found:
                    existing.append({"schedule_code": code, "version_id": found[0],
                                     "components": len(components), "rules": len(rules)})
                    per_schedule[code] = {"verified_components": len(components), "verified_rules": len(rules),
                                          "queued": _tally(queued), "stored": True, "existing": True,
                                          "pages": section["page_numbers"]}
                    verified_components += len(components)
                    verified_rules += len(rules)
                    continue
                cursor = conn.execute(
                    "INSERT INTO tariff_versions(state, utility, schedule_code, version, effective_from, status) "
                    "VALUES (?, ?, ?, ?, ?, 'draft')", identity)
                version_id = cursor.lastrowid
                seen = set()
                stored_components = 0
                for component in components:
                    key = (component["component_type"], component["billing_category"], str(component["rate"]),
                           component["unit"], component["variant"])
                    if key in seen:
                        continue
                    seen.add(key)
                    conn.execute(
                        "INSERT INTO tariff_components(tariff_version_id, component_type, billing_category, "
                        "rate, formula, unit, variant, source_page, source_cell) "
                        "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                        (version_id, component["component_type"], component["billing_category"],
                         float(component["rate"]), component["unit"], component["variant"],
                         component["source_page"], component["source_cell"]))
                    stored_components += 1
                seen_rules = set()
                stored_rules = 0
                for rule_type, rule_text in rules:
                    if (rule_type, rule_text) in seen_rules:
                        continue
                    seen_rules.add((rule_type, rule_text))
                    conn.execute(
                        "INSERT INTO tariff_rules(tariff_version_id, rule_type, rule_text) VALUES (?, ?, ?)",
                        (version_id, rule_type, rule_text))
                    stored_rules += 1
                created.append({"schedule_code": code, "version_id": version_id,
                                "components": stored_components, "rules": stored_rules})
                verified_components += stored_components
                verified_rules += stored_rules
                per_schedule[code] = {"verified_components": stored_components, "verified_rules": stored_rules,
                                      "queued": _tally(queued), "stored": True,
                                      "pages": section["page_numbers"]}
            for item in queued_items:
                parse_review.queue_item(
                    conn, SOURCE_KIND, item["source_ref"], item["schedule_code"], item["field"],
                    str(item["value"])[:500], item["signal"],
                    f"{utility} {item['schedule_code']}: {item['detail']}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "tariff_number": version_number,
        "state": state,
        "utility": utility,
        "model": getattr(client, "model", MODEL),
        "model_calls": model_calls,
        "usage": usage,
        "created": created,
        "existing": existing,
        "declined": declined,
        "queued": len(queued_items),
        "unreadable_pages": unreadable,
        "verified_components": verified_components,
        "verified_rules": verified_rules,
        "queued_components": _tally(i for i in queued_items if i["bucket"] == "component"),
        "queued_rules": _tally(i for i in queued_items if i["bucket"] == "rule"),
        "per_schedule": per_schedule,
        "new_vocabulary": {key: sorted(value) for key, value in new_values.items()},
        "sections_found": len(sections),
    }


