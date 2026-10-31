"""Recompute stored bills from active tariff versions and persist audit results."""

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
try:
    from src import db
except ImportError:
    import db

if __package__:
    from .normalize_schedule import normalize_schedule_code
    from .billing_engine import (
        CENT,
        as_decimal,
        evaluate_component,
        evaluate_component_group,
        evaluate_schedule_components,
        resolve_period_version,
        round_money,
    )
else:
    from normalize_schedule import normalize_schedule_code
    from billing_engine import (
        CENT,
        as_decimal,
        evaluate_component,
        evaluate_component_group,
        evaluate_schedule_components,
        resolve_period_version,
        round_money,
    )


ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = db.default_target(ROOT / "data" / "tariffs.db")
DEFAULT_MAPPING_PATH = ROOT / "docs" / "bill-label-mapping.json"
DIRECT = "direct"
ZERO_EXPECTED = "zero_expected"
OUT_OF_SCOPE = "out_of_scope"


def _sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}.")


def load_mapping(mapping_path=DEFAULT_MAPPING_PATH):
    path = Path(mapping_path)
    raw = path.read_text(encoding="utf-8")
    mapping = json.loads(raw)
    if not isinstance(mapping, dict):
        raise ValueError("Bill label mapping must be a JSON object.")
    if not isinstance(mapping.get("mapping_version"), str) or not mapping[
        "mapping_version"
    ].strip():
        raise ValueError("Bill label mapping must have a mapping_version.")
    identity = mapping.get("tariff_identity")
    if not isinstance(identity, dict) or not all(
        isinstance(identity.get(key), str) and identity[key].strip()
        for key in ("state", "utility")
    ):
        raise ValueError("Bill label mapping must identify tariff state and utility.")
    pilot = mapping.get("pilot_bill_to")
    if not isinstance(pilot, dict) or not all(
        isinstance(pilot.get(key), str) and pilot[key].strip()
        for key in ("from", "through")
    ):
        raise ValueError("Bill label mapping must define the pilot bill-to window.")
    if not isinstance(mapping.get("eligibility"), dict) or not mapping[
        "eligibility"
    ].get("schedule_code"):
        raise ValueError("Bill label mapping must define schedule eligibility.")
    if not isinstance(mapping.get("alternative_schedule"), dict) or not mapping[
        "alternative_schedule"
    ].get("schedule_code"):
        raise ValueError("Bill label mapping must define an alternative schedule.")
    labels = mapping.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise ValueError("Bill label mapping must contain a non-empty labels object.")
    for label, entry in labels.items():
        if not isinstance(label, str) or not label.strip():
            raise ValueError("Bill label mapping contains an empty label.")
        if not isinstance(entry, dict) or not entry.get("evidence"):
            raise ValueError(f"Mapping for {label!r} must include evidence.")
        disposition = entry.get("disposition")
        if disposition not in {DIRECT, ZERO_EXPECTED, OUT_OF_SCOPE}:
            raise ValueError(
                f"Mapping for {label!r} has unsupported disposition {disposition!r}."
            )
        if disposition == DIRECT and not entry.get("component_type"):
            raise ValueError(
                f"Direct mapping for {label!r} must name a component_type."
            )
        if disposition == OUT_OF_SCOPE and not entry.get("reason"):
            raise ValueError(
                f"Out-of-scope mapping for {label!r} must include a reason."
            )
    return mapping, _sha256(raw)


def _row_dict(cursor, row):
    return dict(zip((column[0] for column in cursor.description), row))


def _fetch_bill(conn, bill_id):
    cursor = conn.execute(
        """
        SELECT b.*, a.customer_name, a.service_address, a.customer_class,
               a.meter_number, a.voltage, a.delivery_phase
        FROM bills b JOIN accounts a ON a.account_number = b.account_number
        WHERE b.id = ?
        """,
        (bill_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"Bill {bill_id} does not exist.")
    bill = _row_dict(cursor, row)
    if bill["billed_rate_raw"]:
        normalized_schedule = normalize_schedule_code(bill["billed_rate_raw"])
        if normalized_schedule != bill["schedule_code"]:
            raise ValueError(
                f"Bill {bill_id} has billed rate {bill['billed_rate_raw']!r} "
                f"but stored schedule {bill['schedule_code']!r}."
            )
    for column in ("total_consumption_kwh", "total_billed"):
        if bill[column] is not None:
            bill[column] = as_decimal(bill[column])
    if bill["demand_kw"] is not None:
        bill["demand_kw"] = as_decimal(bill["demand_kw"])
    charge_cursor = conn.execute(
        """
        SELECT id, bill_id, line_label, amount
        FROM bill_charges WHERE bill_id = ? ORDER BY id
        """,
        (bill_id,),
    )
    bill["charges"] = [
        _row_dict(charge_cursor, charge) for charge in charge_cursor.fetchall()
    ]
    for charge in bill["charges"]:
        charge["amount"] = as_decimal(charge["amount"])
    determinant_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'bill_determinants'"
    ).fetchone()
    if not determinant_table:
        raise ValueError(
            "bill_determinants is missing; run src/create_db.py before auditing."
        )
    determinant_rows = conn.execute(
        """
        SELECT label, determinant_type, value
        FROM bill_determinants WHERE bill_id = ? ORDER BY id
        """,
        (bill_id,),
    ).fetchall()
    bill["determinants"] = {}
    for _, determinant_type, value in determinant_rows:
        if isinstance(value, float) and value.is_integer():
            value = int(value)  # Postgres returns 731.0 where SQLite returns 731
        numeric_value = as_decimal(value)
        if (
            determinant_type in bill["determinants"]
            and bill["determinants"][determinant_type] != numeric_value
        ):
            raise ValueError(
                f"Bill {bill_id} has conflicting values for determinant "
                f"{determinant_type!r}."
            )
        bill["determinants"][determinant_type] = numeric_value
    if "total_consumption" not in bill["determinants"]:
        bill["determinants"]["total_consumption"] = as_decimal(
            bill["total_consumption_kwh"]
        )
    if "demand" not in bill["determinants"] and bill["demand_kw"] is not None:
        bill["determinants"]["demand"] = as_decimal(bill["demand_kw"])
    return bill


def _components_for_version(conn, version_id):
    cursor = conn.execute(
        """
        SELECT c.*, v.schedule_code, v.state, v.utility
        FROM tariff_components c
        JOIN tariff_versions v ON v.id = c.tariff_version_id
        WHERE c.tariff_version_id = ?
        ORDER BY c.id
        """,
        (version_id,),
    )
    components = [_row_dict(cursor, row) for row in cursor.fetchall()]
    for component in components:
        for column in ("rate", "tier_min", "tier_max"):
            if component[column] is not None:
                component[column] = as_decimal(component[column])
    return components


def _rules_for_version(conn, version_id):
    cursor = conn.execute(
        """
        SELECT id, tariff_version_id, rule_type, rule_text
        FROM tariff_rules WHERE tariff_version_id = ? ORDER BY id
        """,
        (version_id,),
    )
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _tariff_data(conn, bill, mapping):
    base_version = resolve_period_version(
        conn,
        bill["schedule_code"],
        bill["bill_from"],
        bill["bill_to"],
        state=mapping["tariff_identity"]["state"],
        utility=mapping["tariff_identity"]["utility"],
    )
    versions = [base_version]
    schedule_components = _components_for_version(conn, base_version["id"])
    components = list(schedule_components)
    rules = {base_version["id"]: _rules_for_version(conn, base_version["id"])}

    rider_codes = [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT schedule_code FROM tariff_versions
            WHERE state = ? AND utility = ? AND status = 'active'
              AND schedule_code LIKE 'RIDER %'
            ORDER BY schedule_code
            """,
            (base_version["state"], base_version["utility"]),
        )
    ]
    rider_components = []
    for rider_code in rider_codes:
        rider_version = resolve_period_version(
            conn,
            rider_code,
            bill["bill_from"],
            bill["bill_to"],
            state=base_version["state"],
            utility=base_version["utility"],
        )
        versions.append(rider_version)
        rules[rider_version["id"]] = _rules_for_version(
            conn, rider_version["id"]
        )
        rider_components.extend(
            component
            for component in _components_for_version(conn, rider_version["id"])
            if component["billing_category"] == bill["schedule_code"]
        )
    components.extend(rider_components)

    alternative = mapping.get("alternative_schedule")
    if not alternative:
        raise ValueError("Mapping config must describe an alternative schedule.")
    alternative_version = resolve_period_version(
        conn,
        alternative["schedule_code"],
        bill["bill_from"],
        bill["bill_to"],
        state=base_version["state"],
        utility=base_version["utility"],
    )
    versions.append(alternative_version)
    rules[alternative_version["id"]] = _rules_for_version(
        conn, alternative_version["id"]
    )
    alternative_components = _components_for_version(
        conn, alternative_version["id"]
    )

    version_keys = {
        v["id"]: f'{v["schedule_code"]}#{v["version"]}' for v in versions
    }

    def natural(row):
        # surrogate ids are not content: identify a row's version by its natural key
        out = {k: v for k, v in row.items() if k != "id"}
        if "tariff_version_id" in out:
            out["tariff_version_id"] = version_keys[out["tariff_version_id"]]
        return out

    def canonical(rows):
        dumped = (
            json.dumps(natural(r), sort_keys=True, default=_json_default, separators=(",", ":"))
            for r in rows
        )
        return sorted(dumped)

    snapshot = {
        "versions": sorted(
            json.dumps(natural(v), sort_keys=True, default=_json_default)
            for v in versions
        ),
        "components": canonical(components + alternative_components),
        "rules": canonical(
            rule for version_rules in rules.values() for rule in version_rules
        ),
    }
    serialized_snapshot = json.dumps(
        snapshot, sort_keys=True, default=_json_default, separators=(",", ":")
    )
    return {
        "base_version": base_version,
        "schedule_components": schedule_components,
        "rider_components": rider_components,
        "components": components,
        "rules": rules,
        "alternative_version": alternative_version,
        "alternative_components": alternative_components,
        "snapshot_hash": _sha256(serialized_snapshot),
    }


def _find_mapping_component(entry, components, label):
    candidates = [
        component
        for component in components
        if component["component_type"] == entry["component_type"]
        and component["variant"] == entry.get("variant")
    ]
    if not candidates:
        raise ValueError(
            f"No active tariff component matches direct mapping {label!r} "
            f"({entry['component_type']!r}, variant {entry.get('variant')!r})."
        )
    if len(candidates) > 1 and not all(
        component.get("tier_min") is not None
        or component.get("tier_max") is not None
        for component in candidates
    ):
        raise ValueError(
            f"Direct mapping {label!r} matches multiple non-tiered components."
        )
    return candidates


def _evaluate_mapped_component(candidates, determinants):
    if len(candidates) == 1:
        component = candidates[0]
        if component.get("tier_min") is not None or component.get("tier_max") is not None:
            return evaluate_component_group(candidates, determinants)
        return evaluate_component(component, determinants)
    return evaluate_component_group(candidates, determinants)


def _direct_result(bill, label, charge, entry, candidates):
    raw_expected = _evaluate_mapped_component(candidates, bill["determinants"])
    expected = round_money(raw_expected)
    billed = round_money(as_decimal(charge["amount"]))
    delta = billed - expected
    if delta == 0:
        verdict = "MATCH"
    elif delta >= CENT * 2:
        verdict = "OVER"
    elif delta <= -(CENT * 2):
        verdict = "UNDER"
    else:
        verdict = "MATCH"
    component = candidates[0]
    variant = component.get("variant")
    determinant_type = (
        {"on_peak": "on_peak_usage", "off_peak": "off_peak_usage"}.get(
            variant, "total_consumption"
        )
    )
    detail = {
        "evidence": entry["evidence"],
        "component_type": entry["component_type"],
        "variant": variant,
        "rate": str(as_decimal(component["rate"])),
        "unit": component["unit"],
        "determinant_type": determinant_type,
        "determinant_value": str(bill["determinants"].get(determinant_type, "")),
    }
    return {
        "bill_charge_id": charge["id"],
        "line_label": label,
        "disposition": DIRECT,
        "verdict": verdict,
        "tariff_component_id": component["id"],
        "billed_amount": str(billed),
        "expected_amount": str(expected),
        "delta": str(delta),
        "detail": detail,
    }


def _zero_result(label, charge, entry):
    billed = round_money(as_decimal(charge["amount"]))
    verdict = "ZERO-VIOLATION" if billed != 0 else "MATCH"
    return {
        "bill_charge_id": charge["id"],
        "line_label": label,
        "disposition": ZERO_EXPECTED,
        "verdict": verdict,
        "tariff_component_id": None,
        "billed_amount": str(billed),
        "expected_amount": "0.00",
        "delta": str(billed),
        "detail": {"evidence": entry["evidence"]},
    }


def _out_of_scope_result(label, charge, entry):
    return {
        "bill_charge_id": charge["id"],
        "line_label": label,
        "disposition": OUT_OF_SCOPE,
        "verdict": "OUT-OF-SCOPE",
        "tariff_component_id": None,
        "billed_amount": str(round_money(as_decimal(charge["amount"]))),
        "expected_amount": None,
        "delta": None,
        "detail": {
            "reason": entry["reason"],
            "evidence": entry["evidence"],
        },
    }


def _missing_result(label, entry, candidates, determinants):
    expected = round_money(
        _evaluate_mapped_component(candidates, determinants)
    )
    component = candidates[0]
    return {
        "bill_charge_id": None,
        "line_label": label,
        "disposition": DIRECT,
        "verdict": "MISSING",
        "tariff_component_id": component["id"],
        "billed_amount": "0.00",
        "expected_amount": str(expected),
        "delta": str(-expected),
        "detail": {
            "evidence": entry["evidence"],
            "component_type": entry["component_type"],
            "variant": component.get("variant"),
        },
    }


def _line_flag(line):
    verdict = line["verdict"]
    if verdict == "OVER":
        return {
            "type": "MISBILL-OVERCHARGE",
            "line_label": line["line_label"],
            "impact": line["delta"],
        }
    if verdict == "UNDER":
        return {
            "type": "MISBILL-UNDERCHARGE",
            "line_label": line["line_label"],
            "impact": line["delta"],
        }
    if verdict in {"ZERO-VIOLATION", "MISSING"}:
        return {
            "type": verdict,
            "line_label": line["line_label"],
            "impact": line["delta"],
        }
    return None


def _schedule_totals(conn, components, determinants, bill_to, version_id, billing_class=None):
    items = evaluate_schedule_components(
        conn,
        components,
        determinants,
        bill_to,
        billing_class=billing_class,
        version_id=version_id,
    )
    total = sum((as_decimal(item["amount"]) for item in items), Decimal(0))
    return items, round_money(total)


def _minimum_charge_status(rules):
    minimum_rules = [
        rule for rule in rules if rule["rule_type"] == "minimum_charge"
    ]
    if not minimum_rules:
        return {
            "status": "not_found",
            "reason": "No minimum-charge rule is stored for the resolved version.",
        }
    if any("contract" in rule["rule_text"].casefold() for rule in minimum_rules):
        return {
            "status": "not_applicable",
            "reason": "No contracted minimum-charge amount is stored.",
            "rule": minimum_rules[0]["rule_text"],
        }
    return {
        "status": "not_evaluated",
        "reason": "The stored minimum-charge rule requires a separate evaluator.",
    }


def _conditional_components(tariff):
    conditional = []
    for component in tariff["rider_components"]:
        version_rules = tariff["rules"][component["tariff_version_id"]]
        option_rules = [
            rule
            for rule in version_rules
            if rule["rule_type"] == "contribution_options"
        ]
        if option_rules:
            conditional.append(
                {
                    "tariff_component_id": component["id"],
                    "component_type": component["component_type"],
                    "billing_category": component["billing_category"],
                    "status": "not_applicable",
                    "reason": (
                        "The stored tariff rule makes this contribution optional; "
                        "no customer election is stored."
                    ),
                    "rule": option_rules[0]["rule_text"],
                }
            )
    return conditional


def _compute_bill_audit(conn, bill_id, mapping, mapping_hash):
    bill = _fetch_bill(conn, bill_id)
    tariff = _tariff_data(conn, bill, mapping)
    mapped_components = {}
    for label, entry in mapping["labels"].items():
        if entry["disposition"] == DIRECT:
            mapped_components[label] = _find_mapping_component(
                entry, tariff["components"], label
            )

    actual_labels = set()
    charge_counts = {}
    for charge in bill["charges"]:
        label = charge["line_label"]
        actual_labels.add(label)
        charge_counts[label] = charge_counts.get(label, 0) + 1
        if label not in mapping["labels"]:
            raise ValueError(
                f"Unmapped bill label {label!r} on bill {bill_id}; "
                "add a reviewed mapping before auditing."
            )
        if (
            mapping["labels"][label]["disposition"] == DIRECT
            and charge_counts[label] > 1
        ):
            raise ValueError(
                f"Directly mapped label {label!r} appears more than once "
                f"on bill {bill_id}."
            )

    line_results = []
    for charge in bill["charges"]:
        label = charge["line_label"]
        entry = mapping["labels"][label]
        if entry["disposition"] == DIRECT:
            line_results.append(
                _direct_result(
                    bill,
                    label,
                    charge,
                    entry,
                    mapped_components[label],
                )
            )
        elif entry["disposition"] == ZERO_EXPECTED:
            line_results.append(_zero_result(label, charge, entry))
        else:
            line_results.append(_out_of_scope_result(label, charge, entry))

    for label, entry in mapping["labels"].items():
        if entry["disposition"] == DIRECT and label not in actual_labels:
            line_results.append(
                _missing_result(
                    label,
                    entry,
                    mapped_components[label],
                    bill["determinants"],
                )
            )

    line_results.sort(
        key=lambda line: (
            line["bill_charge_id"] is None,
            line["bill_charge_id"] or 0,
            line["line_label"],
        )
    )
    flags = [flag for line in line_results if (flag := _line_flag(line))]
    direct_lines = [
        line for line in line_results if line["disposition"] == DIRECT
    ]
    billed_total = round_money(
        sum(
            (as_decimal(line["billed_amount"]) for line in direct_lines),
            Decimal(0),
        )
    )
    expected_total = round_money(
        sum(
            (as_decimal(line["expected_amount"]) for line in direct_lines),
            Decimal(0),
        )
    )
    bill_delta = billed_total - expected_total

    base_rules = tariff["rules"][tariff["base_version"]["id"]]
    eligibility = mapping["eligibility"]
    schedule_matches = bill["schedule_code"] == eligibility["schedule_code"]
    schedule_check = {
        "status": "match" if schedule_matches else "wrong_schedule",
        "expected_schedule_code": eligibility["schedule_code"],
        "billed_schedule_code": bill["schedule_code"],
        "eligibility_status": eligibility["status"],
        "reason": eligibility["reason"],
    }
    if not schedule_matches:
        flags.append(
            {
                "type": "WRONG-SCHEDULE",
                "line_label": None,
                "impact": None,
            }
        )

    primary_items, primary_schedule_total = _schedule_totals(
        conn,
        tariff["schedule_components"],
        bill["determinants"],
        bill["bill_to"],
        tariff["base_version"]["id"],
    )
    alternative = mapping["alternative_schedule"]
    alternative_items, alternative_total = _schedule_totals(
        conn,
        tariff["alternative_components"],
        bill["determinants"],
        bill["bill_to"],
        tariff["alternative_version"]["id"],
        billing_class=alternative.get("billing_class"),
    )
    price_difference = primary_schedule_total - alternative_total
    alternative_is_cheaper = price_difference > CENT
    cheaper_flag = (
        alternative_is_cheaper
        and alternative.get("eligibility_status") == "established"
    )
    if cheaper_flag:
        flags.append(
            {
                "type": "CHEAPER-RATE",
                "line_label": None,
                "impact": str(price_difference),
            }
        )

    determinant_snapshot = {
        key: str(value) for key, value in sorted(bill["determinants"].items())
    }
    input_snapshot = {
        "bill_id": bill_id,
        "bill": {
            key: bill[key]
            for key in (
                "account_number",
                "bill_from",
                "bill_to",
                "billing_days",
                "billed_rate_raw",
                "schedule_code",
                "total_consumption_kwh",
                "demand_kw",
                "total_billed",
                "customer_name",
                "service_address",
                "customer_class",
                "source_file",
                "history_year_status",
            )
        },
        "determinants": determinant_snapshot,
        "charges": bill["charges"],
        "mapping_hash": mapping_hash,
        "tariff_snapshot_hash": tariff["snapshot_hash"],
    }
    input_hash = _sha256(
        json.dumps(
            input_snapshot,
            sort_keys=True,
            default=_json_default,
            separators=(",", ":"),
        )
    )
    report = {
        "bill_id": bill_id,
        "account_number": bill["account_number"],
        "bill_from": bill["bill_from"],
        "bill_to": bill["bill_to"],
        "billed_schedule_code": bill["schedule_code"],
        "tariff_version": {
            "id": tariff["base_version"]["id"],
            "version": tariff["base_version"]["version"],
            "effective_from": tariff["base_version"]["effective_from"],
            "effective_to": tariff["base_version"]["effective_to"],
        },
        "mapping_version": mapping["mapping_version"],
        "tariff_snapshot_hash": tariff["snapshot_hash"],
        "lines": line_results,
        "like_for_like_totals": {
            "billed": str(billed_total),
            "expected": str(expected_total),
            "delta": str(bill_delta),
        },
        "schedule_eligibility": schedule_check,
        "alternative_schedule": {
            "schedule_code": alternative["schedule_code"],
            "billing_class": alternative.get("billing_class"),
            "eligibility_status": alternative["eligibility_status"],
            "eligibility_note": alternative["eligibility_note"],
            "primary_schedule_only_total": str(primary_schedule_total),
            "alternative_schedule_only_total": str(alternative_total),
            "difference_primary_minus_alternative": str(price_difference),
            "is_cheaper_by_more_than_one_cent": alternative_is_cheaper,
            "cheaper_rate_flag_raised": cheaper_flag,
            "primary_components": primary_items,
            "alternative_components": alternative_items,
        },
        "minimum_charge": _minimum_charge_status(base_rules),
        "conditional_components": _conditional_components(tariff),
        "price_anomaly": {
            "status": "not_evaluable",
            "reason": (
                "Stored bill lines contain amounts but no printed per-line billed "
                "rates; the available price comparison is reported as OVER/UNDER."
            ),
        },
        "flags": flags,
        "accounting": {
            "stored_charge_line_count": len(bill["charges"]),
            "verdict_line_count": len(line_results),
            "all_stored_lines_verdicted": sum(
                line["bill_charge_id"] is not None for line in line_results
            )
            == len(bill["charges"]),
        },
    }
    return {
        "report": report,
        "input_hash": input_hash,
        "result_hash": _sha256(
            json.dumps(
                report,
                sort_keys=True,
                default=_json_default,
                separators=(",", ":"),
            )
        ),
    }


def _pilot_bill_ids(conn, mapping):
    pilot = mapping["pilot_bill_to"]
    schedule_code = mapping["eligibility"]["schedule_code"]
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT id FROM bills
            WHERE schedule_code = ? AND bill_to BETWEEN ? AND ?
            ORDER BY bill_to, id
            """,
            (schedule_code, pilot["from"], pilot["through"]),
        )
    ]


def _validate_mapping_census(conn, mapping):
    pilot = mapping["pilot_bill_to"]
    schedule_code = mapping["eligibility"]["schedule_code"]
    labels = {
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT c.line_label
            FROM bill_charges c JOIN bills b ON b.id = c.bill_id
            WHERE b.schedule_code = ? AND b.bill_to BETWEEN ? AND ?
            """,
            (schedule_code, pilot["from"], pilot["through"]),
        )
    }
    mapped = set(mapping["labels"])
    if labels != mapped:
        missing = sorted(labels - mapped)
        stale = sorted(mapped - labels)
        raise ValueError(
            f"Pilot label census does not match mapping config; unmapped={missing}, "
            f"not_in_census={stale}."
        )
    return labels


def _verify_mapping_evidence(results, mapping):
    matched_labels = {
        line["line_label"]
        for result in results
        for line in result["report"]["lines"]
        if line["disposition"] == DIRECT and line["verdict"] == "MATCH"
    }
    missing_proofs = sorted(
        label
        for label, entry in mapping["labels"].items()
        if entry["disposition"] == DIRECT and label not in matched_labels
    )
    if missing_proofs:
        raise ValueError(
            "Direct mapping arithmetic is not verified by a pilot bill for: "
            + ", ".join(missing_proofs)
        )


def _ensure_audit_schema(conn):
    required = {"audit_runs", "audit_lines", "bill_determinants"}
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = required - existing
    if missing:
        raise ValueError(
            "Audit schema is missing "
            + ", ".join(sorted(missing))
            + "; run src/create_db.py before auditing."
        )


def run_audits(conn, mapping_path=DEFAULT_MAPPING_PATH, bill_ids=None):
    """Audit the configured pilot (or selected bill IDs) transactionally."""
    _ensure_audit_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        mapping, mapping_hash = load_mapping(mapping_path)
        _validate_mapping_census(conn, mapping)
        selected_ids = (
            _pilot_bill_ids(conn, mapping) if bill_ids is None else list(bill_ids)
        )
        if not selected_ids:
            raise ValueError("No bills match the configured pilot selection.")
        results = [
            _compute_bill_audit(conn, bill_id, mapping, mapping_hash)
            for bill_id in selected_ids
        ]
        if bill_ids is None:
            _verify_mapping_evidence(results, mapping)

        statuses = []
        for result in results:
            report = result["report"]
            existing = conn.execute(
                """
                SELECT id, result_hash FROM audit_runs
                WHERE bill_id = ? AND input_hash = ?
                """,
                (report["bill_id"], result["input_hash"]),
            ).fetchone()
            if existing:
                if existing[1] != result["result_hash"]:
                    raise ValueError(
                        f"Audit for bill {report['bill_id']} is not deterministic."
                    )
                stored_line_count = conn.execute(
                    "SELECT COUNT(*) FROM audit_lines WHERE audit_run_id = ?",
                    (existing[0],),
                ).fetchone()[0]
                if stored_line_count != len(report["lines"]):
                    raise ValueError(
                        f"Stored audit {existing[0]} has an incomplete line set."
                    )
                statuses.append("no-op")
                continue

            cursor = conn.execute(
                """
                INSERT INTO audit_runs(
                    bill_id, mapping_version, tariff_snapshot_hash,
                    input_hash, result_hash, report_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report["bill_id"],
                    report["mapping_version"],
                    report["tariff_snapshot_hash"],
                    result["input_hash"],
                    result["result_hash"],
                    json.dumps(
                        report,
                        sort_keys=True,
                        default=_json_default,
                        separators=(",", ":"),
                    ),
                ),
            )
            audit_run_id = cursor.lastrowid
            for line in report["lines"]:
                conn.execute(
                    """
                    INSERT INTO audit_lines(
                        audit_run_id, bill_charge_id, line_label, disposition,
                        verdict, tariff_component_id, billed_amount,
                        expected_amount, delta, detail
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        audit_run_id,
                        line["bill_charge_id"],
                        line["line_label"],
                        line["disposition"],
                        line["verdict"],
                        line["tariff_component_id"],
                        line["billed_amount"],
                        line["expected_amount"],
                        line["delta"],
                        json.dumps(
                            line["detail"],
                            sort_keys=True,
                            default=_json_default,
                            separators=(",", ":"),
                        ),
                    ),
                )
            statuses.append("created")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    audit_snapshot_hash = _sha256(
        json.dumps(
            [
                (
                    result["report"]["bill_id"],
                    result["input_hash"],
                    result["result_hash"],
                )
                for result in results
            ],
            separators=(",", ":"),
        )
    )
    return {
        "mapping_version": mapping["mapping_version"],
        "bill_ids": selected_ids,
        "statuses": statuses,
        "audit_snapshot_hash": audit_snapshot_hash,
        "reports": [result["report"] for result in results],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Recompute and persist the configured pilot bill audits."
    )
    parser.add_argument(
        "--bill-id",
        type=int,
        action="append",
        dest="bill_ids",
        help="Audit one or more explicit stored bill IDs instead of the pilot set.",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=DEFAULT_MAPPING_PATH,
        help="Reviewed bill-label mapping JSON.",
    )
    args = parser.parse_args()
    try:
        with db.connect(DATABASE_PATH) as conn:
            result = run_audits(conn, args.mapping, args.bill_ids)
    except (OSError, db.Error, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    print(
        json.dumps(
            {
                "mapping_version": result["mapping_version"],
                "bill_ids": result["bill_ids"],
                "statuses": result["statuses"],
                "audit_snapshot_hash": result["audit_snapshot_hash"],
                "reports": result["reports"],
            },
            indent=2,
            default=_json_default,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
