import pytest

from src.normalize_schedule import normalize_schedule_code


def test_strips_ve_prefix():
    assert normalize_schedule_code("VE-110") == "110"


def test_accepts_already_normalized_codes():
    assert normalize_schedule_code("154") == "154"
    assert normalize_schedule_code("SGCM-1") == "SGCM-1"


def test_rejects_empty_values():
    with pytest.raises(ValueError):
        normalize_schedule_code("   ")
    with pytest.raises(ValueError):
        normalize_schedule_code("VE-")
