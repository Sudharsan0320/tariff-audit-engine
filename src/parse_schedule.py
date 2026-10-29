import argparse
import pdfplumber
import re
import sqlite3


parser = argparse.ArgumentParser(description="Parse a Virginia tariff schedule.")
parser.add_argument("schedule_code", type=str, help="Schedule code to parse")
args = parser.parse_args()

pdf_path = "data/raw/VEPGA-Amendment-12-Effective-July-1-2025.pdf"
conn = sqlite3.connect("data/tariffs.db")

matching_pages = []
schedule_header = f"SCHEDULE {args.schedule_code}"
with pdfplumber.open(pdf_path) as pdf:
    for page_index, page in enumerate(pdf.pages):
        page_text = page.extract_text()
        if page_text is None:
            continue
        lines = page_text.splitlines()
        if not lines:
            continue
        first_line = lines[0].strip()
        if first_line == schedule_header:
            matching_pages.append((page_index, page_text))

print(f"{len(matching_pages)} pages found")
print("Pages:", ", ".join(str(page_index + 1) for page_index, _ in matching_pages))

cur = conn.cursor()
version_values = (
    "VA",
    "Virginia Electric and Power Company",
    args.schedule_code,
    12,
    "2025-07-01",
    "draft",
)
cur.execute(
    """
    SELECT id FROM tariff_versions
    WHERE state = ? AND utility = ? AND schedule_code = ?
      AND version = ? AND effective_from = ? AND status = ?
    ORDER BY id DESC
    LIMIT 1
    """,
    version_values,
)
existing_version = cur.fetchone()
if existing_version is None:
    cur.execute(
        """
        INSERT INTO tariff_versions(
            state, utility, schedule_code, version, effective_from, status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        version_values,
    )
    tariff_version_id = cur.lastrowid
else:
    tariff_version_id = existing_version[0]

print(tariff_version_id)
conn.commit()


def schedule_b_components(schedule_text):
    paragraphs = {}
    current_paragraph = None
    for line in schedule_text.splitlines():
        heading = re.match(r"^([A-Z])\.\s*(.*)$", line.strip())
        if heading:
            current_paragraph = heading.group(1)
            if current_paragraph in {"A", "B"}:
                paragraphs[current_paragraph] = [heading.group(2)]
            else:
                current_paragraph = None
        elif current_paragraph in paragraphs:
            paragraphs[current_paragraph].append(line.strip())

    extracted = []
    for option in ("A", "B"):
        paragraph = " ".join(paragraphs.get(option, []))
        items = list(
            re.finditer(
                r"\((i|ii|iii|iv)\)\s*(.*?)"
                r"(?=,?\s*plus\s+\((?:ii|iii|iv)\)|$)",
                paragraph,
                re.I,
            )
        )
        for item in items:
            description = item.group(2)
            if option == "A" or re.search(
                r"\bMonthly Facilities Charge\b", description, re.I
            ):
                component_type = "monthly_facilities_charge"
                amount = re.search(r"(\d+(?:\.\d+)?)\s*%", description)
                if amount is None:
                    raise ValueError(
                        f"Missing percentage in Schedule B item: {description}"
                    )
                rate = float(amount.group(1))
            elif re.search(r"\bOne-time Facilities Charge\b", description, re.I):
                component_type = "one_time_facilities_charge"
                rate = 100.0
            else:
                raise ValueError(f"Unrecognized Schedule B charge item: {description}")

            if re.search(r"\bdistribution and substation facilities\b", description, re.I):
                facility = "distribution"
            elif re.search(r"\btransmission facilities\b", description, re.I):
                facility = "transmission"
            else:
                raise ValueError(
                    f"Unrecognized Schedule B facility class: {description}"
                )

            extracted.append(
                (
                    component_type,
                    rate,
                    "percent",
                    f"option_{option.lower()}_{facility}",
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )

    if len(extracted) != 6:
        raise ValueError(
            f"Expected 6 Schedule B facilities charge items, found {len(extracted)}."
        )
    return extracted


def schedule_c_components(schedule_text):
    charge_specs = {
        "1": ("connection_charge", "usd_per_service"),
        "2": ("minimum_temporary_service_charge", "usd_per_service"),
        "3": ("tax_effect_recovery_factor", "multiplier"),
        "5": ("alternate_service_meter_minimum_charge", "usd_per_month"),
    }
    paragraphs = {}
    current_number = None
    for line in schedule_text.splitlines():
        heading = re.match(r"^(\d+)\.\s*(.*)$", line.strip())
        if heading:
            current_number = heading.group(1)
            paragraphs[current_number] = [heading.group(2)]
        elif current_number is not None:
            paragraphs[current_number].append(line.strip())

    extracted = []
    for number, (component_type, unit) in charge_specs.items():
        paragraph = " ".join(paragraphs.get(number, []))
        if not paragraph:
            raise ValueError(f"Missing Schedule C charge item {number}.")
        if number == "3":
            if not re.search(r"\bFactor\b", paragraph, re.I):
                raise ValueError("Missing Factor charge name in Schedule C item 3.")
            factor_match = re.search(
                r"\bFactor\s+of\s+(\d+(?:\.\d+)?)\b", paragraph, re.I
            )
            if factor_match is None:
                raise ValueError(
                    f"Missing plain decimal Factor rate in Schedule C item 3: "
                    f"{paragraph}"
                )
            rate = float(factor_match.group(1))
        else:
            amount_matches = list(
                re.finditer(r"\$\s*(\d+(?:\.\d+)?)", paragraph)
            )
            if len(amount_matches) != 1:
                raise ValueError(
                    f"Expected one dollar amount in Schedule C item {number}: "
                    f"{paragraph}"
                )
            if number == "1" and not re.search(
                r"\bper\s+service\b", paragraph, re.I
            ):
                raise ValueError(
                    f"Missing per service unit in Schedule C item 1: {paragraph}"
                )
            if number == "2" and not re.search(
                r"\bper\s+instance\b", paragraph, re.I
            ):
                raise ValueError(
                    f"Missing per instance unit in Schedule C item 2: {paragraph}"
                )
            rate = float(amount_matches[0].group(1))
        extracted.append(
            (
                component_type,
                rate,
                unit,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        )
    return extracted


def schedule_d_components(charge_section):
    clauses = {}
    current_clause = None
    for line in charge_section.splitlines():
        section_heading = re.match(r"^\s*([IVXLCDM]+)\.\s", line, re.I)
        if (
            section_heading
            and line[section_heading.end() :].strip()
            == line[section_heading.end() :].strip().upper()
        ):
            current_clause = None
            continue
        clause_heading = re.match(r"^\s*([ABC])\.\s*(.*)$", line)
        if clause_heading:
            current_clause = clause_heading.group(1)
            clauses.setdefault(current_clause, []).append(clause_heading.group(2))
        elif current_clause is not None:
            clauses[current_clause].append(line.strip())

    variant_markers = (
        (
            "A",
            "mh_mv_to_sv_type_1",
            lambda text: (
                re.search(r"\bMH\s+or\s+MV\b", text, re.I)
                and re.search(r"\bSV\b", text, re.I)
                and re.search(r"\bType\s+1\b", text, re.I)
            ),
        ),
        (
            "B",
            "basic_rate_to_led",
            lambda text: (
                re.search(r"\bnon-premium\s*\(basic\)\s*rate\b", text, re.I)
                and re.search(r"\bLED\b", text, re.I)
            ),
        ),
        (
            "C",
            "premium_rate_to_led",
            lambda text: (
                re.search(r"\bpremium rate\b", text, re.I)
                and re.search(r"\bLED\b", text, re.I)
            ),
        ),
    )
    extracted = []
    for clause_id, variant, marker in variant_markers:
        clause = " ".join(clauses.get(clause_id, []))
        if not clause:
            raise ValueError(f"Missing Schedule D customer charge clause II.{clause_id}.")
        if not marker(clause):
            raise ValueError(
                f"Could not resolve Schedule D clause II.{clause_id} conversion class."
            )
        flat_rates = list(
            re.finditer(
                r"\bflat\s+rate\s+of\s+\$\s*(\d+(?:\.\d+)?)\s+per\s+luminaire\b",
                clause,
                re.I,
            )
        )
        if len(flat_rates) != 1:
            raise ValueError(
                f"Expected one flat per-luminaire rate in Schedule D clause "
                f"II.{clause_id}: {clause}"
            )
        extracted.append(
            (
                "luminaire_conversion_charge",
                float(flat_rates[0].group(1)),
                "usd_per_luminaire",
                variant,
                None,
                None,
                None,
                None,
                None,
            )
        )
    return extracted


def schedule_e_components(schedule_text):
    normalized_text = " ".join(schedule_text.splitlines())
    if not re.search(r"\bSTREET LIGHTING PATROL SERVICE\b", normalized_text, re.I):
        raise ValueError("Missing Schedule E Street Lighting Patrol Service heading.")
    charge_rates = list(
        re.finditer(
            r"\bThe charge for such service shall be at the rate of "
            r"\$\s*(\d+(?:\.\d+)?)\s+per hour for each patrolling unit furnished\b",
            normalized_text,
            re.I,
        )
    )
    if len(charge_rates) != 1:
        raise ValueError(
            "Expected one Schedule E Patrol Service Charge rate, "
            f"found {len(charge_rates)}."
        )
    return [
        (
            "patrol_service_charge",
            float(charge_rates[0].group(1)),
            "usd_per_hour_unit",
            None,
            None,
            None,
            None,
            None,
            None,
        )
    ]


def month_range_season(start_month, end_month):
    return f"{start_month[:3].lower()}_{end_month[:3].lower()}"


def payment_component(rate, unit, label, component_type):
    if unit == "usd_per_kw" and re.search(
        r"\bPayment to the Customer\b", label, re.I
    ):
        return "capacity_payment", -abs(rate)
    return component_type, rate


def schedule_sgcm_components(schedule_text):
    normalized_text = " ".join(schedule_text.splitlines())
    if not re.search(
        r"\bIII\.\s*DETERMINATION OF PAYMENT TO CUSTOMER\b",
        normalized_text,
        re.I,
    ):
        raise ValueError("Missing Standby Generator payment section.")
    monthly_charge = list(
        re.finditer(
            r"\bmonthly charge of \$\s*(\d+(?:\.\d+)?) to cover costs associated "
            r"with metering facilities, meter reading and processing, "
            r"communication, and administration\b",
            normalized_text,
            re.I,
        )
    )
    if len(monthly_charge) != 1:
        raise ValueError(
            "Expected one Standby Generator monthly metering charge, "
            f"found {len(monthly_charge)}."
        )
    payment_specs = (
        ("November", "April"),
        ("May", "October"),
    )
    extracted = [
        (
            "monthly_charge",
            float(monthly_charge[0].group(1)),
            "usd_per_month",
            None,
            None,
            None,
            None,
            None,
            None,
        )
    ]
    for start_month, end_month in payment_specs:
        payment_rate = list(
            re.finditer(
                rf"\bFor the billing months of {start_month}\s+through "
                rf"{end_month},\s+the Customer shall be paid "
                rf"\$\s*(\d+(?:\.\d+)?)\s+per kW\b",
                normalized_text,
                re.I,
            )
        )
        if len(payment_rate) != 1:
            raise ValueError(
                f"Expected one Standby Generator payment for "
                f"{start_month} through {end_month}, found {len(payment_rate)}."
            )
        rate = float(payment_rate[0].group(1))
        component_type, rate = payment_component(
            rate, "usd_per_kw", "Payment to the Customer", "capacity_payment"
        )
        extracted.append(
            (
                component_type,
                rate,
                "usd_per_kw",
                None,
                month_range_season(start_month, end_month),
                None,
                None,
                None,
                None,
            )
        )
    return extracted


cur.execute(
    """
    SELECT COUNT(*) FROM tariff_components WHERE tariff_version_id = ?
    """,
    (tariff_version_id,),
)
component_count = cur.fetchone()[0]

if component_count == 0:
    schedule_text = "\n".join(page_text for _, page_text in matching_pages)
    lines = schedule_text.splitlines()
    roman_heading = re.compile(r"^\s*([IVXLCDM]+)\.\s", re.I)
    rate_start = None
    rate_section_number = None
    for index, line in enumerate(lines):
        heading = roman_heading.match(line)
        if heading and re.search(r"\bRATE\b", line, re.I):
            rate_start = index
            rate_section_number = heading.group(1).upper()
            break
    if rate_start is None or rate_section_number is None:
        if args.schedule_code == "C":
            extracted_components = schedule_c_components(schedule_text)
            rate_lines = []
        elif args.schedule_code == "E":
            extracted_components = schedule_e_components(schedule_text)
            rate_lines = []
        elif args.schedule_code in {"SGCM", "SGCM-1"}:
            extracted_components = schedule_sgcm_components(schedule_text)
            rate_lines = []
        elif args.schedule_code == "B":
            extracted_components = schedule_b_components(schedule_text)
            for (
                component_type,
                rate,
                unit,
                variant,
                season,
                tier_min,
                tier_max,
                component_tier_unit,
                billing_class,
            ) in extracted_components:
                cur.execute(
                    """
                    INSERT INTO tariff_components(
                        tariff_version_id, component_type, billing_category, rate,
                        formula, unit, tier_min, tier_max, tier_unit, season, variant,
                        billing_class
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tariff_version_id,
                        component_type,
                        None,
                        rate,
                        None,
                        unit,
                        tier_min,
                        tier_max,
                        component_tier_unit,
                        season,
                        variant,
                        billing_class,
                    ),
                )
            conn.commit()
            print(f"{len(extracted_components)} components inserted")
            conn.close()
            raise SystemExit(0)
        else:
            for index, line in enumerate(lines):
                heading = roman_heading.match(line)
                heading_title = line[heading.end() :].strip() if heading else ""
                if (
                    heading
                    and heading_title
                    and heading_title == heading_title.upper()
                    and re.search(r"\bCHARGE\b", heading_title, re.I)
                ):
                    rate_start = index
                    rate_section_number = heading.group(1).upper()
                    break
            if rate_start is None or rate_section_number is None:
                print("no extractable rates — cross-reference schedule")
                conn.close()
                raise SystemExit(0)
            rate_end = next(
                (
                    index
                    for index in range(rate_start + 1, len(lines))
                    if (
                        (heading := roman_heading.match(lines[index]))
                        and heading.group(1).upper() != rate_section_number
                        and lines[index][heading.end() :].strip()
                        == lines[index][heading.end() :].strip().upper()
                    )
                ),
                None,
            )
            if rate_end is None:
                raise ValueError(
                    f"No end to charge section for schedule {args.schedule_code}"
                )
            rate_lines = lines[rate_start:rate_end]
            extracted_components = []
    else:
        rate_end = next(
            (
                index
                for index in range(rate_start + 1, len(lines))
                if (
                    (heading := roman_heading.match(lines[index]))
                    and heading.group(1).upper() != rate_section_number
                )
            ),
            None,
        )
        if rate_end is None:
            raise ValueError(
                f"No end to rate section for schedule {args.schedule_code}"
            )

        rate_lines = lines[rate_start:rate_end]
        extracted_components = []
    if args.schedule_code == "D" and rate_lines:
        extracted_components = schedule_d_components("\n".join(rate_lines))
    pending_label = ""
    current_billing_class = None
    current_season = None
    current_variant = None
    tier_total = 0
    tier_unit = None
    pending_tier_prefix = None
    pending_billing_month = None
    payment_label = ""
    charge_anchor = None
    amount_pattern = re.compile(
        r"(?P<open>\()?(?:\$(?P<dollar>\d+(?:\.\d+)?)|"
        r"(?P<cents>\d+(?:\.\d+)?)\s*¢)(?(open)\))"
    )

    def clean_label(label):
        label = re.sub(r"^(?:\d+|[A-Za-z])[.)]\s*", "", label.strip())
        label = re.sub(r"^Plus\s+", "", label, flags=re.I)
        label = re.split(r"\bper\b", label, maxsplit=1, flags=re.I)[0]
        return label.rstrip(":").strip()

    def tier_definition(phrase):
        phrase = phrase.strip()
        patterns = (
            (
                re.match(
                    r"^(First|Next)\s+([\d,]+)\s+kWh\s+per\s+kW\b",
                    phrase,
                    re.I,
                ),
                "kwh_per_kw",
            ),
            (
                re.match(r"^(First|Next)\s+([\d,]+)\s+kW\b", phrase, re.I),
                "kw",
            ),
            (
                re.match(
                    r"^(First|Next)\s+([\d,]+)\s+(?:ES\s+)?kWh\b",
                    phrase,
                    re.I,
                ),
                "kwh",
            ),
        )
        for match, unit in patterns:
            if match:
                return match, unit
        if re.match(r"^Additional\b", phrase, re.I):
            return "additional", None
        return None, None

    is_lighting_schedule = args.schedule_code in {"150", "151", "SMH", "SSL"}
    if is_lighting_schedule:
        fixture = None
        table_mode = None
        current_kind = None
        in_pole_adders = False
        table_header_lines = []
        table_active = False
        ssl_item_context = ""
        ssl_current_kind = None
        ssl_table_active = False
        ssl_header_lines = []
        lighting_row = re.compile(
            r"^([\d,]+)\s+(.*?)\s+"
            r"\$\s*([\d,]+\.\d+)\s+\$\s*([\d,]+\.\d+)$"
        )
        pole_adder = re.compile(
            r"^[a-z]\.\s*(.+?)\s+\$\s*([\d,]+\.\d+)$",
            re.I,
        )

        for line in rate_lines[1:]:
            stripped_line = line.strip()
            if args.schedule_code == "SSL":
                numbered_heading = re.match(r"^\d+\.\s*(.*)$", stripped_line)
                if numbered_heading:
                    ssl_item_context = numbered_heading.group(1)
                    ssl_current_kind = (
                        "distribution_service_charge"
                        if re.search(r"\bDistribution\b", ssl_item_context, re.I)
                        else "es_service_charge"
                        if re.search(r"\bElectricity Supply\b", ssl_item_context, re.I)
                        else None
                    )
                    ssl_header_lines = []
                    ssl_table_active = False
                    continue

                if ssl_current_kind is None:
                    continue

                ssl_header_lines.append(stripped_line)
                ssl_header_lines = ssl_header_lines[-10:]
                ssl_header = " ".join(ssl_header_lines)
                if (
                    re.search(r"\bRate Tier\b", ssl_header, re.I)
                    and re.search(r"\bOperating\b", ssl_header, re.I)
                    and re.search(r"\bRange\b", ssl_header, re.I)
                ):
                    ssl_table_active = True

                tier_row = re.match(
                    r"^(\d+)\s+([\d,]+)\s*[-–−‒]\s*([\d,]+)\s+"
                    r"([\d,]+)\s+\$\s*([\d,]+\.\d+)$",
                    stripped_line,
                )
                if tier_row and ssl_table_active:
                    tier_number = tier_row.group(1)
                    distribution_line = None
                    if ssl_current_kind == "distribution_service_charge":
                        distribution_line = (
                            "premium"
                            if re.search(r"\bPremium\b", ssl_item_context, re.I)
                            else "basic"
                        )
                    variant = (
                        f"{distribution_line}_tier_{tier_number}"
                        if distribution_line is not None
                        else f"tier_{tier_number}"
                    )
                    extracted_components.append(
                        (
                            ssl_current_kind,
                            float(tier_row.group(5).replace(",", "")),
                            "usd_per_luminaire",
                            variant,
                            None,
                            None,
                            None,
                            None,
                            None,
                        )
                    )
                    continue

                ssl_item_context = f"{ssl_item_context} {stripped_line}".strip()
                continue

            fixture_match = re.search(r"\bType\s+(\d+)\s+fixtures\b", stripped_line, re.I)
            if fixture_match:
                fixture = f"type_{fixture_match.group(1)}"
                table_mode = "kind_pair" if fixture == "type_1" else "position"

            numbered_heading = re.match(r"^(\d+)\.\s*(.*)$", stripped_line)
            if numbered_heading:
                heading_number = numbered_heading.group(1)
                heading_text = numbered_heading.group(2)
                table_header_lines = []
                table_active = False
                if re.search(r"\bDistribution Service Charge\b", heading_text, re.I):
                    current_kind = "distribution_service_charge"
                elif re.search(
                    r"\bElectricity Supply Service Charge\b", heading_text, re.I
                ):
                    current_kind = "es_service_charge"
                else:
                    current_kind = None
                if current_kind is not None:
                    table_header_lines = []
                    table_active = False
                    if args.schedule_code == "SMH":
                        fixture = ""
                        table_mode = "position"
                    continue
                if heading_number == "5" and re.search(
                    r"Additional Distribution Service Charges", heading_text, re.I
                ):
                    in_pole_adders = True
                    fixture = None
                    current_kind = None
                    continue
                in_pole_adders = False
                current_kind = None
                if fixture_match:
                    continue
                elif args.schedule_code == "151" and heading_number == "1":
                    fixture = "mercury_vapor"
                    table_mode = "kind_pair"
                elif args.schedule_code == "151" and heading_number == "2":
                    fixture = "urbanlite"
                    table_mode = "kind_pair"
                elif heading_number == "3" and re.search(
                    r"Directional Lighting Service", heading_text, re.I
                ):
                    fixture = "directional"
                    table_mode = "position"
                elif args.schedule_code == "151" and heading_number == "4":
                    fixture = "incandescent"
                    table_mode = "kind_pair"
                elif heading_number == "4" and re.search(
                    r"Expressway", heading_text, re.I
                ):
                    fixture = "expressway"
                    table_mode = "position"
                continue

            if in_pole_adders:
                adder_match = pole_adder.match(stripped_line)
                if adder_match:
                    variant = re.sub(
                        r"[\s-]+", "_", adder_match.group(1).lower()
                    )
                    extracted_components.append(
                        (
                            "distribution_service_charge",
                            float(adder_match.group(2).replace(",", "")),
                            "usd_per_luminaire",
                            variant,
                            None,
                            None,
                            None,
                            None,
                            None,
                        )
                    )
                continue

            if re.match(r"^[a-z]\.\s", stripped_line, re.I):
                table_header_lines = []
                table_active = False
                if re.search(r"\bDistribution Service Charge\b", stripped_line, re.I):
                    current_kind = "distribution_service_charge"
                elif re.search(
                    r"\bElectricity Supply Service Charge\b",
                    stripped_line,
                    re.I,
                ):
                    current_kind = "es_service_charge"
            elif re.fullmatch(r"Distribution Service Charge", stripped_line, re.I):
                current_kind = "distribution_service_charge"
            elif re.fullmatch(
                r"Electricity Supply Service Charge", stripped_line, re.I
            ):
                current_kind = "es_service_charge"

            table_header_lines.append(stripped_line)
            table_header_lines = table_header_lines[-8:]
            table_header = " ".join(table_header_lines)
            if (
                re.search(r"\bLumens\b", table_header, re.I)
                and re.search(r"\bPer Unit Per Month\b", table_header, re.I)
                and (
                    (
                        re.search(r"\bFirst Unit\b", table_header, re.I)
                        and re.search(r"\bPer Pole\b", table_header, re.I)
                    )
                    or (
                        re.search(r"\bDistribution\b", table_header, re.I)
                        and re.search(r"\bElectricity Supply\b", table_header, re.I)
                    )
                )
            ):
                table_active = True
                if re.search(r"\bFirst Unit\b", table_header, re.I):
                    table_mode = "position"
                else:
                    table_mode = "kind_pair"

            row_match = lighting_row.match(stripped_line)
            if not row_match or fixture is None:
                continue

            lumens = row_match.group(1).replace(",", "")
            attributes = row_match.group(2).split()
            wattage_index = next(
                (
                    index
                    for index, token in enumerate(attributes)
                    if re.fullmatch(r"[\d,]+", token)
                ),
                None,
            )
            if wattage_index is None:
                raise ValueError(f"Unrecognized lighting row for schedule {args.schedule_code}: {line}")
            medium = "_".join(token.lower() for token in attributes[:wattage_index])
            first_rate = float(row_match.group(3).replace(",", ""))
            second_rate = float(row_match.group(4).replace(",", ""))
            fixture_variant = fixture
            if medium and medium != fixture:
                fixture_variant = f"{fixture}_{medium}".strip("_")
            if table_mode == "kind_pair":
                rate_rows = (
                    ("distribution_service_charge", first_rate),
                    ("es_service_charge", second_rate),
                )
                for component_type, rate in rate_rows:
                    extracted_components.append(
                        (
                            component_type,
                            rate,
                            "usd_per_luminaire",
                            "_".join(part for part in (fixture_variant, lumens) if part),
                            None,
                            None,
                            None,
                            None,
                            None,
                        )
                    )
            elif current_kind is not None:
                for position, rate in (
                    ("first", first_rate),
                    ("additional", second_rate),
                ):
                    extracted_components.append(
                        (
                            current_kind,
                            rate,
                            "usd_per_luminaire",
                            "_".join(
                                part
                                for part in (fixture_variant, position, lumens)
                                if part
                            ),
                            None,
                            None,
                            None,
                            None,
                            None,
                        )
                    )

    for line in (
        rate_lines[1:]
        if not is_lighting_schedule and args.schedule_code != "D"
        else []
    ):
        stripped_line = line.strip()
        if not stripped_line:
            continue

        paragraph_heading = re.match(r"^[A-Z]\.\s*(.*)$", stripped_line)
        if paragraph_heading:
            payment_label = (
                paragraph_heading.group(1)
                if re.search(
                    r"\bPayment to the Customer\b",
                    paragraph_heading.group(1),
                    re.I,
                )
                else ""
            )

        billing_month_span = re.search(
            r"\b(?:billing months of\s+)?([A-Za-z]+)\s+through\s+"
            r"([A-Za-z]+)\b",
            stripped_line,
            re.I,
        )
        if billing_month_span:
            current_season = month_range_season(
                billing_month_span.group(1), billing_month_span.group(2)
            )
            pending_billing_month = None
            tier_total = 0
            tier_unit = None
        elif (
            billing_month_start := re.search(
                r"\bbilling months of\s+([A-Za-z]+)\s*$",
                stripped_line,
                re.I,
            )
        ):
            pending_billing_month = billing_month_start.group(1)
        elif pending_billing_month is not None:
            continuation = re.match(r"^\s*through\s+([A-Za-z]+)\b", stripped_line, re.I)
            if continuation:
                current_season = month_range_season(
                    pending_billing_month, continuation.group(1)
                )
                tier_total = 0
                tier_unit = None
            pending_billing_month = None

        amount_match = amount_pattern.search(stripped_line)
        period_match = re.search(
            r"\bFor the period\s+([A-Za-z]+)\s+\d+\s+through\s+"
            r"([A-Za-z]+)\s+\d+\b",
            stripped_line,
            re.I,
        )
        if period_match:
            pending_tier_prefix = None
            current_season = (
                month_range_season(period_match.group(1), period_match.group(2))
            )
            tier_total = 0
            tier_unit = None
            continue

        if amount_pattern.search(stripped_line) is None:
            if re.search(r"\bPrimary Voltage\b", stripped_line, re.I):
                pending_tier_prefix = None
                current_variant = "primary"
                tier_total = 0
                tier_unit = None
                continue
            if re.search(r"\bSecondary Voltage\b", stripped_line, re.I):
                pending_tier_prefix = None
                current_variant = "secondary"
                tier_total = 0
                tier_unit = None
                continue

        if re.match(r"^[A-Z]\.\s", stripped_line):
            pending_tier_prefix = None
            current_variant = None
            if re.search(r"\bNon-Demand\b", stripped_line, re.I):
                current_billing_class = "non_demand"
                current_season = None
                tier_total = 0
                tier_unit = None
                pending_label = ""
            elif re.search(r"\bDemand\b", stripped_line, re.I):
                current_billing_class = "demand"
                current_season = None
                tier_total = 0
                tier_unit = None
                pending_label = ""
            elif re.search(r"\bbilling months\b", stripped_line, re.I):
                if re.search(r"\bsummer\b", stripped_line, re.I):
                    current_season = "summer"
                    tier_total = 0
                    tier_unit = None
                elif re.search(r"\bbase\b", stripped_line, re.I):
                    current_season = "base"
                    tier_total = 0
                    tier_unit = None
            continue

        if re.search(r"\bbilling months\b", stripped_line, re.I):
            if re.search(r"\bsummer\b", stripped_line, re.I):
                pending_tier_prefix = None
                current_season = "summer"
                tier_total = 0
                tier_unit = None
                continue
            if re.search(r"\bbase\b", stripped_line, re.I):
                pending_tier_prefix = None
                current_season = "base"
                tier_total = 0
                tier_unit = None
                continue

        if amount_match is None:
            if re.match(r"^(?:First|Next|Additional)\b", stripped_line, re.I):
                pending_tier_prefix = (
                    f"{pending_tier_prefix} {stripped_line}".strip()
                    if pending_tier_prefix
                    else stripped_line
                )
                continue

            pending_tier_prefix = None
            if re.match(r"^\d+[.)]\s", stripped_line):
                current_variant = None
                tier_total = 0
                tier_unit = None
            pending_label = clean_label(stripped_line)
            numbered_heading = re.match(r"^\d+[.)]\s*(.+)$", stripped_line)
            if numbered_heading and re.search(r"\bCharge\b", numbered_heading.group(1), re.I):
                charge_anchor = clean_label(numbered_heading.group(1))
            continue

        if pending_tier_prefix is not None:
            stripped_line = f"{pending_tier_prefix} {stripped_line}"
            pending_tier_prefix = None
            amount_match = amount_pattern.search(stripped_line)

        inline_label = stripped_line[: amount_match.start()].strip()
        matrix_amounts = list(
            re.finditer(r"(\d+(?:\.\d+)?)\s*¢", stripped_line)
        )
        matrix_row = re.match(r"^([ABC])\b", stripped_line, re.I)
        if matrix_row and len(matrix_amounts) == 2:
            if charge_anchor is None or current_season is None:
                raise ValueError(
                    f"Incomplete energy matrix context for schedule "
                    f"{args.schedule_code}: {line}"
                )
            matrix_class = matrix_row.group(1).lower()
            component_type = re.sub(r"[\s-]+", "_", charge_anchor.lower())
            component_type = component_type.replace("electricity_supply", "es")
            if component_type.endswith("_energy_charge"):
                component_type = component_type[: -len("_charge")]
            for peak, match in zip(("on_peak", "off_peak"), matrix_amounts):
                matrix_rate = round(float(match.group(1)) / 100, 6)
                extracted_components.append(
                    (
                        component_type,
                        matrix_rate,
                        "usd_per_kwh",
                        f"{matrix_class}_{peak}",
                        current_season,
                        None,
                        None,
                        None,
                        current_billing_class,
                    )
                )
            continue

        tier_phrase = inline_label.split("@", 1)[0].strip()
        tier_match, tier_kind = tier_definition(tier_phrase)
        label = (
            pending_label
            if not inline_label
            or "@" in inline_label
            or re.search(r"\bFor\s+(?:Unmetered|Metered)\b", inline_label, re.I)
            or re.match(r"^All\b", inline_label, re.I)
            or tier_match is not None
            else clean_label(inline_label)
        )
        dollar_amount = amount_match.group("dollar")
        cents_amount = amount_match.group("cents")
        currency = "$" if dollar_amount is not None else "¢"
        amount_text = dollar_amount if dollar_amount is not None else cents_amount
        amount = float(amount_text)
        if currency == "¢":
            amount = round(amount / 100, 6)
            unit = "usd_per_kwh"
        elif re.search(r"\bper\s+rkVA\b", stripped_line, re.I):
            unit = "usd_per_rkva"
        elif re.search(r"\bper\s+kW\b", stripped_line, re.I):
            unit = "usd_per_kw"
        elif re.search(r"\bper\s+service\b", stripped_line, re.I):
            unit = "usd_per_service"
        elif re.search(r"\bper\s+instance\b", stripped_line, re.I):
            unit = "usd_per_service"
        elif re.search(r"\bper\s+luminaire\b", stripped_line, re.I):
            unit = "usd_per_luminaire"
        elif re.search(r"\bper\s+hour\b", stripped_line, re.I):
            unit = "usd_per_hour_unit"
        elif re.search(r"\bper\s+Delivery Point\b", stripped_line, re.I):
            unit = "usd_per_month"
        elif re.search(r"\bper\s+Billing Month\s+per Meter\b", stripped_line, re.I):
            unit = "usd_per_unit_month"
        elif re.search(r"\bper\s+Billing Month\b", stripped_line, re.I):
            unit = "usd_per_month"
        elif re.search(r"\bper\s+kWh\b", stripped_line, re.I):
            unit = "usd_per_kwh"
        elif re.sub(r"[\s-]+", "_", label.lower()) == "basic_customer_charge":
            unit = (
                "usd_per_unit_month"
                if re.search(r"\bMetered\b", inline_label, re.I)
                else "usd_per_month"
            )
        else:
            raise ValueError(f"Unrecognized unit for schedule {args.schedule_code}: {line}")

        if amount_match.group("open") is not None:
            amount = -amount

        component_type = re.sub(r"[\s-]+", "_", label.lower())
        component_type = component_type.replace("electricity_supply", "es")
        if component_type.endswith("_energy_charge"):
            component_type = component_type[: -len("_charge")]
        component_type, amount = payment_component(
            amount,
            unit,
            f"{payment_label} {label}",
            component_type,
        )

        if tier_match and tier_match != "additional":
            tier_size = float(tier_match.group(2).replace(",", ""))
            tier_min = 0 if tier_match.group(1).lower() == "first" else tier_total
            tier_max = tier_min + tier_size
            tier_total = tier_max
            tier_unit = tier_kind
        elif tier_match == "additional" and tier_unit is not None:
            tier_min = tier_total
            tier_max = None
        else:
            tier_min = None
            tier_max = None
            tier_unit = None
            tier_total = 0

        if re.search(r"\b(?:On|Off)-peak\b", label, re.I):
            variant = None
        elif current_variant is not None:
            variant = current_variant
        elif re.search(r"\bUnmetered\b", inline_label, re.I):
            variant = "unmetered"
        elif re.search(r"\bMetered\b", inline_label, re.I):
            variant = "metered"
        elif re.search(r"\bOn-peak\b", stripped_line, re.I):
            variant = "on_peak"
        elif re.search(r"\bOff-peak\b", stripped_line, re.I):
            variant = "off_peak"
        else:
            variant = None

        extracted_components.append(
            (
                component_type,
                amount,
                unit,
                variant,
                current_season,
                tier_min,
                tier_max,
                tier_unit,
                current_billing_class,
            )
        )

    for component in extracted_components:
        print(*component)

    for (
        component_type,
        rate,
        unit,
        variant,
        season,
        tier_min,
        tier_max,
        component_tier_unit,
        billing_class,
    ) in extracted_components:
        cur.execute(
            """
            INSERT INTO tariff_components(
                tariff_version_id, component_type, billing_category, rate,
                formula, unit, tier_min, tier_max, tier_unit, season, variant,
                billing_class
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tariff_version_id,
                component_type,
                None,
                rate,
                None,
                unit,
                tier_min,
                tier_max,
                component_tier_unit,
                season,
                variant,
                billing_class,
            ),
        )

    conn.commit()
    print(f"{len(extracted_components)} components inserted")

conn.close()
