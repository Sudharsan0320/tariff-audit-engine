import argparse
import os
import re
import sqlite3
from pathlib import Path

import pdfplumber


ROOT = Path(__file__).resolve().parents[1]
PDF_PATH = ROOT / "data" / "raw" / "VEPGA-Amendment-12-Effective-July-1-2025.pdf"
DATABASE_PATH = Path(os.environ.get("TARIFF_DB_PATH") or ROOT / "data" / "tariffs.db")
EFFECTIVE_FROM = "2025-07-01"
RIDER_VERSION = 12
UTILITY = "Virginia Electric and Power Company"
STATE = "VA"
TARIFF_SCHEDULES = (
    "100",
    "102",
    "110",
    "120",
    "130",
    "131",
    "132",
    "134",
    "150",
    "151",
    "154",
    "SMH",
    "SSL",
)


def normalize_heading(value):
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def rider_specs():
    specs = {
        "A": ("FUEL CHARGE RIDER A", "uniform"),
        "CCR-CM": ("RIDER CCR-CM", "uniform"),
        "RPS-CM": ("RIDER RPS-CM", "uniform"),
        "PIPP-CM": ("RIDER PIPP-CM", "uniform"),
        "T-CM": ("RIDER T-CM", "table"),
        "GEN-CM": ("RIDER GEN-CM", "table"),
        "DIST-CM": ("RIDER DIST-CM", "table"),
        "CE-CM": ("RIDER CE-CM", "table"),
        "OSW-CM": ("RIDER OSW-CM", "table"),
        "SNA-CM": ("RIDER SNA-CM", "table"),
        "E-CM": ("RIDER E-CM", "table"),
        "RBB-CM": ("RIDER RBB-CM", "table"),
        "SMR-CM": ("RIDER SMR-CM", "table"),
        "G-CM": ("RIDER G - CM", "rec"),
        "K": ("RIDER K", "credit"),
        "L": ("RIDER L", "credit"),
    }
    return specs


def rider_component_type(rider_code):
    if rider_code == "A":
        title = "fuel_charge_rider_a"
    else:
        title = f"rider_{rider_code.lower()}"
    return re.sub(r"[^a-z0-9]+", "_", title).strip("_")


def find_rider_pages(pdf, rider_code):
    title, _ = rider_specs()[rider_code]
    normalized_title = normalize_heading(title)
    pages = []
    for page_number, page in enumerate(pdf.pages, start=1):
        text = page.extract_text() or ""
        headers = [normalize_heading(line.strip()) for line in text.splitlines()[:6]]
        if normalized_title in headers:
            pages.append((page_number, page, text))
    if not pages:
        raise ValueError(f"No source pages found for rider {rider_code}.")
    return pages


def schedule_codes_in_text(text):
    matches = re.findall(r"(?<!\w)(?:\d{3}|SMH|SSL)(?!\w)", text, re.I)
    return [match.upper() for match in matches]


def component(component_type, rate, unit, schedule_code):
    return (
        component_type,
        rate,
        unit,
        schedule_code,
        None,
        None,
        None,
        None,
        None,
    )


def parse_uniform_rider(rider_code, texts):
    text = " ".join(" ".join(page.splitlines()) for page in texts)
    schedule_clause = re.search(
        r"Rate Schedules\s+(.+?)\s+shall be increased by",
        text,
        re.I,
    )
    rate_match = re.search(
        r"increased by\s+(\d+(?:\.\d+)?)\s+cents per kilowatt-hour",
        text,
        re.I,
    )
    if schedule_clause is None or rate_match is None:
        raise ValueError(f"Could not resolve uniform rate for rider {rider_code}.")
    schedules = schedule_codes_in_text(schedule_clause.group(1))
    if len(schedules) != len(TARIFF_SCHEDULES) or set(schedules) != set(
        TARIFF_SCHEDULES
    ):
        raise ValueError(
            f"Unexpected schedule coverage for rider {rider_code}: {schedules}"
        )
    rate = float(rate_match.group(1)) / 100
    component_type = rider_component_type(rider_code)
    return [
        component(component_type, rate, "usd_per_kwh", schedule)
        for schedule in schedules
    ]


def parse_table_rider(rider_code, pages):
    component_type = rider_component_type(rider_code)
    extracted = []
    for _, page, _ in pages:
        for table in page.extract_tables():
            for row in table:
                if not row or not row[0]:
                    continue
                schedule = " ".join(row[0].split()).upper()
                if schedule not in TARIFF_SCHEDULES:
                    continue
                amounts = []
                for cell in row[1:]:
                    if not cell:
                        continue
                    match = re.search(
                        r"\$?\s*(\d+(?:\.\d+)?)\s*(¢/kWh|/kW)\b",
                        cell,
                        re.I,
                    )
                    if match:
                        rate = float(match.group(1))
                        unit = match.group(2).lower()
                        if unit == "¢/kwh":
                            rate = rate / 100
                            unit = "usd_per_kwh"
                        else:
                            unit = "usd_per_kw"
                        amounts.append((rate, unit))
                if len(amounts) != 1:
                    raise ValueError(
                        f"Expected one rate for rider {rider_code} / "
                        f"schedule {schedule}, found {len(amounts)}."
                    )
                rate, unit = amounts[0]
                extracted.append(component(component_type, rate, unit, schedule))
    if len(extracted) != len(TARIFF_SCHEDULES):
        raise ValueError(
            f"Expected {len(TARIFF_SCHEDULES)} table rates for rider "
            f"{rider_code}, found {len(extracted)}."
        )
    schedules = [row[3] for row in extracted]
    if len(set(schedules)) != len(TARIFF_SCHEDULES):
        raise ValueError(f"Duplicate schedule rows in rider {rider_code}.")
    return extracted


def parse_special_rider(rider_code, texts):
    text = " ".join(" ".join(page.splitlines()) for page in texts)
    if rider_code == "G-CM":
        rate_match = re.search(
            r"Monthly Rate for RECs\s*\(MRR\)\s*equals\s*"
            r"(\d+(?:\.\d+)?)\s+cents per kWh",
            text,
            re.I,
        )
        applicability = re.search(
            r"who also purchases Electricity Supply Service in accordance with "
            r"either\s+(.+?)\.",
            text,
            re.I,
        )
        if rate_match is None or applicability is None:
            raise ValueError("Could not resolve Rider G-CM REC rate or schedules.")
        schedules = schedule_codes_in_text(applicability.group(1))
        expected_schedules = ["100", "110", "120", "130", "131", "132", "134"]
        if set(schedules) != set(expected_schedules) or len(schedules) != 7:
            raise ValueError(f"Unexpected Rider G-CM schedules: {schedules}")
        return [
            component(
                rider_component_type(rider_code),
                float(rate_match.group(1)) / 100,
                "usd_per_kwh",
                schedule,
            )
            for schedule in schedules
        ]

    rate_match = re.search(
        r"(\d+(?:\.\d+)?)¢\s+Credit per kWh",
        text,
        re.I,
    )
    applicability = re.search(
        r"\bSchedules?\s+(.+?)\.",
        re.split(r"\bII\.", text, maxsplit=1, flags=re.I)[0],
        re.I,
    )
    if rate_match is None or applicability is None:
        raise ValueError(f"Could not resolve Rider {rider_code} credit.")
    schedules = schedule_codes_in_text(applicability.group(1))
    expected_schedules = ["100", "130", "131"] if rider_code == "K" else ["100", "130"]
    if set(schedules) != set(expected_schedules) or len(schedules) != len(
        expected_schedules
    ):
        raise ValueError(f"Unexpected Rider {rider_code} schedules: {schedules}")
    return [
        component(
            f"rider_{rider_code.lower()}_monthly_credit",
            -float(rate_match.group(1)) / 100,
            "usd_per_kwh",
            schedule,
        )
        for schedule in schedules
    ]


def parse_rider_components(rider_code, pages):
    _, parser_type = rider_specs()[rider_code]
    texts = [text for _, _, text in pages]
    if parser_type == "uniform":
        return parse_uniform_rider(rider_code, texts)
    if parser_type == "table":
        return parse_table_rider(rider_code, pages)
    return parse_special_rider(rider_code, texts)


def ensure_rider_version(conn, rider_code):
    schedule_code = f"RIDER {rider_code}"
    identity_values = (
        STATE,
        UTILITY,
        schedule_code,
        RIDER_VERSION,
        EFFECTIVE_FROM,
    )
    existing = conn.execute(
        """
        SELECT id FROM tariff_versions
        WHERE state = ? AND utility = ? AND schedule_code = ?
          AND version = ? AND effective_from = ?
        ORDER BY id DESC LIMIT 1
        """,
        identity_values,
    ).fetchone()
    if existing is not None:
        return existing[0]
    version_values = (*identity_values, "draft")
    cursor = conn.execute(
        """
        INSERT INTO tariff_versions(
            state, utility, schedule_code, version, effective_from, status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        version_values,
    )
    return cursor.lastrowid


def parse_rider(rider_code, pdf_path=PDF_PATH, database_path=DATABASE_PATH):
    rider_code = rider_code.upper()
    if rider_code not in rider_specs():
        raise ValueError(
            f"Unsupported rider {rider_code}; expected one of "
            f"{', '.join(rider_specs())}."
        )
    with pdfplumber.open(pdf_path) as pdf:
        pages = find_rider_pages(pdf, rider_code)
        extracted = parse_rider_components(rider_code, pages)

    with sqlite3.connect(database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            version_id = ensure_rider_version(conn, rider_code)
            existing_count = conn.execute(
                "SELECT COUNT(*) FROM tariff_components WHERE tariff_version_id = ?",
                (version_id,),
            ).fetchone()[0]
            if existing_count:
                if existing_count != len(extracted):
                    raise ValueError(
                        f"Rider version {version_id} has {existing_count} "
                        f"components; expected {len(extracted)}."
                    )
                conn.commit()
                return version_id, 0
            for (
                component_type,
                rate,
                unit,
                billing_category,
                season,
                tier_min,
                tier_max,
                tier_unit,
                variant,
            ) in extracted:
                conn.execute(
                    """
                    INSERT INTO tariff_components(
                        tariff_version_id, component_type, billing_category, rate,
                        formula, unit, season, tier_min, tier_max, tier_unit,
                        variant
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        component_type,
                        billing_category,
                        rate,
                        unit,
                        season,
                        tier_min,
                        tier_max,
                        tier_unit,
                        variant,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return version_id, len(extracted)


def main():
    parser = argparse.ArgumentParser(description="Parse a Virginia tariff rider.")
    parser.add_argument("rider_code", help="Rider code, e.g. T-CM or A")
    args = parser.parse_args()
    try:
        version_id, component_count = parse_rider(args.rider_code)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    print(f"Version {version_id}: {component_count} components inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
