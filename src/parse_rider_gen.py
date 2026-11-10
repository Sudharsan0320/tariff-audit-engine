import pdfplumber
import re
from pathlib import Path

try:
    from src import db
    from src.citations import table_cell
except ImportError:
    import db
    from citations import table_cell

RIDER_CODE = "GEN"
UTILITY = "Virginia Electric and Power Company"
STATE = "VA"
SCHEDULE_CODE = "RIDER GEN"
VERSION = 1
EFFECTIVE_FROM = "2025-04-01"


def parse_rider_gen(pdf_path=None, database_path=None):
    """Parse the Dominion GEN rider PDF into tariff components.
    
    Returns (version_id, component_count) to match the parse_rider contract.
    """
    if pdf_path is None:
        ROOT = Path(__file__).resolve().parent.parent
        pdf_path = ROOT / "data" / "raw" / "rider-gen.pdf"
    if database_path is None:
        database_path = db.default_target("data/tariffs.db")
    
    with pdfplumber.open(pdf_path) as pdf:
        tables = pdf.pages[0].extract_tables()
        if not tables or not tables[0]:
            raise ValueError("No tables found in GEN rider PDF")

    version_values = (STATE, UTILITY, SCHEDULE_CODE, VERSION, EFFECTIVE_FROM)
    
    with db.connect(database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing_version = conn.execute(
                """
                SELECT id, status FROM tariff_versions
                WHERE state = ? AND utility = ? AND schedule_code = ?
                  AND version = ? AND effective_from = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                version_values,
            ).fetchone()
            
            if existing_version is not None and existing_version[1] != "draft":
                conn.commit()
                return existing_version[0], 0
            
            if existing_version is None:
                cur = conn.execute(
                    """
                    INSERT INTO tariff_versions(
                        state, utility, schedule_code, version, effective_from, status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (STATE, UTILITY, SCHEDULE_CODE, VERSION, EFFECTIVE_FROM, "draft"),
                )
                tariff_version_id = cur.lastrowid
            else:
                tariff_version_id = existing_version[0]
            
            component_count = conn.execute(
                "SELECT COUNT(*) FROM tariff_components WHERE tariff_version_id = ?",
                (tariff_version_id,),
            ).fetchone()[0]
            
            if component_count == 0:
                for row in tables[0][1:]:
                    schedule = row[0]
                    for component_type, rate_column, unit, divisor in (
                        ("supply", 1, "usd_per_kwh", 100),
                        ("demand", 4, "usd_per_kw", 1),
                    ):
                        rate_cell = row[rate_column]
                        if rate_cell is None or not rate_cell.strip():
                            continue
                        
                        match = re.fullmatch(
                            r"\$?(\d+(?:\.\d+)?)(?:¢/kWh|/kW)(?:\d+(?:,\d+)*)?",
                            rate_cell,
                        )
                        if match is None:
                            raise ValueError(f"Unrecognized rate for {schedule}: {rate_cell}")
                        
                        conn.execute(
                            """
                            INSERT INTO tariff_components(
                                tariff_version_id, component_type, billing_category, rate,
                                formula, unit, source_page, source_cell
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                tariff_version_id,
                                component_type,
                                schedule,
                                float(match.group(1)) / divisor,
                                None,
                                unit,
                                1,
                                table_cell(schedule, tables[0][0][rate_column], rate_cell),
                            ),
                        )
                component_count = conn.execute(
                    "SELECT COUNT(*) FROM tariff_components WHERE tariff_version_id = ?",
                    (tariff_version_id,),
                ).fetchone()[0]
            
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    
    return tariff_version_id, component_count


if __name__ == "__main__":
    version_id, count = parse_rider_gen()
    print(f"Parsed GEN rider: version {version_id}, {count} components")
