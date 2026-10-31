import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

import pdfplumber

if __package__:
    from .normalize_schedule import normalize_schedule_code
else:
    from normalize_schedule import normalize_schedule_code


LEFT_COLUMN_RIGHT_EDGE = 270
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def canonical_source_path(path):
    """Store project-relative POSIX paths so the database is machine-independent."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)

MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
PROFILE_FIELDS = (
    ("Phone Number", "phone_number"),
    ("Mailing Address", "mailing_address"),
    ("Customer Class", "customer_class"),
    ("ACCOUNT NO.", "account_number"),
    ("Service Address", "service_address"),
    ("Turn On Date", "turn_on_date_raw"),
    ("District Office", "district_office"),
    ("Tax District", "tax_district"),
    ("NAICS Code", "naics_code"),
    ("Meter Number(s)", "meter_number"),
    ("Current Rate", "current_rate_raw"),
    ("Voltage", "voltage"),
    ("Delivery Phase", "delivery_phase"),
    ("Minimum Demand", "minimum_demand"),
    ("Facility Charge", "facility_charge"),
    ("Billing Status", "billing_status"),
)
FIELD_PREFIXES = tuple(label for label, _ in PROFILE_FIELDS)
MONEY_PATTERN = re.compile(
    r"(?<![\w/])(?:\(?-?\$?-?[\d,]+(?:\.\d+)?\)?)(?![\w/])"
)


def _decimal(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.upper() in {"N/A", "NA", "-"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _money_values(value):
    text = re.sub(r"\$\s+", "$", value)
    values = []
    for match in MONEY_PATTERN.finditer(text):
        token = match.group().strip()
        negative = token.startswith("(") and token.endswith(")")
        amount = token.strip("()").replace("$", "").replace(",", "")
        try:
            parsed = Decimal(amount)
        except InvalidOperation:
            continue
        values.append(-abs(parsed) if negative else parsed)
    return values


def _line_groups(words, tolerance=2.5):
    groups = []
    for word in sorted(words, key=lambda item: (item["top"], item["x0"])):
        if not groups or abs(word["top"] - groups[-1][0]["top"]) > tolerance:
            groups.append([word])
        else:
            groups[-1].append(word)
    return groups


def _profile_values(page):
    cropped = page.crop((0, 0, LEFT_COLUMN_RIGHT_EDGE, page.height))
    text = cropped.extract_text() or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    try:
        heading = lines.index("Account Profile")
    except ValueError as error:
        raise ValueError("Account Profile section not found.") from error

    account_name = next(
        (
            line
            for line in lines[heading + 1 :]
            if not any(line.startswith(label) for label in FIELD_PREFIXES)
        ),
        None,
    )
    if not account_name:
        raise ValueError("Account name is missing from the Account Profile section.")

    values = {key: None for _, key in PROFILE_FIELDS}
    index = heading + 1
    while index < len(lines):
        line = lines[index]
        label = next(
            (candidate for candidate in FIELD_PREFIXES if line.startswith(candidate)),
            None,
        )
        if label is None:
            index += 1
            continue
        key = next(field for field_label, field in PROFILE_FIELDS if field_label == label)
        collected = [line[len(label) :].strip()]
        if key == "service_address":
            collected[0] = re.split(
                r"\bANNUAL SUMMARY\b", collected[0], maxsplit=1, flags=re.IGNORECASE
            )[0].strip()
        index += 1
        if key == "service_address":
            while index < len(lines) and not any(
                lines[index].startswith(candidate) for candidate in FIELD_PREFIXES
            ):
                if re.search(
                    r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", " ".join(collected), re.I
                ) or "ANNUAL SUMMARY" in lines[index].upper():
                    break
                collected.append(lines[index])
                index += 1
        value = " ".join(part for part in collected if part).strip()
        values[key] = value or None

    values["account_name"] = account_name
    try:
        values["turn_on_date"] = datetime.strptime(
            values["turn_on_date_raw"], "%B %d, %Y"
        ).date().isoformat()
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Unreadable Turn On Date: {values['turn_on_date_raw']!r}."
        ) from error

    for key in ("minimum_demand", "facility_charge"):
        if isinstance(values[key], str) and values[key].strip().upper() in {"N/A", "NA"}:
            values[key] = None
    return values


def _header_account_number(pdf):
    first_page = pdf.pages[0].extract_text() or ""
    if not any(line.strip() == "Electric Account Profile" for line in first_page.splitlines()):
        raise ValueError("Document is not an Electric Account Profile.")
    for line in first_page.splitlines():
        if line.strip().startswith("Account Number:"):
            value = line.partition(":")[2].strip()
            if value.isdigit():
                return value
            raise ValueError(f"Unreadable account number on page 1: {line!r}.")
    raise ValueError("Electric Account Profile has no account number on page 1.")


def _extract_annual_summaries(pdf):
    summaries = []
    pattern = re.compile(
        r"\b(20\d{2})(?:\s+\(YTD\))?\s+\$([\d,]+(?:\.\d+)?)\s+"
        r"([\d,]+)\s+kWh\b",
        re.IGNORECASE,
    )
    for page in pdf.pages:
        for line in (page.extract_text() or "").splitlines():
            match = pattern.search(line.strip())
            if match:
                summaries.append(
                    {
                        "year": int(match.group(1)),
                        "is_ytd": "(YTD)" in line.upper(),
                        "total_billed": Decimal(match.group(2).replace(",", "")),
                        "total_usage_kwh": int(match.group(3).replace(",", "")),
                    }
                )
    return summaries


def _parse_cell_value(label, value):
    if not value:
        return None
    if label in {"Bill From", "Bill To"}:
        try:
            return datetime.strptime(value, "%m/%d/%y").date().isoformat()
        except ValueError:
            return value
    if label == "Billed Rate":
        return value
    if label == "Billing Days":
        number = _decimal(value)
        return int(number) if number is not None and number == number.to_integral_value() else None
    if label in {"Total Consumption", "Demand"}:
        return _decimal(value)
    return None


def _row_cells(words, centers):
    boundary = centers[0] - 18
    label_words = []
    cell_words = [[] for _ in centers]
    for word in sorted(words, key=lambda item: item["x0"]):
        center = (word["x0"] + word["x1"]) / 2
        if center < boundary:
            label_words.append(word["text"])
            continue
        column = min(range(len(centers)), key=lambda index: abs(center - centers[index]))
        if abs(center - centers[column]) <= 18:
            cell_words[column].append(word["text"])
    label = " ".join(label_words).strip()
    cells = [" ".join(words_in_cell).strip() or None for words_in_cell in cell_words]
    return label, cells


def _row_label_is_bold(words, centers):
    boundary = centers[0] - 18
    label_words = [
        word
        for word in words
        if (word["x0"] + word["x1"]) / 2 < boundary
    ]
    return any("bold" in word.get("fontname", "").casefold() for word in label_words)


def _new_month(month_name, year):
    return {
        "history_year": year,
        "month_name": month_name,
        "bill_from": None,
        "bill_to": None,
        "billing_days": None,
        "billed_rate_raw": None,
        "schedule_code": None,
        "total_consumption_kwh": None,
        "demand_kw": None,
        "total_billed": None,
        "bill_summary_subtotal": None,
        "bill_summary_rollups": [],
        "bill_charges": [],
        "bill_determinants": [],
    }


def _determinant_type(label):
    return re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")


def _extract_history(pdf):
    sections = []
    current_section = None
    current_months = []
    centers = []
    in_bill_summary = False
    in_bill_determinants = False
    year_pattern = re.compile(r"Historical Electricity Usage\s*-\s*(20\d{2})", re.I)

    for page in pdf.pages:
        for words in _line_groups(page.extract_words(extra_attrs=["fontname"])):
            line = " ".join(word["text"] for word in sorted(words, key=lambda item: item["x0"]))
            year_match = year_pattern.search(line)
            if year_match:
                current_section = {"year": int(year_match.group(1)), "months": []}
                sections.append(current_section)
                current_months = []
                centers = []
                in_bill_summary = False
                in_bill_determinants = False
                continue
            if current_section is None:
                continue

            month_words = [word for word in words if word["text"].upper() in MONTHS]
            if len(month_words) >= 2 and len(month_words) == len(words):
                month_words.sort(key=lambda item: item["x0"])
                current_months = [word["text"].upper() for word in month_words]
                centers = [
                    (word["x0"] + word["x1"]) / 2 for word in month_words
                ]
                current_section["months"] = [
                    _new_month(month, current_section["year"])
                    for month in current_months
                ]
                in_bill_summary = False
                in_bill_determinants = False
                continue
            if not centers:
                continue

            label, cells = _row_cells(words, centers)
            normalized_label = re.sub(r"\s+", " ", label).strip()
            if normalized_label == "Bill Determinants":
                in_bill_determinants = True
                in_bill_summary = False
                continue
            if normalized_label == "Bill Summary":
                in_bill_summary = True
                in_bill_determinants = False
                continue

            field_name = {
                "Bill From": "bill_from",
                "Bill To": "bill_to",
                "Billing Days": "billing_days",
                "Billed Rate": "billed_rate_raw",
                "Total Consumption": "total_consumption_kwh",
                "Demand": "demand_kw",
            }.get(normalized_label)
            if field_name:
                for month, value in zip(current_section["months"], cells):
                    parsed = _parse_cell_value(normalized_label, value)
                    month[field_name] = parsed
                    if in_bill_determinants and parsed is not None:
                        month["bill_determinants"].append(
                            {
                                "label": normalized_label,
                                "determinant_type": _determinant_type(normalized_label),
                                "value": parsed,
                            }
                        )
                    if field_name == "billed_rate_raw" and parsed:
                        try:
                            month["schedule_code"] = normalize_schedule_code(parsed)
                        except ValueError:
                            month["schedule_code"] = None
                continue

            if in_bill_determinants:
                for month, value in zip(current_section["months"], cells):
                    parsed = _decimal(value)
                    if parsed is not None:
                        month["bill_determinants"].append(
                            {
                                "label": normalized_label,
                                "determinant_type": _determinant_type(normalized_label),
                                "value": parsed,
                            }
                        )
                continue

            if re.match(r"^\*{0,2}\s*(?:total charges|total billed|bill total)\b", normalized_label, re.I):
                for month, value in zip(current_section["months"], cells):
                    amounts = _money_values(value or "")
                    if amounts:
                        month["total_billed"] = amounts[-1]
                in_bill_summary = False
                continue

            if not in_bill_summary:
                continue
            if re.match(r"^\*\s*subtotal\b", normalized_label, re.I):
                for month, value in zip(current_section["months"], cells):
                    amounts = _money_values(value or "")
                    if amounts:
                        month["bill_summary_subtotal"] = amounts[-1]
                in_bill_summary = False
                continue

            if _row_label_is_bold(words, centers) and normalized_label not in {
                "Basic Cust. Charges",
                "Basic Customer Chg",
            }:
                for month, value in zip(current_section["months"], cells):
                    amounts = _money_values(value or "")
                    if amounts:
                        month["bill_summary_rollups"].append(
                            {"line_label": normalized_label, "amount": amounts[-1]}
                        )
                continue

            for month, value in zip(current_section["months"], cells):
                amounts = _money_values(value or "")
                if amounts:
                    month["bill_charges"].append(
                        {"line_label": normalized_label, "amount": amounts[-1]}
                    )

    bills = []
    for section in sections:
        for month in section["months"]:
            month["month"] = MONTHS[month.pop("month_name")]
            bills.append(month)
    return sections, bills


def parse_usage_pdf(pdf_path):
    """Extract a Dominion EAP into a reviewable draft without database writes."""
    path = Path(pdf_path).resolve()
    with pdfplumber.open(path) as pdf:
        account_number = _header_account_number(pdf)
        profile_page = next(
            (
                page
                for page in pdf.pages
                if "Account Profile" in (page.extract_text() or "").splitlines()
            ),
            None,
        )
        if profile_page is None:
            raise ValueError("No Account Profile section found in the document.")
        account = _profile_values(profile_page)
        if account.get("account_number") != account_number:
            raise ValueError(
                "Account number mismatch: page 1 has "
                f"{account_number!r}, profile has {account.get('account_number')!r}."
            )
        sections, bills = _extract_history(pdf)
        annual_summaries = _extract_annual_summaries(pdf)

    if not sections or not bills:
        raise ValueError("No Historical Electricity Usage sections found.")
    annual_by_year = {summary["year"]: summary for summary in annual_summaries}
    for section in sections:
        witness = annual_by_year.get(section["year"], {})
        if witness.get("is_ytd"):
            section["history_year_status"] = "year_to_date"
        elif len(section["months"]) < 12:
            section["history_year_status"] = "partial_source"
        else:
            section["history_year_status"] = "complete"
    for bill in bills:
        bill["account_number"] = account_number
        bill["source_file"] = canonical_source_path(path)
        bill["history_year_status"] = next(
            section["history_year_status"]
            for section in sections
            if section["year"] == bill["history_year"]
        )
    return {
        "account": account,
        "annual_summaries": annual_summaries,
        "history_sections": [
            {
                "year": section["year"],
                "month_count": len(section["months"]),
                "history_year_status": section["history_year_status"],
            }
            for section in sections
        ],
        "bills": bills,
        "source_file": canonical_source_path(path),
    }


def summarize_draft(draft):
    years = {}
    for bill in draft["bills"]:
        year = bill["history_year"]
        summary = years.setdefault(
            year,
            {
                "month_count": 0,
                "usage_kwh": Decimal(0),
                "billed": Decimal(0),
                "rates": [],
                "rate_sequence": [],
                "history_year_status": bill.get("history_year_status", "complete"),
            },
        )
        summary["month_count"] += 1
        summary["usage_kwh"] += bill["total_consumption_kwh"] or Decimal(0)
        summary["billed"] += bill["total_billed"] or Decimal(0)
        if bill["billed_rate_raw"] not in summary["rates"]:
            summary["rates"].append(bill["billed_rate_raw"])
        summary["rate_sequence"].append(
            {"month": bill["month"], "billed_rate_raw": bill["billed_rate_raw"]}
        )
        if bill.get("history_year_status", "complete") != summary["history_year_status"]:
            summary["history_year_status"] = "inconsistent"

    months = []
    for bill in draft["bills"]:
        months.append(
            {
                "year": bill["history_year"],
                "month": bill["month"],
                "bill_from": bill["bill_from"],
                "bill_to": bill["bill_to"],
                "billing_days": bill["billing_days"],
                "billed_rate_raw": bill["billed_rate_raw"],
                "total_consumption_kwh": bill["total_consumption_kwh"],
                "demand_kw": bill["demand_kw"],
                "charge_line_count": len(bill["bill_charges"]),
                "charge_sum": sum(
                    (charge["amount"] for charge in bill["bill_charges"]), Decimal(0)
                ),
            }
        )
    return {"years": years, "months": months}


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}.")


def main():
    parser = argparse.ArgumentParser(
        description="Extract and print a reviewable Dominion EAP usage draft."
    )
    parser.add_argument("pdf_path", type=Path, help="Path to a native EAP PDF")
    args = parser.parse_args()
    draft = parse_usage_pdf(args.pdf_path)
    draft["report"] = summarize_draft(draft)
    print(json.dumps(draft, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())