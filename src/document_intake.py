"""Document intake (Phase 11A): save -> identify -> route.

Any uploaded PDF is stored content-addressed, identified from native PDF text (no OCR) by
document identity (utility names, tariff-book headings, bill markers; never rate values),
and routed through a utility -> adapter registry. A utility with no registered adapter is
saved and reported as unsupported; nothing is ever parsed or stamped under another
utility's identity, and an unsupported or unidentified document creates zero version rows.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pdfplumber

try:
    from src import db
    from src import parse_rider as dominion_rider
    from src import parse_rider_gen
    from src.parser_contract import rider_adapter
except ImportError:
    import db
    import parse_rider as dominion_rider
    import parse_rider_gen
    from parser_contract import rider_adapter

ROOT = Path(__file__).resolve().parents[1]
SCAN_PAGES = 6

TARIFF = "tariff"
BILL = "bill"
UNKNOWN = "unknown"

PARSED = "parsed"
INGESTED = "ingested"
SAVED_UNSUPPORTED = "saved_unsupported"
SAVED_UNIDENTIFIED = "saved_unidentified"

# A commission-form book number: "P.S.C. of D.C. No. 1" (the regulator's number for the book).
COMMISSION_BOOK_NUMBER = re.compile(
    r"\bP\.\s?S\.\s?C\.?\s+of\s+((?:[A-Z][A-Za-z]*\.?\s*){1,4}?)No\.?\s*(\d+)")

# Document-identity probes: headings and markers only.
TARIFF_PROBES = (
    ("tariff book number", re.compile(r"\bTARIFF\s+NO\.?\s*\d+", re.I)),
    ("commission tariff book number", COMMISSION_BOOK_NUMBER),
    ("standard rate schedules heading", re.compile(r"STANDARD\s+RATE\s+SCHEDULES", re.I)),
    ("tariff sheet marker", re.compile(r"\bSheet No\.\s*\d")),
    ("amendment to electric service agreement",
     re.compile(r"AGREEMENT\s+FOR\s+THE\s+PROVISION\s+OF\s+ELECTRIC\s+SERVICE", re.I)),
    ("rider heading", re.compile(r"^\s*RIDER\s+[A-Z0-9-]+\s*$", re.M)),
)
BILL_PROBES = (
    ("account profile heading", re.compile(r"Electric\s+Account\s+Profile", re.I)),
    ("bill determinants block", re.compile(r"Bill\s+Determinants", re.I)),
    ("billed rate row", re.compile(r"Billed Rate\b", re.I)),
    ("account number marker", re.compile(r"Account Number:|ACCOUNT NO\.", re.I)),
    ("billing days row", re.compile(r"Billing Days\b", re.I)),
)

# Utilities this system can recognise (identity only). `state_probes` map a state to
# identity text found in the document.
UTILITIES = {
    "dominion_va": {
        "name": "Dominion Energy Virginia",
        "short_name": "Dominion",
        "legal_name": "Virginia Electric and Power Company",
        "name_probes": (
            re.compile(r"VIRGINIA\s+ELECTRIC\s+AND\s+POWER\s+COMPANY", re.I),
            re.compile(r"Dominion Energy"),
        ),
        "state_probes": (
            ("VA", re.compile(r"COMMONWEALTH OF VIRGINIA|VEPGA|\bVA\s+\d{5}|Virginia Electric", re.I)),
        ),
    },
    "appalachian_va": {
        "name": "Appalachian Power Company",
        "legal_name": "Appalachian Power Company",
        "name_probes": (re.compile(r"APPALACHIAN\s+POWER\s+COMPANY", re.I),),
        "state_probes": (("VA", re.compile(r"VIRGINIA\s+S\.C\.C\.|VA\.\s*S\.C\.C\.", re.I)),),
    },
    # Identified by the name the book prints. No fixed state: the book's own printed state
    # (generic state-name detection) decides, since the company files tariffs in more than one.
    "pepco_dc": {
        "name": "Pepco",
        "legal_name": "Pepco",
        "name_probes": (re.compile(r"\bPepco\b"),),
        "state_probes": (),
    },
}

VERSION_PROBES = (
    ("amendment", re.compile(r"AMENDMENT\s+NO\.?\s*(\d+)", re.I)),
    ("tariff_no", re.compile(r"TARIFF\s+NO\.?\s*(\d+)", re.I)),
    ("commission_no", COMMISSION_BOOK_NUMBER),
)
EFFECTIVE_PROBES = (
    # "Date Effective: Usage on and after October 1, 2026" — a few words between the label and the date.
    re.compile(r"Effective:?\s+(?:[A-Za-z]+\s+){0,4}?(?:on\s+and\s+after|on\s+or\s+after)\s+"
               r"([A-Z][a-z]+\s+\d{1,2},\s*\d{4})"),
    re.compile(r"Effective:?\s+([A-Z][a-z]+\s+\d{1,2},\s*\d{4})"),
    re.compile(r"as of\s+([A-Z][a-z]+\s+\d{1,2},\s*\d{4})\s*\(the", re.I),
)
RIDER_DESIGNATION_PROBES = (
    ("rider_gen", re.compile(r"RIDER\s+GEN(?:\s|,|$)", re.I)),
)
MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), 1)}


class IntakeError(ValueError):
    """The upload cannot be taken in."""


class AdapterRefused(IntakeError):
    """An adapter refused the document; carries the message and failure list for the API."""

    def __init__(self, message, failures=None):
        super().__init__(message)
        self.message = message
        self.failures = failures or []


# ---------------------------------------------------------------- plain words
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_STATE_PATTERNS = sorted(
    ((abbr, re.compile(r"\b" + r"\s+".join(name.split()) + r"\b", re.I)) for abbr, name in STATE_NAMES.items()),
    key=lambda item: -len(STATE_NAMES[item[0]]),
)
RAW_STATUS_NOTES = {
    PARSED: "parsed",
    INGESTED: "ingested",
    SAVED_UNSUPPORTED: "saved_unsupported",
    SAVED_UNIDENTIFIED: "saved_unidentified",
}


def company_label(detection):
    """'Dominion (Virginia Electric and Power Company)', or None when no company was recognised."""
    entry = UTILITIES.get(detection.get("utility_id"))
    if entry is None:
        return None
    if entry.get("short_name"):
        return f"{entry['short_name']} ({entry['legal_name']})"
    return entry["name"]


def kind_label(detection, kind=None):
    doc_kind = detection.get("doc_kind")
    if doc_kind == BILL:
        return "bill"
    if doc_kind == TARIFF:
        if detection.get("rider_designation") or kind == "rider":
            return "rider"
        return "tariff schedule"
    return "other"


def plain_words(detection, status, *, kind=None, result=None):
    """What the person uploading sees: company, state, document kind, and the outcome as a sentence.

    `status` is a routing status (parsed/ingested/saved_*), 'draft'/'active' (a stored version's
    state), 'bill_loaded', or 'refused_bill' / 'refused_tariff' (wrong endpoint for the document).
    The raw status stays alongside as secondary detail.
    """
    result = result or {}
    company = company_label(detection)
    state = STATE_NAMES.get(detection.get("state"), detection.get("state"))
    kind_text = kind_label(detection, kind)
    where = f"{company}, {state}" if company and state else company
    if status == "refused_bill":
        outcome = "This looks like a bill, not a tariff document — nothing was loaded as rates."
    elif status == "refused_tariff":
        outcome = "This looks like a tariff document, not a bill — nothing was loaded."
    elif status == "bill_loaded":
        outcome = (f"Loaded as a bill{' for ' + company if company else ''} — "
                   "usage history is now stored and usable for audits.")
    elif status == "unchanged":
        outcome = f"This {kind_text} was already loaded — nothing was changed."
    elif status in ("draft", PARSED):
        outcome = f"Loaded as a {company + ' ' if company else ''}{kind_text}"
        if status == "draft":
            outcome += " — rates are stored as a validated draft; activate it before audits can use it."
        else:
            outcome += " — rates are now stored and usable for audits."
    elif status == INGESTED:
        created, existing = result.get("versions_created", 0), result.get("versions_existing", 0)
        live, held = len(result.get("activated", [])), len(result.get("stayed_draft", []))
        queued = result.get("queued_for_review")
        queue_text = f"; {queued} items are queued for review" if queued else ""
        if created == 0:
            outcome = (f"The {where} tariff book was already stored — all {existing} schedule versions "
                       f"matched, nothing new was created{queue_text}.")
        else:
            outcome = (f"Loaded the {where} tariff book — {created} new schedule versions created "
                       f"({live} went live, {held} stayed as drafts), {existing} already stored{queue_text}.")
    elif status == SAVED_UNSUPPORTED and company:
        outcome = (f"Saved under {where}, but this document type isn't readable yet — "
                   "nothing was guessed or stored as rates.")
    else:
        outcome = "Couldn't identify this document — it was saved for review and nothing was changed."
    return {
        "company": company or "company not recognized",
        "state": state,
        "kind": kind_text,
        "outcome": outcome,
        "status": RAW_STATUS_NOTES.get(status, status),
        "detail": result.get("message"),
    }


# ---------------------------------------------------------------- save
def content_path(sha256, upload_dir):
    return Path(upload_dir) / f"{sha256}.pdf"


def save_upload(data, original_filename, upload_dir):
    """Store bytes content-addressed by SHA-256; identical bytes reuse the existing file."""
    sha256 = hashlib.sha256(data).hexdigest()
    path = content_path(sha256, upload_dir)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return path, sha256, len(data)


def record_intake(path, original_filename, detection, status, plain=None):
    """Sidecar next to the stored file: what it was identified as and how it was routed."""
    sidecar = Path(path).with_suffix(".intake.json")
    sidecar.write_text(
        json.dumps(
            {
                "original_filename": Path(original_filename or "").name,
                "status": status,
                "doc_kind": detection["doc_kind"],
                "utility": detection["utility"],
                "state": detection["state"],
                "version_label": detection["version_label"],
                "effective_date": detection["effective_date"],
                "plain": plain,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return sidecar


def list_intakes(upload_dir):
    """Past uploads, newest first, each with the same plain-words fields the upload result shows."""
    rows = []
    for sidecar in Path(upload_dir).glob("*.intake.json"):
        try:
            info = json.loads(sidecar.read_text(encoding="utf-8"))
        except ValueError:
            continue
        plain = info.get("plain")
        if plain is None:
            detection = {"doc_kind": info.get("doc_kind"), "utility_id": next(
                (k for k, v in UTILITIES.items() if v["name"] == info.get("utility")), None),
                "state": info.get("state"), "rider_designation": None}
            plain = plain_words(detection, info.get("status"))
        rows.append({"file": info.get("original_filename"), "uploaded_at": sidecar.stat().st_mtime, **plain})
    return sorted(rows, key=lambda row: row["uploaded_at"], reverse=True)


# ---------------------------------------------------------------- identify
def _iso_date(text):
    match = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text.strip())
    month = MONTHS.get(match.group(1).lower()) if match else None
    if not month:
        return None
    return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(2)):02d}"


def state_from_printed_names(pages):
    """The state a book prints most often by full name (all states plus the District of Columbia).

    Longer names are consumed first so "West Virginia" is not also counted as "Virginia".
    Returns (abbreviation, page, text), or None when no state is named or the top two tie.
    """
    counts, first = {}, {}
    for number, text in pages:
        flat = " ".join(text.split())
        for abbr, pattern in _STATE_PATTERNS:
            for match in pattern.finditer(flat):
                counts[abbr] = counts.get(abbr, 0) + 1
                first.setdefault(abbr, (number, match.group(0)))
            flat = pattern.sub(" ", flat)
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda item: -item[1])
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    abbr = ranked[0][0]
    return abbr, first[abbr][0], first[abbr][1]


def _first_hits(probes, pages):
    hits = []
    for label, pattern in probes:
        for number, text in pages:
            match = pattern.search(text)
            if match:
                hits.append({"probe": label, "page": number, "text": " ".join(match.group(0).split())})
                break
    return hits


def identify_document(path):
    """Detection record for a PDF: doc_kind, utility, state, version info, evidence, reasons."""
    record = {
        "doc_kind": UNKNOWN,
        "utility": None,
        "utility_id": None,
        "state": None,
        "version_label": None,
        "effective_date": None,
        "rider_designation": None,
        "evidence": [],
        "reasons": [],
        "pages_scanned": 0,
        "total_pages": None,
    }
    try:
        with pdfplumber.open(path) as pdf:
            record["total_pages"] = len(pdf.pages)
            pages = [
                (index, page.extract_text() or "")
                for index, page in enumerate(pdf.pages[:SCAN_PAGES], 1)
            ]
    except Exception as error:  # unreadable or not a PDF
        record["reasons"].append(f"unreadable PDF: {type(error).__name__}: {error}")
        return record
    record["pages_scanned"] = len(pages)
    if not any(text.strip() for _, text in pages):
        record["reasons"].append(
            f"no extractable text in the first {SCAN_PAGES} pages (scanned documents need OCR, which is not attempted)"
        )
        return record

    tariff_hits = _first_hits(TARIFF_PROBES, pages)
    bill_hits = _first_hits(BILL_PROBES, pages)
    if bill_hits and len(bill_hits) >= 2 and len(bill_hits) > len(tariff_hits):
        record["doc_kind"] = BILL
        record["evidence"] += bill_hits
    elif tariff_hits and len(tariff_hits) >= len(bill_hits):
        record["doc_kind"] = TARIFF
        record["evidence"] += tariff_hits
    else:
        record["reasons"].append(
            f"no tariff-book heading and fewer than two bill markers in the first {SCAN_PAGES} pages "
            f"(tariff markers: {len(tariff_hits)}, bill markers: {len(bill_hits)})"
        )

    for utility_id, entry in UTILITIES.items():
        name_hits = []
        for pattern in entry["name_probes"]:
            for number, text in pages:
                match = pattern.search(text)
                if match:
                    name_hits.append({"probe": f"utility name ({entry['name']})", "page": number,
                                      "text": " ".join(match.group(0).split())})
                    break
        if name_hits:
            record["utility"], record["utility_id"] = entry["name"], utility_id
            record["evidence"] += name_hits[:1]
            for state, pattern in entry["state_probes"]:
                for number, text in pages:
                    match = pattern.search(text)
                    if match:
                        record["state"] = state
                        record["evidence"].append({"probe": f"state ({state})", "page": number,
                                                   "text": " ".join(match.group(0).split())})
                        break
                if record["state"]:
                    break
            if record["state"] is None:
                printed = state_from_printed_names(pages)
                if printed:
                    record["state"] = printed[0]
                    record["evidence"].append({"probe": f"state ({printed[0]}) from printed name",
                                               "page": printed[1], "text": printed[2]})
            break
    if record["utility"] is None and record["doc_kind"] != UNKNOWN:
        record["reasons"].append("utility not recognised from the document's identity text")
    elif record["utility"] and record["state"] is None:
        record["reasons"].append(f"state not established for {record['utility']}")

    if record["doc_kind"] == TARIFF:
        joined = [(number, " ".join(text.split())) for number, text in pages]
        for label, pattern in VERSION_PROBES:
            for number, text in joined:
                match = pattern.search(text)
                if match:
                    if label == "amendment":
                        record["version_label"] = f"Amendment No. {match.group(1)}"
                    elif label == "commission_no":
                        record["version_label"] = f"P.S.C. of {match.group(1).strip()} No. {match.group(2)}"
                    else:
                        record["version_label"] = f"Tariff No. {match.group(1)}"
                    record["evidence"].append({"probe": "version", "page": number, "text": match.group(0)})
                    break
            if record["version_label"]:
                break
        for pattern in EFFECTIVE_PROBES:
            for number, text in joined:
                match = pattern.search(text)
                if match and _iso_date(match.group(1)):
                    record["effective_date"] = _iso_date(match.group(1))
                    record["evidence"].append({"probe": "effective date", "page": number,
                                               "text": " ".join(match.group(0).split())})
                    break
            if record["effective_date"]:
                break
        
        for label, pattern in RIDER_DESIGNATION_PROBES:
            for number, text in joined:
                match = pattern.search(text)
                if match:
                    # For GEN rider: verify "GENERATION FACILITIES PROJECTS" also appears on the same page
                    if label == "rider_gen":
                        if "GENERATION FACILITIES PROJECTS" not in text:
                            continue
                    record["rider_designation"] = label.replace("rider_", "").upper()
                    record["evidence"].append({"probe": "rider designation", "page": number,
                                               "text": " ".join(match.group(0).split())})
                    break
            if record["rider_designation"]:
                break
    return record


# ---------------------------------------------------------------- adapters
def gen_rider_adapter(kind=None, code=None):
    """GEN rider adapter: routes GEN-designated rider sheets to parse_rider_gen."""
    def parse(source, target):
        try:
            return parse_rider_gen.parse_rider_gen(pdf_path=source, database_path=target)
        except Exception as error:
            raise AdapterRefused("The GEN rider could not be parsed.", [f"{type(error).__name__}: {error}"])
    return parse


def dominion_adapter(kind, code):
    """The existing Amendment 12 parsers as a contract-shaped adapter: parse_fn(source, target)."""
    if kind not in ("schedule", "rider"):
        raise IntakeError("kind must be 'schedule' or 'rider'.")
    if not code:
        raise IntakeError("a schedule or rider code is required for this utility.")

    def parse(source, target):
        if kind == "rider":
            try:
                return rider_adapter(code)(source, target)
            except Exception as error:
                raise AdapterRefused("The rider could not be parsed.", [f"{type(error).__name__}: {error}"])
        env = {k: v for k, v in os.environ.items() if k not in ("TARIFF_DB_URL", "TARIFF_DB_PATH")}
        env["TARIFF_DB_URL" if db.is_postgres(target) else "TARIFF_DB_PATH"] = str(target)
        result = subprocess.run(
            [sys.executable, str(ROOT / "src" / "parse_schedule.py"), code, "--pdf", str(source)],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            lines = (result.stdout + result.stderr).strip().splitlines()
            raise AdapterRefused("The schedule could not be parsed.", lines[-5:])
        with db.connect(target, readonly=True) as conn:
            found = conn.execute(
                "SELECT id FROM tariff_versions WHERE schedule_code = ? AND utility = ? ORDER BY id DESC",
                (code, dominion_rider.UTILITY),
            ).fetchone()
        if not found:
            raise AdapterRefused("The parser wrote no version.", [f"No version for {code}."])
        return found[0], None

    return parse


def _dominion_accepts(detection, kind=None):
    """The Dominion parsers are built for one amendment; refuse to stamp any other.

    A schedule book must show Amendment 12 and its effective date. A single rider sheet often prints
    neither, so it is refused only when it explicitly shows a different amendment or date.
    """
    expected = f"Amendment No. {dominion_rider.RIDER_VERSION}"
    if kind == "rider":
        label, date = detection["version_label"], detection["effective_date"]
        if (label and label != expected) or (date and date != dominion_rider.EFFECTIVE_FROM):
            return (
                f"the Dominion rider parser is built for {expected} effective {dominion_rider.EFFECTIVE_FROM}; "
                f"this document shows {label or 'no amendment number'} / {date or 'no effective date'}"
            )
        return None
    if detection["version_label"] != expected or detection["effective_date"] != dominion_rider.EFFECTIVE_FROM:
        return (
            f"the Dominion parsers are built for {expected} effective {dominion_rider.EFFECTIVE_FROM}; "
            f"this document shows {detection['version_label'] or 'no amendment number'}"
            f" / {detection['effective_date'] or 'no effective date'}"
        )
    return None


def apco_book_adapter(kind=None, code=None):
    """Whole-book adapter: no kind or code. Parses drafts, then activates the ones that validate."""
    if __package__:
        from . import parse_apco, parse_review, activate_version as activation, validate_draft
    else:
        import parse_apco, parse_review, validate_draft
        import activate_version as activation

    def parse(source, target):
        try:
            summary = parse_apco.parse_book(source, target)
        except parse_apco.BookRefused as error:
            raise AdapterRefused("The tariff book could not be read.", [str(error)])
        activated, drafts = [], []
        with db.connect(target) as conn:
            for item in summary["created"]:
                failures = validate_draft.validate_drafts(conn, item["version_id"])
                if failures:
                    for failure in failures:
                        parse_review.queue_item(
                            conn, "tariff_book_section", f"version {item['version_id']}", item["schedule_code"],
                            "validation", failure[:200], "validation_failed",
                            f"{parse_apco.UTILITY} {item['schedule_code']} stays a draft: {failure}")
                    conn.commit()
                    drafts.append({**item, "problems": failures})
                    continue
                activation.activate_with_alert(conn, item["version_id"])
                activated.append(item)
        summary["activated"], summary["draft"] = activated, drafts
        return summary

    return parse


def _apco_accepts(detection, kind=None):
    return None


def ai_book_adapter(kind=None, code=None, client=None):
    """Every detected tariff that has no hand-written adapter, run by the packet coordinator.

    The coordinator reads only cached readings (it has no model client), validates the rates and
    rules sides separately, and activates a schedule only when both sides pass.
    """
    if __package__:
        from . import ai_extract, packet_pipeline
    else:
        import ai_extract, packet_pipeline

    def parse(source, target):
        try:
            run = packet_pipeline.run_book(source, target, client=client)
        except packet_pipeline.ReadingMissing as error:
            raise ai_extract.ExtractionUnavailable(f"{ai_extract.NOT_CONFIGURED} ({error})", "not_configured")
        except ai_extract.BookRefused as error:
            raise AdapterRefused("The tariff book could not be read.", [str(error)])
        created, existing, activated, drafts, declined = [], [], [], [], []
        with db.connect(target) as conn:
            for row in run["table"]:
                outcome = row["outcome"]
                if outcome.startswith("existing version"):
                    existing.append({"schedule_code": row["schedule"], "version_id": int(outcome.split()[2].rstrip(";"))})
                elif outcome.startswith("not stored"):
                    declined.append({"schedule_code": row["schedule"], "reason": outcome,
                                     "signal": (row["signals"] or [None])[0]})
                else:
                    item = {"schedule_code": row["schedule"], "version_id": row["version_id"],
                            "components": row["components"], "rules": row["rules_stored"]}
                    created.append(item)
                    if outcome == "activated":
                        activated.append(item)
                    else:
                        problems = [r[0] for r in conn.execute(
                            "SELECT detail FROM packet_gates WHERE tariff_version_id = ? AND passed = 0",
                            (row["version_id"],)).fetchall()]
                        drafts.append({**item, "problems": problems})
        return {
            "tariff_number": run["tariff_number"], "state": run["state"], "utility": run["utility"],
            "created": created, "existing": existing, "activated": activated, "draft": drafts,
            "declined": declined, "queued": sum(r["queued"] for r in run["table"]),
            "unreadable_pages": run["unreadable_pages"], "model_calls": run["model_calls"],
            "structured_calls": run["structured_calls"], "structured": run["structured"],
            "packet_table": run["table"],
        }

    return parse


AI_BOOK = {"factory": ai_book_adapter, "accepts": _apco_accepts, "book": True}
ADAPTERS = {
    "dominion_va": {"factory": dominion_adapter, "accepts": _dominion_accepts},
    # Hand-written Dominion wins. Every other detected tariff uses the AI adapter.
    "appalachian_va": AI_BOOK,
}


# ---------------------------------------------------------------- route
def route_tariff(path, detection, *, target=None, kind=None, code=None, adapters=None):
    """Route a detected tariff to its utility's adapter, or say plainly that none exists.

    A hand-written adapter wins where one is registered (Dominion). With the default registry,
    every other detected utility tariff uses the AI adapter. GEN-designated rider sheets route
    to the GEN parser regardless of other conditions.
    """
    using_default = adapters is None
    adapters = ADAPTERS if adapters is None else adapters
    if detection["doc_kind"] == BILL:
        raise IntakeError("this document was identified as a bill, not a tariff.")
    
    if detection["doc_kind"] == TARIFF and detection["rider_designation"] == "GEN":
        parse_fn = gen_rider_adapter()
        try:
            version_id, count = parse_fn(path, target)
            return {"status": PARSED, "version_id": version_id, "components": count}
        except AdapterRefused as error:
            return {
                "status": SAVED_UNSUPPORTED,
                "versions_created": 0,
                "message": f"The file is saved and identified as a {detection['utility']} GEN rider, but {error.message}; "
                           "no version was created.",
            }
    
    if detection["doc_kind"] != TARIFF or detection["utility_id"] is None:
        return {
            "status": SAVED_UNIDENTIFIED,
            "versions_created": 0,
            "message": "The file is saved, but it could not be identified as a known utility's tariff; "
                       "nothing was parsed and no version was created. " + "; ".join(detection["reasons"]),
        }
    entry = adapters.get(detection["utility_id"])
    if entry is None and using_default and detection["utility_id"] not in (None, "dominion_va"):
        entry = AI_BOOK
    if entry is None:
        return {
            "status": SAVED_UNSUPPORTED,
            "versions_created": 0,
            "message": f"The file is saved and identified as a {detection['utility']} tariff"
                       f"{' (' + detection['version_label'] + ')' if detection['version_label'] else ''}, "
                       "but this utility's parser is not built yet; no version was created.",
        }
    unsupported = entry["accepts"](detection, kind)
    if unsupported:
        return {
            "status": SAVED_UNSUPPORTED,
            "versions_created": 0,
            "message": f"The file is saved and identified as a {detection['utility']} tariff, but {unsupported}; "
                       "no version was created.",
        }
    parse_fn = entry["factory"](kind, code)
    if entry.get("book"):
        try:
            summary = parse_fn(path, target)
        except Exception as error:
            if __package__:
                from .ai_extract import ExtractionUnavailable
            else:
                from ai_extract import ExtractionUnavailable
            if isinstance(error, ExtractionUnavailable):
                return {
                    "status": SAVED_UNSUPPORTED,
                    "versions_created": 0,
                    "message": f"The file is saved and identified as a {detection['utility']} tariff, "
                               f"but {error}; no version was created.",
                }
            raise
        return {
            "status": INGESTED,
            "tariff_number": summary["tariff_number"],
            "versions_created": len(summary["created"]),
            "versions_existing": len(summary["existing"]),
            "activated": [{"schedule_code": i["schedule_code"], "version_id": i["version_id"],
                           "components": i["components"], "rules": i["rules"]} for i in summary["activated"]],
            "stayed_draft": [{"schedule_code": i["schedule_code"], "version_id": i["version_id"],
                              "problems": i["problems"]} for i in summary["draft"]],
            "queued_for_review": summary["queued"],
            "declined": summary["declined"],
            "unreadable_pages": summary["unreadable_pages"],
            **({
                "extraction": {
                    "model": summary["model"],
                    "model_calls": summary["model_calls"],
                    "usage": summary["usage"],
                    "sections_found": summary["sections_found"],
                    "verified_components": summary["verified_components"],
                    "verified_rules": summary["verified_rules"],
                    "queued_components": summary["queued_components"],
                    "queued_rules": summary["queued_rules"],
                    "per_schedule": summary["per_schedule"],
                    "new_vocabulary": summary["new_vocabulary"],
                },
            } if "model" in summary else {}),
        }
    version_id, count = parse_fn(path, target)
    return {"status": PARSED, "version_id": version_id, "components": count}
