"""Source-derived, rerunnable backfill for stored tariff schedule kinds."""

import json
import re
from pathlib import Path

import pdfplumber

try:
    from src import parse_apco
except ImportError:
    import parse_apco


ROOT = Path(__file__).resolve().parent.parent
APCO_UTILITY = "Appalachian Power Company"
VEPGA_UTILITY = "Virginia Electric and Power Company"
UTILITIES = (APCO_UTILITY, VEPGA_UTILITY)
APCO_PDF = "APCo-Tariff-28.pdf"
VEPGA_PDF = "VEPGA-Amendment-12-Effective-July-1-2025.pdf"
VEPGA_GEN_PDF = "rider-gen.pdf"


def _canonical(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def _date_text(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _apco_source_index(source_dir):
    path = Path(source_dir) / APCO_PDF
    pages = parse_apco.read_pages(path)
    analysis = parse_apco.analyse_book(path)
    by_page = {}
    by_code = {}
    for sheet, group in analysis["groups"].items():
        if group["kind"] not in ("schedule", "rider", "exhibit"):
            continue
        title_page = group["pages"][0]
        heading = None
        for page_number in group["pages"]:
            parsed_heading = parse_apco.heading_of(pages[page_number - 1]["text"])
            if parsed_heading[0] in ("schedule", "rider", "exhibit"):
                title_page = page_number
                heading = parsed_heading
                break
        if heading is None:
            raise ValueError(f"APCo section {sheet} has no supported printed heading")
        heading_kind, designation, _ = heading
        role = "informational" if heading_kind == "exhibit" else heading_kind
        code = "EXHIBIT" if heading_kind == "exhibit" else parse_apco.schedule_code(designation)
        evidence = {
            "source": f"data/raw/{APCO_PDF}",
            "title_page": title_page,
            "pages": list(group["pages"]),
            "printed_heading": group["heading"],
            "printed_designation": designation,
            "tariff_book_sheets": list(group["sheets"]),
        }
        record = {"kind": role, "evidence": evidence, "section": str(sheet)}
        by_code.setdefault(_canonical(code), []).append(record)
        for page_number in group["pages"]:
            by_page[page_number] = record
    return by_page, by_code


def _vepga_source_index(source_dir):
    by_code = {}
    source_files = (VEPGA_PDF, VEPGA_GEN_PDF)
    for filename in source_files:
        path = Path(source_dir) / filename
        with pdfplumber.open(path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                lines = (page.extract_text() or "").splitlines()[:12]
                for line in lines:
                    line = line.strip()
                    if re.match(r"^FUEL CHARGE RIDER A\b", line, re.IGNORECASE):
                        code, role = "RIDER A", "rider"
                    else:
                        match = re.match(r"^(SCHEDULE|RIDER)\s+(.+?)\s*$", line, re.IGNORECASE)
                        if not match or match.group(2).lower().startswith("(continued"):
                            continue
                        role = "schedule" if match.group(1).upper() == "SCHEDULE" else "rider"
                        code = match.group(2).strip()
                        if role == "rider":
                            code = f"RIDER {code}"
                    key = _canonical(code)
                    evidence = {
                        "source": f"data/raw/{filename}",
                        "page": page_number,
                        "printed_heading": line,
                    }
                    current = by_code.get(key)
                    if current and current["kind"] != role:
                        raise ValueError(f"Conflicting VEPGA source roles for {code}")
                    if current is None:
                        by_code[key] = {"kind": role, "evidence": evidence}
    return by_code


def _version_rows(conn):
    return conn.execute(
        "SELECT id, utility, schedule_code, status, effective_from "
        "FROM tariff_versions WHERE utility IN (?, ?) "
        "ORDER BY utility, schedule_code, status, effective_from",
        UTILITIES,
    ).fetchall()


def _component_pages(conn):
    result = {}
    for version_id, source_page in conn.execute(
        "SELECT tariff_version_id, source_page FROM tariff_components "
        "WHERE source_page IS NOT NULL"
    ).fetchall():
        result.setdefault(version_id, set()).add(int(source_page))
    return result


def _apco_record(code, version_id, pages_by_version, page_index, code_index):
    cited_pages = sorted(pages_by_version.get(version_id, ()))
    if any(page not in page_index for page in cited_pages):
        missing = [page for page in cited_pages if page not in page_index]
        raise ValueError(f"APCo source pages {missing} do not map to a printed section")
    section_ids = {page_index[page]["section"] for page in cited_pages}
    if len(section_ids) > 1:
        raise ValueError(f"APCo schedule {code} cites multiple printed sections")
    if section_ids:
        section_id = next(iter(section_ids))
        record = next(
            page_index[page]
            for page in cited_pages
            if page_index[page]["section"] == section_id
        )
        evidence = dict(record["evidence"])
        evidence["component_citation_pages"] = cited_pages
        return record["kind"], evidence
    matches = code_index.get(_canonical(code), [])
    if len(matches) != 1:
        raise ValueError(f"APCo schedule {code} has no unique printed-heading evidence")
    record = matches[0]
    return record["kind"], dict(record["evidence"])


def derive_schedule_kind_records(conn, source_dir=None):
    """Derive kind and provenance from printed tariff headings, not database row IDs."""
    source_dir = Path(source_dir or ROOT / "data" / "raw")
    apco_by_page, apco_by_code = _apco_source_index(source_dir)
    vepga_by_code = _vepga_source_index(source_dir)
    pages_by_version = _component_pages(conn)
    records = {}
    for version_id, utility, code, status, effective_from in _version_rows(conn):
        key = (utility, code, status, _date_text(effective_from))
        if key in records:
            raise ValueError(f"Duplicate tariff version key: {key}")
        if utility == APCO_UTILITY:
            kind, evidence = _apco_record(
                code, version_id, pages_by_version, apco_by_page, apco_by_code
            )
        elif utility == VEPGA_UTILITY:
            source = vepga_by_code.get(_canonical(code))
            if source is None:
                raise ValueError(f"VEPGA schedule {code} has no printed-heading evidence")
            kind, evidence = source["kind"], dict(source["evidence"])
        else:
            raise ValueError(f"Unsupported utility for schedule-kind backfill: {utility}")
        records[key] = {
            "id": version_id,
            "kind": kind,
            "evidence": json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        }
    return records


def ensure_schedule_kind_schema(conn):
    """Add only the schedule-kind columns, using the project's db connection."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tariff_versions)").fetchall()}
    if "schedule_kind" not in columns:
        conn.execute(
            "ALTER TABLE tariff_versions ADD COLUMN schedule_kind TEXT "
            "CHECK (schedule_kind IN ('schedule', 'rider', 'informational'))"
        )
    if "schedule_kind_evidence" not in columns:
        conn.execute("ALTER TABLE tariff_versions ADD COLUMN schedule_kind_evidence TEXT")


def backfill_schedule_kinds(conn, reference_conn, source_dir=None):
    """Write source-derived kinds after exact-key and scratch-kind validation."""
    reference = {}
    for utility, code, status, effective_from, kind in reference_conn.execute(
        "SELECT utility, schedule_code, status, effective_from, schedule_kind "
        "FROM tariff_versions WHERE utility IN (?, ?)",
        UTILITIES,
    ).fetchall():
        key = (utility, code, status, _date_text(effective_from))
        if key in reference:
            raise ValueError(f"Duplicate reference tariff version key: {key}")
        reference[key] = kind

    derived = derive_schedule_kind_records(conn, source_dir)
    if set(derived) != set(reference):
        raise ValueError(
            "Primary/reference version-key mismatch: "
            f"missing={sorted(set(reference) - set(derived))}, "
            f"extra={sorted(set(derived) - set(reference))}"
        )
    mismatches = [
        (key, reference[key], derived[key]["kind"])
        for key in sorted(reference)
        if reference[key] is not None and reference[key] != derived[key]["kind"]
    ]
    if mismatches:
        raise ValueError(f"Source-derived kinds disagree with scratch: {mismatches}")

    for key in sorted(derived):
        record = derived[key]
        kind = reference[key] if reference[key] is None else record["kind"]
        conn.execute(
            "UPDATE tariff_versions SET schedule_kind = ?, schedule_kind_evidence = ? "
            "WHERE id = ?",
            (kind, record["evidence"], record["id"]),
        )

    stored = {}
    for utility, code, status, effective_from, kind, evidence in conn.execute(
        "SELECT utility, schedule_code, status, effective_from, "
        "schedule_kind, schedule_kind_evidence FROM tariff_versions "
        "WHERE utility IN (?, ?)",
        UTILITIES,
    ).fetchall():
        stored[(utility, code, status, _date_text(effective_from))] = (kind, evidence)
    for key, record in derived.items():
        expected_kind = reference[key] if reference[key] is None else record["kind"]
        if stored.get(key) != (expected_kind, record["evidence"]):
            raise ValueError(f"Stored schedule kind/evidence failed fidelity check: {key}")
    return derived
