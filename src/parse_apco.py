"""Appalachian Power (Virginia) tariff-book adapter (Stage 11B).

`parse_book(source, target)` reads a whole APCo Virginia standard-service tariff book with no
manual inputs and writes DRAFT versions (one per schedule), their cited components and their
rules. It is a contract adapter (`src/parser_contract.py`): drafts only, every component cited,
idempotent, and a foreign or unreadable source is refused without writing anything.

Everything is read from the book: the tariff number (the stored `version`), each sheet's printed
effective date, the schedule designations and every rate. Nothing is assumed. A schedule whose
layout cannot be read with its printed cross-checks is NOT forced: it is queued in the parse-review
queue with the reason and no version is written for it.

Rate rows are read by position: the numbers under the printed "Generation", "Distribution" and
"Total" column headings. Cents per kWh are converted to dollars with Decimal. Every parsed row is
cross-checked (Generation + Distribution == Total, exactly as printed).
"""

import argparse
import functools
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys

import pdfplumber

try:
    from src import db
except ImportError:
    import db

if __package__:
    from . import parse_review
else:
    import parse_review

ROOT = Path(__file__).resolve().parent.parent
UTILITY = "Appalachian Power Company"
STATE = "VA"
SOURCE_KIND = "tariff_book_section"
SIGNAL_UNPARSED = "unparsed_schedule"
SIGNAL_UNREADABLE = "unreadable_pages"

MIN_READABLE_CHARS = 40
MAX_CONTROL_RATIO = 0.05
MAX_CID_MARKERS = 5
LINE_TOLERANCE = 2.5  # points: words closer than this in `top` share a text line
COLUMN_TOLERANCE = 30.0  # points: a number belongs to the nearest column heading within this
MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}

NUMBER = re.compile(r"^\$?-?\d[\d,]*(?:\.\d+)?$")
PLACEHOLDER = re.compile(r"^[—–-]$")
SHEET = re.compile(r"Sheet(?:\s+No\.)?\s+(NBP[\s-]*\d+(?:-\d+)?|\d+[A-Z]?(?:-\d+)?)")
EFFECTIVE = re.compile(r"Effective:?\s*([A-Za-z]+)\.?\s+(\d{1,2}),?\s*(\d{4})")
TARIFF_NO = re.compile(r"TARIFF\s+NO\.\s*(\d+)")
FOOTER = re.compile(r"^(Issued:|Pursuant to|Dated:|Case[: ]|ACCEPTED FOR FILING|PUBLIC UTILITY REGULATION)")
HEADING = re.compile(r"^([A-Z][A-Z ,&\-/’']{3,}?)(?:\s*\(.*\))?:?$")
UNIT = re.compile(r"\(\s*(¢/kWh|\$/kWh|\$/kW|\$/kVA|\$)\s*\)")
UNITS = {
    "¢/kWh": ("usd_per_kwh", Decimal("0.01")),
    "$/kWh": ("usd_per_kwh", Decimal(1)),
    "$/kW": ("usd_per_kw", Decimal(1)),
    "$/kVA": ("usd_per_kw", Decimal(1)),
    "$": ("usd_per_month", Decimal(1)),
}
VOLTAGE = re.compile(
    r"^(?:(\d{2,3})\s+)?(Secondary|Primary|Subtransmission|Transmission)\s+Voltage\s*[:(]?\s*(\d{2,3})?\)?:?\s*$")
TITLE_LINE = re.compile(
    r"^(APPALACHIAN POWER|(?:V\.?A\. )?S\.C\.C\.|SCHEDULE\b|OPTIONAL RIDER|RIDER\b|Sheet\b|"
    r"(?:First|Second|Third|Fourth|Fifth|Sixth)\s+Revis|\(.*\)$)")
RATE_START = re.compile(r"^(MONTHLY RATE|RATE)\b", re.I)
CODES = re.compile(r"\(Schedule Codes?\s+([^)]*)\)")
REACTIVE = re.compile(r"Reactive Demand Charge.*?\$\s*(\d+(?:\.\d+)?)\s+per\s+KVAR", re.I)
BLOCK = re.compile(r"\bBlock\s+(\d+)\b")
# Single-rate prose riders: (component suffix, pattern, unit, scale, cited phrase)
PROSE_RATES = (
    ("fuel_factor", re.compile(r"(\d+(?:\.\d+)?)\s*¢\s*per\s+kilowatt-hour"), "usd_per_kwh", Decimal("0.01")),
    ("sales_use_tax_surcharge", re.compile(r"surcharge of\s+(\d+(?:\.\d+)?)\s*¢/kWh"), "usd_per_kwh", Decimal("0.01")),
    ("rate_reduction_credit", re.compile(r"\(PRF\(G\)\)\s*=\s*(-?\d+(?:\.\d+)?)\s*%"), "percent", Decimal(1)),
)
RULE_HEADINGS = (
    ("availability", re.compile(r"^AVAILABILITY|^APPLICABILITY")),
    ("minimum_charge", re.compile(r"^MINIMUM (?:CHARGES?|MONTHLY|BILL)")),
    ("demand_determination", re.compile(r"MEASUREMENT AND DETERMINATION|BILLING DEMAND|DETERMINATION OF")),
)
TOU = re.compile(r"(on-peak|off-peak|critical on-peak)[^.]*\b(defined|consists)\b", re.I)


class BookRefused(ValueError):
    """The source is not an APCo Virginia tariff book we can read."""


# ---------------------------------------------------------------- reading
@functools.lru_cache(maxsize=2)
def _load(path, mtime, size):
    pages = []
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            pages.append({
                "n": number,
                "text": page.extract_text() or "",
                "words": page.extract_words(),
            })
    return tuple(pages)


def read_pages(path):
    path = Path(path)
    stat = path.stat()
    return _load(str(path), stat.st_mtime_ns, stat.st_size)


def readable(text):
    """False for pages whose glyphs have no text mapping (encoded fonts, raw control codes)."""
    if len(text.strip()) < MIN_READABLE_CHARS:
        return False
    control = sum(1 for c in text if ord(c) < 32 and c not in "\r\n\t")
    return control / len(text) < MAX_CONTROL_RATIO and text.count("(cid:") < MAX_CID_MARKERS


def printed_date(text):
    """Latest valid 'Effective: <Month> <d>, <yyyy>' on a page, as ISO, or None."""
    found = []
    for month, day, year in EFFECTIVE.findall(text):
        number = MONTHS.get(month[:3].lower())
        if number and 1 <= int(day) <= 31:
            found.append(f"{int(year):04d}-{number:02d}-{int(day):02d}")
    return max(found) if found else None


def sheet_of(text):
    match = SHEET.search("\n".join(text.splitlines()[:3]))
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(1)).replace("NBP", "NBP-", 1).replace("NBP--", "NBP-") \
        if match.group(1).startswith("NBP") else match.group(1)


def sheet_prefix(sheet):
    if sheet.startswith("NBP"):
        return "NBP-" + re.search(r"\d+", sheet).group(0)
    return sheet.split("-")[0]


def heading_of(text):
    """(kind, printed designation, printed name) from a page's title lines."""
    lines = [l.strip() for l in text.splitlines()[:9]]
    for index, line in enumerate(lines):
        kind = designation = None
        if re.match(r"^NBP[\s-]*(?:RIDER)?", line, re.I):
            kind, designation = "rider", re.sub(r"^NBP[\s-]*(?:RIDER)?[\s-]*", "", line, flags=re.I)
            designation = "NBP " + designation
        elif re.match(r"^OPTIONAL RIDER\s+", line, re.I):
            kind, designation = "rider", re.sub(r"^OPTIONAL RIDER\s+", "", line, flags=re.I)
        elif re.match(r"^RIDER\s+", line, re.I):
            kind, designation = "rider", re.sub(r"^RIDER\s+", "", line, flags=re.I)
        elif re.match(r"^SCHEDULE\s+\S", line):
            kind, designation = "schedule", re.sub(r"^SCHEDULE\s+", "", line)
        elif line.startswith("EXHIBIT OF APPLICABLE RIDERS"):
            kind, designation = "exhibit", line
        elif line.startswith("TERMS AND CONDITIONS"):
            kind, designation = "terms", line
        if kind:
            name = next((l for l in lines[index + 1:index + 4] if l.startswith("(") and "continu" not in l.lower()
                         and not l.lower().startswith("(cont")), "")
            return kind, designation.strip(), name.strip("() ")
    return None, None, None


def schedule_code(printed):
    """The printed designation with dots removed and dash spacing closed (e.g. L.G.S. – T.O.D. -> LGS-TOD)."""
    text = printed.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s*-\s*", "-", text).replace(".", "")
    return re.sub(r"\s+", "", text).strip("-")


def tariff_number(pages):
    for page in pages[:3]:
        match = TARIFF_NO.search(page["text"])
        if match and "APPALACHIAN POWER COMPANY" in page["text"].upper():
            return int(match.group(1))
    raise BookRefused("not an Appalachian Power Company tariff book: no 'APPALACHIAN POWER COMPANY' / "
                      "'TARIFF NO.' on the first pages")


def index_entries(pages):
    """The book's own table of contents: [{kind, designation, name, start_sheet, range}]."""
    lines = []
    for page in pages:
        if (sheet_of(page["text"]) or "").startswith("1-"):
            for line in page["text"].splitlines():
                line = line.strip()
                if not line or line.startswith(("APPALACHIAN", "VA. S.C.C.", "INDEX", "STANDARD SERVICE")):
                    continue
                if FOOTER.match(line):
                    continue
                lines.append(re.sub(r"\.{3,}|…", " ", line).strip())
    starts = re.compile(r"^(Schedule|Optional Rider|Rider|NBP-Rider|Underground|Exhibit|Towns|Terms)\b")
    entries = []
    for line in lines:
        if not line:
            continue
        if starts.match(line):
            entries.append(line)
        elif entries:
            entries[-1] += " " + line
    parsed = []
    tail = re.compile(r"((?:NBP-)?\d+[A-Z]?(?:\s*\.?-\s*\d+)?)(?:\s+thru\s+((?:NBP-)?\d+[A-Z]?(?:-\d+)?))?"
                      r"(?:\s+(\d+A-\d+\s+thru\s+\d+A-\d+))?\s*$")
    for text in entries:
        match = tail.search(text)
        if not match:
            continue
        head = text[:match.start()].strip()
        first = re.sub(r"[\s.]", "", match.group(1)).replace("--", "-")
        kind_match = re.match(r"^(Schedule|Optional Rider|NBP-Rider|Rider)\s+(.*)$", head)
        if not kind_match:
            continue
        tokens = kind_match.group(2).split()
        designation = [tokens[0]]
        for token in tokens[1:]:
            previous = designation[-1]
            if token in "-–" or previous in "-–" or previous.endswith(("-", "–")) or token.startswith(("-", "–")) \
                    or not any(c.islower() for c in token):
                designation.append(token)
            else:
                break
        name = " ".join(tokens[len(designation):])
        parsed.append({
            "kind": "schedule" if kind_match.group(1) == "Schedule" else "rider",
            "designation": " ".join(designation),
            "name": name,
            "start_sheet": first,
            "range": (match.group(0) or "").strip(),
        })
    return parsed


def book_groups(pages):
    """Group the readable pages by sheet prefix; return (groups, unreadable_pages)."""
    groups, unreadable = {}, []
    for page in pages:
        text = page["text"]
        sheet = sheet_of(text) if readable(text) else None
        if not readable(text):
            unreadable.append(page["n"])
            continue
        if sheet is None:
            continue
        key = sheet_prefix(sheet)
        group = groups.setdefault(key, {"key": key, "pages": [], "sheets": []})
        group["pages"].append(page)
        group["sheets"].append(sheet)
    for group in groups.values():
        for page in group["pages"]:
            kind, designation, name = heading_of(page["text"])
            if kind:
                group.update(kind=kind, heading=designation, name=name)
                break
        else:
            group.update(kind="other", heading=None, name="")
        dates = [d for d in (printed_date(p["text"]) for p in group["pages"]) if d]
        group["dates"] = sorted(set(dates))
        group["effective_from"] = max(dates) if dates else None
    return groups, unreadable


# ---------------------------------------------------------------- rate tables
def _lines(words):
    rows = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(word["top"] - rows[-1]["top"]) <= LINE_TOLERANCE:
            rows[-1]["words"].append(word)
        else:
            rows.append({"top": word["top"], "words": [word]})
    for row in rows:
        row["words"].sort(key=lambda w: w["x0"])
        row["text"] = " ".join(w["text"] for w in row["words"])
    return rows


def _headings_of(words):
    columns = {}
    for word in words:
        if word["text"] in ("Generation", "Distribution", "Total"):
            columns[word["text"].lower()] = (word["x0"] + word["x1"]) / 2
    return columns if len(columns) == 3 else None


def _cell_value(token):
    try:
        return Decimal(token.replace("$", "").replace(",", ""))
    except InvalidOperation:
        return None


def _clean(label):
    label = UNIT.sub("", label)
    return re.sub(r"\s+", " ", label).strip(" :.-")


def _kind_of(label, unit_key):
    low = label.lower()
    if unit_key == "$":
        return "basic_service_charge" if "service charge" in low else (
            "customer_charge" if "customer charge" in low else "fixed_monthly_charge")
    if unit_key in ("$/kW", "$/kVA"):
        if "excess" in low:
            return "off_peak_excess_demand_charge"
        return "on_peak_demand_charge" if "on-peak" in low else "demand_charge"
    return "energy_charge"


def _tier(label):
    low = label.lower()
    numbers = [Decimal(n.replace(",", "")) for n in re.findall(r"(\d[\d,]*)\s*kWh", label)]
    if not BLOCK.search(label):
        return None, None, None
    if "less than" in low or "equal to or less" in low:
        return Decimal(0), (numbers[0] if numbers else None), "kwh_per_kw"
    if "between" in low and len(numbers) >= 2:
        return numbers[0], numbers[1], "kwh_per_kw"
    if "excess" in low and numbers:
        return numbers[0], None, "kwh_per_kw"
    return None, None, None


def _season(label):
    match = re.search(r"((?:June|July|December|January|February|May|September)[A-Za-z]*\s*[-–]\s*[A-Za-z]+"
                      r"(?:\s*&\s*[A-Za-z]+\s*[-–]\s*[A-Za-z]+)?)", label)
    return match.group(1).strip() if match else None


def parse_rate_tables(group):
    """Read the Generation/Distribution/Total rows of a group. Returns (components, problems)."""
    components, problems, notes = [], [], []
    columns = None
    active = False
    context = {"voltage": None, "codes": None, "unit": None, "label": ""}
    buffer = []
    previous = None
    for page in group["pages"]:
        sheet = sheet_of(page["text"]) or group["key"]
        for row in _lines(page["words"]):
            text = row["text"]
            if TITLE_LINE.match(text) and not UNIT.fullmatch(text.strip()):
                continue
            if FOOTER.match(text):
                break  # end of this sheet's body; the rate region may continue on the next sheet
            found = _headings_of(row["words"])
            if found:
                columns = found
                active = True
                continue
            if RATE_START.match(text):
                active = True
                codes = CODES.search(text)
                context = {"voltage": None, "codes": re.sub(r"\s+and\s+", ", ", codes.group(1)).strip()
                           if codes else None, "unit": None, "label": ""}
                buffer, previous = [], None
                continue
            if not active:
                continue
            if text.startswith("Each kilowatt") or HEADING.match(text):
                active = False
                continue
            reactive = REACTIVE.search(" ".join(buffer + [text]))
            if reactive and "KVAR" in text:
                label = _clean(" ".join(buffer + [text.split("$")[0]]))
                components.append(_component(
                    "reactive_demand_charge", Decimal(reactive.group(1)), "usd_per_rkva", label, None, None,
                    context, sheet, page["n"], "Total", f"${reactive.group(1)} per KVAR"))
                buffer = []
                continue
            volt = VOLTAGE.match(text)
            if volt:
                context.update(voltage=volt.group(2), codes=volt.group(1) or volt.group(3) or context["codes"],
                               unit=None, label="")
                buffer, previous = [], None
                continue
            if columns is None:
                continue
            cells, label_words = {}, []
            clash = False
            for word in row["words"]:
                token = word["text"]
                center = (word["x0"] + word["x1"]) / 2
                near = min(columns, key=lambda k: abs(columns[k] - center))
                inside = abs(columns[near] - center) <= COLUMN_TOLERANCE
                if inside and (NUMBER.match(token) or PLACEHOLDER.match(token)):
                    if PLACEHOLDER.match(token):
                        continue
                    if near in cells:
                        clash = True
                    cells[near] = (token, _cell_value(token))
                else:
                    label_words.append(token)
            label_part = " ".join(label_words)
            if not cells:
                unit_only = UNIT.fullmatch(label_part.strip())
                if unit_only and previous is not None and previous["unit_key"] is None:
                    previous["unit_key"] = unit_only.group(1)
                    continue
                if UNIT.search(label_part) and not buffer and label_part.rstrip().endswith(")"):
                    context["unit"] = UNIT.search(label_part).group(1)
                    context["label"] = _clean(label_part)
                    continue
                buffer.append(label_part)
                continue
            if clash:
                problems.append(f"sheet {sheet}: two numbers fall under one column in the row "
                                f"'{_clean(' '.join(buffer + [label_part]))}'")
                buffer, previous = [], None
                continue
            full = " ".join(buffer + [label_part]).strip()
            unit = UNIT.search(full)
            previous = {
                "label": full, "cells": cells, "unit_key": unit.group(1) if unit else None,
                "context": dict(context), "sheet": sheet, "page": page["n"],
                "ctx_label": context["label"], "ctx_unit": context["unit"],
            }
            components.append(previous)
            buffer = []
    return _finish_rows(components, problems, notes)


def _component(ctype, value, unit, label, tier, season, context, sheet, page, column, cell_text):
    return {
        "component_type": ctype, "rate": value, "unit": unit, "billing_category": label,
        "tier": tier, "season": season, "variant": {"Total": "total"}.get(column, column.lower()),
        "billing_class": _class_of(context), "source_page": page,
        "source_cell": f'sheet {sheet}, row "{label}", column "{column}", cell "{cell_text}"',
    }


def _class_of(context):
    parts = []
    if context.get("voltage"):
        parts.append(f"{context['voltage']} Voltage")
    if context.get("codes"):
        parts.append(f"code {context['codes']}")
    return " ".join(parts) or None


def _last_place(cells):
    """One unit in the last printed decimal place of the row: the book's own rounding granularity."""
    places = max((len(t.split(".")[1]) if "." in t else 0) for t, _ in cells.values())
    return Decimal(1).scaleb(-places)


def _finish_rows(rows, problems, notes):
    out = []
    for item in rows:
        if "cells" not in item:
            out.append(item)
            continue
        unit_key = item["unit_key"] or item["ctx_unit"]
        if unit_key not in UNITS:
            problems.append(f"sheet {item['sheet']}: no printed unit for the row '{_clean(item['label'])}'")
            continue
        label = _clean(item["label"])
        if not item["unit_key"] and item["ctx_label"]:
            label = f"{_clean(item['ctx_label'])}: {label}"
        values = {k: v[1] for k, v in item["cells"].items()}
        if any(v is None for v in values.values()):
            problems.append(f"sheet {item['sheet']}: unreadable number in the row '{label}'")
            continue
        total = values.get("total")
        parts = [values[k] for k in ("generation", "distribution") if k in values]
        if total is None:
            problems.append(f"sheet {item['sheet']}: the row '{label}' prints no Total")
            continue
        if parts and abs(sum(parts) - total) > _last_place(item["cells"]):
            problems.append(f"sheet {item['sheet']}: the row '{label}' fails its printed cross-check "
                            f"(Generation + Distribution = {sum(parts)}, printed Total = {total})")
            continue
        if parts and sum(parts) != total:
            notes.append(f'sheet {item["sheet"]}, row "{label}": printed Generation + Distribution = '
                         f"{sum(parts)} but printed Total = {total} (a rounding difference of one printed "
                         f"unit in the book; both are stored as printed)")
        unit, scale = UNITS[unit_key]
        ctype = _kind_of(label, unit_key)
        tier = _tier(_clean(item["label"]))
        season = _season(label)
        for column in ("generation", "distribution", "total"):
            if column not in item["cells"]:
                continue
            token, value = item["cells"][column]
            if value == 0 and column != "total":
                continue  # a printed 0 in a split column; the Total variant carries the charge
            comp = _component(ctype, value * scale, unit, label, tier, season, item["context"],
                              item["sheet"], item["page"], column.capitalize(), token)
            comp["variant"] = column
            out.append(comp)
    return out, problems, notes


def parse_lighting_tables(group):
    """Schedule O.L.: 'code type watts lumens kWh Generation Distribution Total' lamp rows and pole charges."""
    components, problems = [], []
    row_pattern = re.compile(
        r"^(\d{3})\s+(.+?)\s+(\d{1,4})\*{0,2}\s+([\d,]+)\*{0,2}\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)$")
    for page in group["pages"]:
        sheet = sheet_of(page["text"])
        section = None
        for line in page["text"].splitlines():
            line = line.strip()
            if re.match(r"^[A-C]\.\s+\S", line):
                section = line
                continue
            match = row_pattern.match(line.replace("*", ""))
            if match:
                code, kind, watts, lumens, kwh, dist_gen, dist, total = match.groups()
                values = {"generation": Decimal(kwh), "distribution": Decimal(dist_gen)}
                if values["generation"] + values["distribution"] != Decimal(dist):
                    problems.append(f"sheet {sheet}: lamp {code} fails its printed cross-check "
                                    f"({values['generation']} + {values['distribution']} != {dist})")
                    continue
                label = f"{(section or '').strip()} | {kind} {watts}W".strip(" |")
                context = {"voltage": None, "codes": code}
                for column, value in (("generation", values["generation"]),
                                      ("distribution", values["distribution"]),
                                      ("total", Decimal(dist))):
                    if value == 0 and column != "total":
                        continue
                    comp = _component("lighting_base_charge", value, "usd_per_luminaire", label, None, None,
                                      context, sheet, page["n"], column.capitalize(), f"{value}")
                    comp["variant"] = column
                    components.append(comp)
                continue
            pole = re.match(r"^(Wood|Aluminum|Fiberglass|Steel|Concrete)\s+Pole\s+\$(\d+\.\d+)\s+per month$", line)
            if pole:
                components.append(_component(
                    "pole_charge", Decimal(pole.group(2)), "usd_per_month", f"{pole.group(1)} Pole",
                    None, None, {"voltage": None, "codes": None}, sheet, page["n"], "Total",
                    f"${pole.group(2)} per month"))
    if not components:
        problems.append("no lamp rows could be read")
    return components, problems


def parse_prose_rates(group, code):
    components = []
    slug = re.sub(r"[^a-z0-9]+", "_", code.lower()).strip("_")
    for page in group["pages"]:
        text = " ".join(page["text"].split())
        sheet = sheet_of(page["text"]) or group["key"]
        for suffix, pattern, unit, scale in PROSE_RATES:
            for match in pattern.finditer(text):
                value = Decimal(match.group(1)) * scale
                ctype = f"rider_{slug}_{suffix}" if suffix != "rate_reduction_credit" else f"rider_{slug}_{suffix}"
                components.append({
                    "component_type": ctype, "rate": value, "unit": unit,
                    "billing_category": f"printed rate: {match.group(0)}", "tier": None, "season": None,
                    "variant": "total", "billing_class": None, "source_page": page["n"],
                    "source_cell": f'sheet {sheet}, sentence "{match.group(0)}", cell "{match.group(1)}"',
                })
    return components


# ---------------------------------------------------------------- rules
def rule_rows(group, code, printed, entry_name):
    rules = []
    sheets = group["sheets"]
    first_page = group['pages'][0]['n']
    rules.append({
        "rule_type": "schedule_designation",
        "rule_text": f"Printed designation: {printed}; name: {entry_name or group.get('name') or 'not printed'}; "
                     f"sheets {sheets[0]} to {sheets[-1]}; PDF pages "
                     f"{group['pages'][0]['n']}-{group['pages'][-1]['n']}.",
        "source_page": first_page,
        "citation_status": "cited",
    })
    if len(group["dates"]) > 1:
        rules.append({
            "rule_type": "sheet_effective_dates",
            "rule_text": "The book prints different effective dates on this schedule's sheets: "
                        + ", ".join(group["dates"]) + f"; the version carries the latest ({group['effective_from']}).",
            "source_page": first_page,
            "citation_status": "cited",
        })
    lines = []
    for page in group["pages"]:
        started = False
        for line in page["text"].splitlines():
            line = line.strip()
            if not started:
                started = bool(TARIFF_NO.search(line)) or line.startswith("V.A. S.C.C.")
                continue
            if FOOTER.match(line) or not line:
                continue
            lines.append(line)
    sections, current = {}, None
    for line in lines:
        match = HEADING.match(line)
        if match and len(line.split()) <= 9 and not line.startswith("("):
            current = line
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    for heading, body in sections.items():
        text = " ".join(" ".join(body).split())
        if not text:
            continue
        for rule_type, pattern in RULE_HEADINGS:
            if pattern.search(heading.upper()):
                rules.append({
                    "rule_type": rule_type,
                    "rule_text": f"{heading}: {text}",
                    "source_page": None,
                    "citation_status": "uncited",
                })
                break
        else:
            if heading.upper().startswith(("MONTHLY RATE", "RATE")):
                for sentence in re.split(r"(?<=\.)\s+", text):
                    if TOU.search(sentence):
                        rules.append({
                            "rule_type": "tou_periods",
                            "rule_text": sentence.strip(),
                            "source_page": None,
                            "citation_status": "uncited",
                        })
    seen, out = set(), []
    for rule in rules:
        rule_key = (rule["rule_type"], rule["rule_text"])
        if rule_key not in seen:
            seen.add(rule_key)
            out.append(rule)
    return out


# ---------------------------------------------------------------- the book
def analyse_book(source):
    """Pure read: everything the book says, ready to store. No database access."""
    pages = read_pages(source)
    number = tariff_number(pages)
    index = index_entries(pages)
    groups, unreadable = book_groups(pages)
    by_start = {}
    for entry in index:
        by_start.setdefault(sheet_prefix(entry["start_sheet"]), entry)
    versions, declined = [], []

    for key, entry in by_start.items():
        group = groups.get(key)
        if group is None and entry["kind"] in ("schedule", "rider"):
            code = schedule_code(entry["designation"])
            declined.append({
                "schedule_code": code if entry["kind"] == "schedule" else f"RIDER {code}",
                "printed": entry["designation"], "name": entry["name"],
                "signal": SIGNAL_UNREADABLE,
                "pages": [], "reason": (
                    f"the index lists sheet {entry['range'] or entry['start_sheet']} but no readable page "
                    f"carries it; {len(unreadable)} page(s) of this book have no extractable text "
                    f"(PDF pages {_ranges(unreadable)}), so this schedule's rates cannot be read natively"),
            })

    for key, group in groups.items():
        entry = by_start.get(key)
        if group["kind"] not in ("schedule", "rider") or entry is None:
            continue
        printed = entry["designation"]
        code = schedule_code(printed)
        code = code if group["kind"] == "schedule" else f"RIDER {code}"
        span = f"PDF pages {group['pages'][0]['n']}-{group['pages'][-1]['n']}"
        item = {"schedule_code": code, "printed": printed, "name": entry["name"] or group["name"],
                "pages": [p["n"] for p in group["pages"]], "sheets": group["sheets"]}
        if group["effective_from"] is None:
            declined.append({**item, "signal": SIGNAL_UNPARSED,
                             "reason": f"no effective date is printed on any sheet ({span}); one is never assumed"})
            continue
        has_table = any(_headings_of(p["words"]) for p in group["pages"])
        problems, notes = [], []
        if has_table:
            components, problems, notes = parse_rate_tables(group)
        elif re.fullmatch(r"OL", code) or "lighting" in (entry["name"] or "").lower():
            components, problems = parse_lighting_tables(group)
        else:
            components = parse_prose_rates(group, code)
            if not components:
                declined.append({**item, "signal": SIGNAL_UNPARSED,
                                 "reason": f"no Generation/Distribution/Total rate table and no single-rate "
                                           f"sentence recognised ({span}); rates are not read from this layout"})
                continue
        if problems or not components:
            declined.append({**item, "signal": SIGNAL_UNPARSED,
                             "reason": "; ".join(problems) if problems else
                             f"the rate table yielded no rows ({span})"})
            continue
        versions.append({
            **item, "effective_from": group["effective_from"], "version": number,
            "components": components, "rules": rule_rows(group, code, printed, entry["name"]) + _note_rules(notes),
            "kind": group["kind"],
        })
    return {
        "tariff_number": number, "versions": versions, "declined": declined,
        "unreadable_pages": unreadable, "index": index,
        "groups": {k: {"kind": g["kind"], "heading": g["heading"], "name": g["name"],
                       "sheets": g["sheets"], "pages": [p["n"] for p in g["pages"]],
                       "dates": g["dates"]} for k, g in groups.items()},
    }


def _note_rules(notes):
    return [{"rule_type": "parse_note", "rule_text": n, "source_page": None, "citation_status": "uncited"} for n in notes]


def _ranges(numbers):
    numbers = sorted(numbers)
    if not numbers:
        return "none"
    out, start, last = [], numbers[0], numbers[0]
    for n in numbers[1:] + [None]:
        if n is not None and n == last + 1:
            last = n
            continue
        out.append(f"{start}" if start == last else f"{start}-{last}")
        if n is not None:
            start = last = n
    return ", ".join(out)


# ---------------------------------------------------------------- storage
def _store(conn, analysis, source_label):
    summary = {"created": [], "existing": [], "declined": [], "queued": 0}
    for version in analysis["versions"]:
        identity = (STATE, UTILITY, version["schedule_code"], version["version"], version["effective_from"])
        existing = conn.execute(
            "SELECT id FROM tariff_versions WHERE state = ? AND utility = ? AND schedule_code = ? "
            "AND version = ? AND effective_from = ? ORDER BY id DESC LIMIT 1", identity).fetchone()
        if existing:
            conn.execute(
                "UPDATE tariff_versions SET schedule_kind = ? WHERE id = ?",
                (version["kind"], existing[0]),
            )
            summary["existing"].append({"schedule_code": version["schedule_code"], "version_id": existing[0]})
            continue
        cursor = conn.execute(
            "INSERT INTO tariff_versions(state, utility, schedule_code, version, effective_from, "
            "status, schedule_kind) VALUES (?, ?, ?, ?, ?, 'draft', ?)",
            (*identity, version["kind"]))
        version_id = cursor.lastrowid
        for c in version["components"]:
            tier = c["tier"] or (None, None, None)
            conn.execute(
                "INSERT INTO tariff_components(tariff_version_id, component_type, billing_category, rate, "
                "formula, unit, tier_min, tier_max, tier_unit, season, variant, billing_class, source_page, "
                "source_cell) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (version_id, c["component_type"], c["billing_category"], float(c["rate"]), c["unit"],
                 None if tier[0] is None else float(tier[0]), None if tier[1] is None else float(tier[1]),
                 tier[2], c["season"], c["variant"], c["billing_class"], c["source_page"], c["source_cell"]))
        for rule in version["rules"]:
            conn.execute("INSERT INTO tariff_rules(tariff_version_id, rule_type, rule_text, source_page, citation_status) VALUES (?, ?, ?, ?, ?)",
                         (version_id, rule["rule_type"], rule["rule_text"], rule.get("source_page"), rule.get("citation_status", "uncited")))
        summary["created"].append({"schedule_code": version["schedule_code"], "version_id": version_id,
                                   "components": len(version["components"]), "rules": len(version["rules"])})
    for item in analysis["declined"]:
        queued = parse_review.queue_item(
            conn, SOURCE_KIND, f"{source_label} / {item['printed']} / sheets "
            f"{item.get('sheets', ['?'])[0] if item.get('sheets') else 'unreadable'}",
            item["schedule_code"], "rates", item["reason"], item["signal"],
            f"{UTILITY} {item['printed']} ({item['name']}): {item['reason']}")
        summary["queued"] += 1 if queued else 0
        summary["declined"].append({"schedule_code": item["schedule_code"], "reason": item["reason"]})
    return summary


def parse_book(source, target):
    """Contract adapter: read the whole book, write drafts + review items. Refuses a foreign source."""
    try:
        analysis = analyse_book(source)
    except BookRefused:
        raise
    except (OSError, ValueError) as error:
        raise BookRefused(f"the source could not be read as a PDF: {error}")
    if not analysis["versions"] and not analysis["declined"]:
        raise BookRefused("the book contains no recognisable schedules")
    label = f"{UTILITY} Tariff No. {analysis['tariff_number']}"
    with db.connect(target) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            summary = _store(conn, analysis, label)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    summary["tariff_number"] = analysis["tariff_number"]
    summary["unreadable_pages"] = analysis["unreadable_pages"]
    return summary


def main():
    parser = argparse.ArgumentParser(description="Parse an Appalachian Power Virginia tariff book into drafts.")
    parser.add_argument("pdf")
    args = parser.parse_args()
    target = db.default_target(ROOT / "data" / "tariffs.db")
    try:
        summary = parse_book(args.pdf, target)
    except (BookRefused, db.Error) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"created {len(summary['created'])}, existing {len(summary['existing'])}, "
          f"declined {len(summary['declined'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
