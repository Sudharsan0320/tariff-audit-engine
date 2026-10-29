from pathlib import Path

import pytest

from src.parse_account_profile import extract_account_profile, parse_profile_text


PROFILE_TEXT = """
Account Profile
COUNTY OF EXAMPLE
Phone Number +18045550100
Mailing Address EXAMPLE VA 23124-0110
Customer Class Virginia County/Muni
ACCOUNT NO. 008980201225
Service Address 7365 EXAMPLE RD
EXAMPLE VA 23124-2471
BALL FIELD
Turn On Date January 28, 2008
District Office 0600-East Richmond
Tax District
NAICS Code
Meter Number(s) 294509988
Current Rate VE-154
Voltage Secondary
Delivery Phase One-Phase
Minimum Demand N/A
Facility Charge N/A
Billing Status Active
"""

REAL_PDF = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "raw"
    / "bills"
    / "8980201225  EAP Report.pdf"
)


def test_parses_native_profile_and_normalizes_current_rate():
    profile = parse_profile_text(PROFILE_TEXT, "008980201225")

    assert profile["account_name"] == "COUNTY OF EXAMPLE"
    assert profile["account_number"] == "008980201225"
    assert profile["service_address"] == (
        "7365 EXAMPLE RD EXAMPLE VA 23124-2471 BALL FIELD"
    )
    assert profile["turn_on_date"] == "2008-01-28"
    assert profile["current_rate_raw"] == "VE-154"
    assert profile["schedule_code"] == "154"
    assert profile["billing_status"] == "Active"


def test_two_witness_account_number_must_match():
    with pytest.raises(ValueError, match="Account number mismatch"):
        parse_profile_text(PROFILE_TEXT, "000000000000")


@pytest.mark.skipif(not REAL_PDF.exists(), reason="local EAP PDF is not in data/raw/bills")
def test_extracts_real_eap_fixture():
    profile = extract_account_profile(REAL_PDF)

    assert profile["account_number"] == "008980201225"
    assert profile["current_rate_raw"] == "VE-154"
    assert profile["schedule_code"] == "154"
    assert "SOFTBALL FIELD" in profile["service_address"]
