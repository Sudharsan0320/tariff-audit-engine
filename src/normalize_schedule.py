import re


VE_PREFIX = re.compile(r"^VE-", re.IGNORECASE)


def normalize_schedule_code(raw):
    """Map a bill-side rate code onto a tariff schedule_code (VE-110 -> 110)."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Schedule code is empty.")
    code = raw.strip().upper()
    code = VE_PREFIX.sub("", code, count=1).strip()
    if not code:
        raise ValueError(f"Schedule code {raw!r} is empty after VE- normalization.")
    return code
