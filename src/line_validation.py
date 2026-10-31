"""Line-item validation: one row per billed line, checked against the latest audit run.

A pure read over stored data. Flags: zero_expected_billed, unmapped_label,
rate_disagreement, amount_mismatch. Nothing is recomputed; the audit run's stored
expected amounts, rates and determinants are what the lines are checked against.
"""

from decimal import Decimal
import json

if __package__:
    from .audit_bills import DEFAULT_MAPPING_PATH, load_mapping
    from .schedule_review import FIXED_MONTHLY_UNITS
else:
    from audit_bills import DEFAULT_MAPPING_PATH, load_mapping
    from schedule_review import FIXED_MONTHLY_UNITS

OK = "ok"
ZERO_EXPECTED_BILLED = "zero_expected_billed"
UNMAPPED_LABEL = "unmapped_label"
RATE_DISAGREEMENT = "rate_disagreement"
AMOUNT_MISMATCH = "amount_mismatch"
FLAG_ORDER = (UNMAPPED_LABEL, ZERO_EXPECTED_BILLED, AMOUNT_MISMATCH, RATE_DISAGREEMENT)
CENT = Decimal("0.01")
HALF_CENT = Decimal("0.005")


class ValidationRefused(ValueError):
    """The bill cannot be validated from the stored data."""


class BillNotFound(ValidationRefused):
    """No such bill."""


class NoAuditRun(ValidationRefused):
    """The bill has no audit run."""


def _rows(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _decimal(value):
    return None if value is None or value == "" else Decimal(str(value))


def _detail(value):
    if isinstance(value, (str, bytes)):
        return json.loads(value)
    return value or {}


def _rate_flag(line, detail, component):
    """Implied unit rate vs the stored rate, within half a cent of the billed amount."""
    if component is None or component["rate"] is None:
        return None
    if component["tier_min"] is not None or component["tier_max"] is not None:
        return None
    if component["unit"] in FIXED_MONTHLY_UNITS:
        quantity = Decimal(1)
    elif component["unit"] == "usd_per_kwh":
        quantity = _decimal(detail.get("determinant_value"))
        if not quantity:
            return None
    else:
        return None
    billed = _decimal(line["billed_amount"])
    stored_rate = _decimal(component["rate"])
    implied = billed / quantity
    # Rounding to cents moves the billed amount by at most half a cent, i.e. 0.005 / quantity per unit.
    if abs(implied - stored_rate) > HALF_CENT / quantity:
        return {"implied_rate": implied, "stored_rate": stored_rate}
    return None


def validate_bill_lines(conn, bill_id, mapping_path=DEFAULT_MAPPING_PATH):
    if not conn.execute("SELECT 1 FROM bills WHERE id = ?", (bill_id,)).fetchone():
        raise BillNotFound(f"No bill {bill_id}.")
    runs = _rows(
        conn.execute(
            "SELECT id, mapping_version FROM audit_runs WHERE bill_id = ? ORDER BY id DESC LIMIT 1",
            (bill_id,),
        )
    )
    if not runs:
        raise NoAuditRun(f"Bill {bill_id} has no audit run.")
    run = runs[0]
    mapping, _ = load_mapping(mapping_path)
    if mapping["mapping_version"] != run["mapping_version"]:
        raise ValidationRefused(
            f"Checked-in mapping version {mapping['mapping_version']!r} does not match "
            f"run {run['id']} mapping version {run['mapping_version']!r}."
        )
    lines = _rows(
        conn.execute("SELECT * FROM audit_lines WHERE audit_run_id = ? ORDER BY id", (run["id"],))
    )
    charges = _rows(
        conn.execute("SELECT id, line_label, amount FROM bill_charges WHERE bill_id = ? ORDER BY id", (bill_id,))
    )
    audited_charge_ids = {line["bill_charge_id"] for line in lines if line["bill_charge_id"] is not None}
    result_rows = []

    for charge in charges:
        if charge["id"] in audited_charge_ids:
            continue
        if charge["line_label"] in mapping["labels"]:
            raise ValidationRefused(
                f"Audit run {run['id']} predates charge {charge['id']} ({charge['line_label']!r}); "
                "re-run the audit before validating."
            )
        result_rows.append(
            {
                "bill_charge_id": charge["id"],
                "line_label": charge["line_label"],
                "disposition": None,
                "billed": _decimal(charge["amount"]),
                "expected": None,
                "status": UNMAPPED_LABEL,
                "flags": [UNMAPPED_LABEL],
                "notes": ["the billed label has no mapping entry and was not audited"],
            }
        )

    for line in lines:
        detail = _detail(line["detail"])
        billed, expected = _decimal(line["billed_amount"]), _decimal(line["expected_amount"])
        flags, notes = [], []
        if line["line_label"] not in mapping["labels"]:
            flags.append(UNMAPPED_LABEL)
            notes.append("the audited label is no longer in the mapping")
        if line["disposition"] == "zero_expected" and billed is not None and billed > 0:
            flags.append(ZERO_EXPECTED_BILLED)
            notes.append(f"zero-expected line billed {billed}")
        if line["disposition"] == "direct":
            if abs(billed - expected) > CENT:
                flags.append(AMOUNT_MISMATCH)
                notes.append(
                    "expected line not billed (billed 0.00)" if line["bill_charge_id"] is None
                    else f"billed {billed} vs recomputed {expected}"
                )
            if line["bill_charge_id"] is not None and line["tariff_component_id"] is not None:
                components = _rows(
                    conn.execute(
                        "SELECT rate, unit, tier_min, tier_max FROM tariff_components WHERE id = ?",
                        (line["tariff_component_id"],),
                    )
                )
                disagreement = _rate_flag(line, detail, components[0] if components else None)
                if disagreement:
                    flags.append(RATE_DISAGREEMENT)
                    notes.append(
                        f"implied rate {disagreement['implied_rate'].normalize():f} vs stored "
                        f"{disagreement['stored_rate'].normalize():f}"
                    )
        flags = [flag for flag in FLAG_ORDER if flag in flags]
        result_rows.append(
            {
                "bill_charge_id": line["bill_charge_id"],
                "line_label": line["line_label"],
                "disposition": line["disposition"],
                "billed": billed,
                "expected": expected,
                "status": flags[0] if flags else OK,
                "flags": flags,
                "notes": notes,
            }
        )

    result_rows.sort(key=lambda r: (r["bill_charge_id"] is None, r["bill_charge_id"] or 0, r["line_label"]))
    flagged = [r for r in result_rows if r["flags"]]
    return {
        "bill_id": bill_id,
        "audit_run_id": run["id"],
        "mapping_version": run["mapping_version"],
        "lines": result_rows,
        "summary": {"lines": len(result_rows), "flagged": len(flagged), "ok": len(result_rows) - len(flagged)},
    }
