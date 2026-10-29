import argparse
from datetime import datetime
from pathlib import Path

import pdfplumber

if __package__:
    from .normalize_schedule import normalize_schedule_code
else:
    from normalize_schedule import normalize_schedule_code


# Annual-summary column starts near x=279 on Dominion EAP page 2.
LEFT_COLUMN_RIGHT_EDGE = 270
TURN_ON_DATE_FORMAT = "%B %d, %Y"
FIELD_LABELS = (
    ("Phone Number", "phone_number"),
    ("Mailing Address", "mailing_address"),
    ("Customer Class", "customer_class"),
    ("ACCOUNT NO.", "account_number"),
    ("Service Address", "service_address"),
    ("Turn On Date", "turn_on_date_raw"),
    ("District Office", "district_office"),
    ("Tax District", "tax_district"),
    ("NAICS Code", "naics_code"),
    ("Meter Number(s)", "meter_numbers"),
    ("Current Rate", "current_rate_raw"),
    ("Voltage", "voltage"),
    ("Delivery Phase", "delivery_phase"),
    ("Minimum Demand", "minimum_demand"),
    ("Facility Charge", "facility_charge"),
    ("Billing Status", "billing_status"),
)
LABEL_PREFIXES = tuple(label for label, _ in FIELD_LABELS)


def find_profile_page(pdf):
    for page in pdf.pages:
        text = page.extract_text() or ""
        if any(line.strip() == "Account Profile" for line in text.splitlines()):
            return page
    raise ValueError("No Account Profile page found in the PDF.")


def extract_header_account_number(pdf):
    first_text = pdf.pages[0].extract_text() or ""
    for line in first_text.splitlines():
        if line.strip().startswith("Account Number:"):
            _, _, number = line.partition(":")
            number = number.strip()
            if number.isdigit():
                return number
            raise ValueError(f"Unreadable account number on page 1: {line!r}.")
    raise ValueError("Page 1 has no Account Number line.")


def left_column_text(page):
    cropped = page.crop((0, 0, LEFT_COLUMN_RIGHT_EDGE, page.height))
    text = cropped.extract_text()
    if not text or not text.strip():
        raise ValueError("Account Profile left column is empty.")
    return text


def line_starts_with_label(line):
    return next(
        (label for label in LABEL_PREFIXES if line.startswith(label)),
        None,
    )


def parse_profile_text(profile_text, header_account_number):
    """Parse native (non-OCR) left-column Account Profile text."""
    lines = [line.rstrip() for line in profile_text.splitlines() if line.strip()]
    try:
        start = next(
            index for index, line in enumerate(lines) if line == "Account Profile"
        )
    except StopIteration as error:
        raise ValueError("Account Profile heading is missing.") from error

    account_name = None
    for line in lines[start + 1 :]:
        if line_starts_with_label(line) is None:
            account_name = line.strip()
            break
    if not account_name:
        raise ValueError("Account Profile name is missing.")

    values = {key: "" for _, key in FIELD_LABELS}
    index = start + 1
    while index < len(lines):
        line = lines[index]
        label = line_starts_with_label(line)
        if label is None:
            index += 1
            continue
        key = next(field for field_label, field in FIELD_LABELS if field_label == label)
        remainder = line[len(label) :].strip()
        collected = [remainder] if remainder else []
        index += 1
        if key == "service_address":
            while index < len(lines) and line_starts_with_label(lines[index]) is None:
                collected.append(lines[index].strip())
                index += 1
        values[key] = " ".join(part for part in collected if part)

    account_number = values["account_number"]
    if account_number != header_account_number:
        raise ValueError(
            "Account number mismatch: page 1 has "
            f"{header_account_number!r}, profile has {account_number!r}."
        )

    try:
        turn_on_date = datetime.strptime(
            values["turn_on_date_raw"], TURN_ON_DATE_FORMAT
        ).date().isoformat()
    except ValueError as error:
        raise ValueError(
            f"Unreadable Turn On Date: {values['turn_on_date_raw']!r}."
        ) from error

    schedule_code = normalize_schedule_code(values["current_rate_raw"])
    return {
        "account_number": account_number,
        "account_name": account_name,
        "customer_class": values["customer_class"],
        "mailing_address": values["mailing_address"],
        "service_address": values["service_address"],
        "current_rate_raw": values["current_rate_raw"].strip().upper(),
        "schedule_code": schedule_code,
        "turn_on_date": turn_on_date,
        "meter_numbers": values["meter_numbers"],
        "voltage": values["voltage"],
        "delivery_phase": values["delivery_phase"],
        "minimum_demand": values["minimum_demand"],
        "facility_charge": values["facility_charge"],
        "billing_status": values["billing_status"],
        "district_office": values["district_office"],
    }


def extract_account_profile(pdf_path):
    path = Path(pdf_path)
    with pdfplumber.open(path) as pdf:
        header_account_number = extract_header_account_number(pdf)
        profile_page = find_profile_page(pdf)
        profile_text = left_column_text(profile_page)
    profile = parse_profile_text(profile_text, header_account_number)
    profile["source_path"] = str(path)
    return profile


def main():
    parser = argparse.ArgumentParser(
        description="Extract a Dominion EAP account profile from a native PDF."
    )
    parser.add_argument("pdf_path", type=str, help="Path to the EAP PDF")
    args = parser.parse_args()
    profile = extract_account_profile(args.pdf_path)
    for key, value in profile.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
