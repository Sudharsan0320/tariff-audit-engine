from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sqlite3

import pytest

from src.ingest_usage_history import load_usage_draft
from src.parse_usage import _extract_history, summarize_draft
from src.validate_usage import validate_usage_draft


def make_connection():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tariff_versions (
            id INTEGER PRIMARY KEY,
            schedule_code TEXT NOT NULL,
            effective_from DATE NOT NULL,
            effective_to DATE,
            status TEXT NOT NULL
        );
        CREATE TABLE accounts (
            account_number TEXT PRIMARY KEY,
            customer_name TEXT,
            service_address TEXT,
            meter_number TEXT,
            customer_class TEXT,
            voltage TEXT,
            delivery_phase TEXT,
            turn_on_date DATE,
            current_rate_raw TEXT
        );
        CREATE TABLE bills (
            id INTEGER PRIMARY KEY,
            account_number TEXT NOT NULL,
            bill_from DATE NOT NULL,
            bill_to DATE NOT NULL,
            billing_days INTEGER,
            billed_rate_raw TEXT,
            schedule_code TEXT,
            total_consumption_kwh REAL,
            demand_kw REAL,
            total_billed NUMERIC,
            source_file TEXT NOT NULL,
            history_year_status TEXT NOT NULL DEFAULT 'complete',
            FOREIGN KEY (account_number) REFERENCES accounts(account_number),
            UNIQUE (account_number, bill_from, bill_to)
        );
        CREATE TABLE bill_charges (
            id INTEGER PRIMARY KEY,
            bill_id INTEGER NOT NULL,
            line_label TEXT NOT NULL,
            amount NUMERIC NOT NULL,
            FOREIGN KEY (bill_id) REFERENCES bills(id)
        );
        INSERT INTO tariff_versions VALUES
            (1, '110', '2025-07-01', NULL, 'active'),
            (2, '120', '2025-07-01', NULL, 'active');
        """
    )
    return conn


def valid_draft():
    return {
        "account": {
            "account_number": "0000123",
            "account_name": "Example County",
            "service_address": "1 Main St",
            "meter_number": "12345",
            "customer_class": "County",
            "voltage": "Secondary",
            "delivery_phase": "Three-Phase",
            "turn_on_date": "2001-07-18",
            "current_rate_raw": "VE-110",
        },
        "annual_summaries": [
            {
                "year": 2025,
                "total_billed": Decimal("10.00"),
                "total_usage_kwh": 5,
            }
        ],
        "bills": [
            {
                "account_number": "0000123",
                "history_year": 2025,
                "month": 7,
                "bill_from": "2025-07-01",
                "bill_to": "2025-07-31",
                "billing_days": 31,
                "billed_rate_raw": "VE-110",
                "schedule_code": "110",
                "total_consumption_kwh": Decimal(5),
                "demand_kw": None,
                "bill_summary_subtotal": Decimal("10.00"),
                "total_billed": Decimal("10.00"),
                "source_file": "profile.pdf",
                "bill_charges": [
                    {"line_label": "Energy Charges", "amount": Decimal("10.00")}
                ],
            }
        ],
    }


def _failure_containing(failures, text):
    return any(text in failure for failure in failures)


def test_coordinate_pivot_keeps_sparse_values_in_their_month_column():
    def word(text, x0, x1, top, fontname="ArialMT"):
        return {
            "text": text,
            "x0": x0,
            "x1": x1,
            "top": top,
            "fontname": fontname,
        }

    words = [
        word("Historical", 10, 55, 10),
        word("Electricity", 58, 95, 10),
        word("Usage", 98, 125, 10),
        word("-2025", 128, 155, 10),
        word("JUL", 153, 167, 30),
        word("AUG", 193, 207, 30),
        word("Bill", 10, 30, 50),
        word("From", 33, 55, 50),
        word("07/01/25", 143, 177, 50),
        word("08/01/25", 183, 217, 50),
        word("Bill", 10, 30, 70),
        word("To", 33, 45, 70),
        word("07/31/25", 143, 177, 70),
        word("08/31/25", 183, 217, 70),
        word("Billing", 10, 45, 90),
        word("Days", 48, 70, 90),
        word("31", 153, 167, 90),
        word("31", 193, 207, 90),
        word("Billed", 10, 40, 110),
        word("Rate", 43, 62, 110),
        word("VE-110", 143, 177, 110),
        word("VE-110", 183, 217, 110),
        word("Total", 10, 37, 130),
        word("Consumption", 40, 105, 130),
        word("5", 153, 167, 130),
        word("6", 193, 207, 130),
        word("Bill", 10, 30, 150),
        word("Summary", 33, 78, 150),
        word("Rider", 10, 40, 170),
        word("GEN", 43, 68, 170),
        word("$1.25", 183, 217, 170),
        word("Energy", 10, 37, 180, "Arial-BoldMT"),
        word("Charges", 40, 85, 180, "Arial-BoldMT"),
        word("$1.25", 183, 217, 180),
        word("*", 10, 15, 190, "Arial-BoldMT"),
        word("Subtotal", 20, 70, 190, "Arial-BoldMT"),
        word("$1.25", 183, 217, 190),
        word("**", 10, 18, 210, "Arial-BoldMT"),
        word("Total", 20, 45, 210, "Arial-BoldMT"),
        word("Charges", 48, 85, 210, "Arial-BoldMT"),
        word("$1.25", 183, 217, 210),
        word("©", 10, 16, 230),
        word("2025", 18, 38, 230),
        word("-", 40, 45, 230),
        word("Dominion", 50, 90, 230),
        word("Energy", 92, 120, 230),
        word("Page", 130, 155, 230),
        word("4", 183, 187, 230),
        word("of", 191, 202, 230),
        word("6", 203, 210, 230),
    ]

    class Page:
        def extract_words(self, extra_attrs=None):
            return words

    class Pdf:
        pages = [Page()]

    sections, bills = _extract_history(Pdf())

    assert len(sections) == 1
    assert [bill["month"] for bill in bills] == [7, 8]
    assert bills[0]["bill_charges"] == []
    assert bills[1]["bill_charges"] == [
        {"line_label": "Rider GEN", "amount": Decimal("1.25")}
    ]
    assert bills[1]["bill_summary_rollups"] == [
        {"line_label": "Energy Charges", "amount": Decimal("1.25")}
    ]
    assert bills[1]["bill_summary_subtotal"] == Decimal("1.25")
    assert bills[1]["total_billed"] == Decimal("1.25")


def test_valid_draft_passes_and_reports_uncovered_period_warning():
    conn = make_connection()
    draft = valid_draft()
    draft["bills"][0].update(
        bill_from="2025-06-01",
        bill_to="2025-06-30",
        billing_days=30,
    )
    result = validate_usage_draft(draft, conn)

    assert result["failures"] == []
    assert any("audit not yet possible" in warning for warning in result["warnings"])


def test_negative_charge_is_informational_but_negative_usage_fails():
    draft = valid_draft()
    draft["bills"][0]["bill_charges"] = [
        {"line_label": "Rider PPA", "amount": Decimal("-1.00")},
        {"line_label": "Energy Charges", "amount": Decimal("11.00")},
    ]
    result = validate_usage_draft(draft, make_connection())
    assert result["failures"] == []
    assert any("negative as-printed charge -1.00" in item for item in result["information"])

    draft["bills"][0]["total_consumption_kwh"] = Decimal(-1)
    result = validate_usage_draft(draft, make_connection())
    assert _failure_containing(result["failures"], "total_consumption_kwh must be non-negative")


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("demand_kw", "demand_kw must be non-negative"),
        ("total_billed", "total_billed must be non-negative"),
    ],
)
def test_v3_still_rejects_negative_demand_and_bill_total(field, expected):
    draft = valid_draft()
    draft["bills"][0][field] = Decimal(-1)

    result = validate_usage_draft(draft, make_connection())

    assert _failure_containing(result["failures"], expected)


def test_empty_draft_fails():
    draft = valid_draft()
    draft["bills"] = []

    result = validate_usage_draft(draft, make_connection())

    assert _failure_containing(result["failures"], "no monthly bills were extracted")


def test_annual_witness_mismatch_is_reported_but_nonblocking():
    draft = valid_draft()
    draft["annual_summaries"][0]["total_usage_kwh"] = 6
    draft["bills"][0]["history_year_status"] = "partial_source"

    result = validate_usage_draft(draft, make_connection())

    assert result["failures"] == []
    assert result["annual_variances"][0]["usage_delta_kwh"] == -1
    assert result["annual_variances"][0]["history_year_status"] == "partial_source"


def test_v7a_requires_detail_subtotal_and_total_to_agree():
    draft = valid_draft()
    draft["bills"][0]["bill_summary_subtotal"] = Decimal("9.00")

    result = validate_usage_draft(draft, make_connection())

    assert _failure_containing(result["failures"], "V7a detail lines sum to 10.00")
    assert _failure_containing(result["failures"], "V7a printed subtotal 9.00 differs")


def test_report_preserves_monthly_rate_sequence():
    draft = valid_draft()
    second_bill = deepcopy(draft["bills"][0])
    second_bill.update(
        month=8,
        bill_from="2025-08-01",
        bill_to="2025-08-31",
        billed_rate_raw="VE-120",
        schedule_code="120",
    )
    draft["bills"].append(second_bill)
    report = summarize_draft(draft)

    assert report["years"][2025]["rate_sequence"] == [
        {"month": 7, "billed_rate_raw": "VE-110"},
        {"month": 8, "billed_rate_raw": "VE-120"},
    ]


def test_parser_has_no_target_account_literals():
    source = (Path(__file__).resolve().parents[1] / "src" / "parse_usage.py").read_text()

    for literal in ("1172485896", "001172485896", "294832472", "COUNTY OF NEW KENT", "VE-120"):
        assert literal not in source


@pytest.mark.parametrize(
    ("corrupt", "expected"),
    [
        ("reversed", "bill_from must be before bill_to"),
        ("days", "billing_days 30 does not match"),
        ("dropped_charge", "V7a detail lines sum to 0"),
        ("duplicate", "duplicate billing period in draft"),
        ("unknown_rate", "unknown schedule_code '999'"),
        ("normalized_mismatch", "does not match billed rate"),
        ("negative_usage", "total_consumption_kwh must be non-negative"),
    ],
)
def test_corrupt_drafts_fail_with_the_specific_reason(corrupt, expected):
    draft = valid_draft()
    conn = make_connection()
    bill = draft["bills"][0]
    if corrupt == "reversed":
        bill["bill_from"], bill["bill_to"] = bill["bill_to"], bill["bill_from"]
    elif corrupt == "days":
        bill["billing_days"] = 30
    elif corrupt == "dropped_charge":
        bill["bill_charges"].clear()
    elif corrupt == "duplicate":
        draft["bills"].append(deepcopy(bill))
    elif corrupt == "unknown_rate":
        bill["billed_rate_raw"] = "VE-999"
    elif corrupt == "normalized_mismatch":
        bill["schedule_code"] = "120"
    elif corrupt == "negative_usage":
        bill["total_consumption_kwh"] = Decimal(-5)

    status, result, _ = load_usage_draft(conn, draft)

    assert status == "rejected"
    assert _failure_containing(result["failures"], expected)
    assert [
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("accounts", "bills", "bill_charges")
    ] == [0, 0, 0]


def test_gate_arbitrates_duplicate_basic_line_by_month_total():
    draft = valid_draft()
    bill = draft["bills"][0]
    bill["total_billed"] = Decimal("9.00")
    bill["bill_summary_subtotal"] = Decimal("9.00")
    bill["bill_charges"] = [
        {"line_label": "Basic Cust. Charges", "amount": Decimal("1.00")},
        {"line_label": "Basic Customer Chg", "amount": Decimal("1.00")},
        {"line_label": "Energy Charges", "amount": Decimal("8.00")},
    ]
    draft["annual_summaries"][0]["total_billed"] = Decimal("9.00")

    result = validate_usage_draft(draft, make_connection())

    assert result["failures"] == []
    assert result["basic_charge_resolution"] == "exclude_basic_customer_chg"
    assert any("V6 finding" in warning for warning in result["warnings"])


def test_failed_load_rolls_back_and_exact_reingest_is_noop():
    conn = make_connection()
    draft = valid_draft()
    draft["bills"][0]["history_year_status"] = "partial_source"
    status, validation, first_hash = load_usage_draft(conn, draft)
    assert status == "loaded"
    assert validation["failures"] == []
    assert conn.execute("SELECT history_year_status FROM bills").fetchone()[0] == "partial_source"

    before_failed_load = first_hash
    corrupted = deepcopy(draft)
    corrupted["bills"][0]["total_consumption_kwh"] = Decimal(-1)
    rejected_status, rejected, rejected_hash = load_usage_draft(conn, corrupted)
    assert rejected_status == "rejected"
    assert _failure_containing(rejected["failures"], "must be non-negative")
    assert rejected_hash == before_failed_load

    second_status, second_validation, second_hash = load_usage_draft(conn, draft)
    assert second_status == "no-op"
    assert second_validation["failures"] == []
    assert second_hash == first_hash
    assert [
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("accounts", "bills", "bill_charges")
    ] == [1, 1, 1]