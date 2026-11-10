"""Why a repriced schedule differs from the bill's own schedule.

Every phrase is filled from the two candidates' stored subtotals and repricing
lines. A zero difference produces no reason. A blocked candidate is not explained.
"""

from decimal import Decimal


def _money(value):
    return f"{value:,.2f}"


def _amount(value):
    return Decimal(str(value))


def _phrase(name, computed, baseline, baseline_code, schedule, difference):
    direction = "less" if difference < 0 else "more"
    return (
        f"The {name} is ${_money(computed)} under {schedule} and "
        f"${_money(baseline)} under {baseline_code}, ${_money(abs(difference))} {direction}."
    )


def _grouped(lines):
    groups = {}
    for line in lines:
        key = (line.get("rider"), line["component_type"])
        groups[key] = groups.get(key, Decimal(0)) + _amount(line["amount"])
    return groups


def _part_row(part, own, candidate):
    key = "schedule_subtotal" if part == "schedule" else "rider_subtotal"
    baseline = _amount(own[key])
    computed = _amount(candidate[key])
    difference = computed - baseline
    if difference == 0:
        return None
    name = "schedule part" if part == "schedule" else "riders part"
    return {
        "kind": "part",
        "part": part,
        "own": baseline,
        "computed": computed,
        "difference": difference,
        "text": _phrase(
            name, computed, baseline, own["schedule_code"], candidate["schedule_code"], difference
        ),
    }


def part_differences(own, candidate):
    """Schedule part and riders part, largest absolute difference first.

    The two differences sum to the candidate total minus the own-schedule total.
    A part with no difference is omitted.
    """
    rows = []
    for part in ("schedule", "riders"):
        row = _part_row(part, own, candidate)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: (-abs(row["difference"]), row["part"]))
    return rows


def line_differences(own, candidate):
    """One row per repricing line group whose amount differs from the own schedule.

    Schedule lines group by component type. Rider lines group by rider and
    component type. The row differences sum to the part differences.
    """
    rows = []
    for part, key in (("schedule", "schedule_lines"), ("riders", "rider_lines")):
        baseline = _grouped(own.get(key) or [])
        computed = _grouped(candidate.get(key) or [])
        for group in set(baseline) | set(computed):
            base_amount = baseline.get(group, Decimal(0))
            computed_amount = computed.get(group, Decimal(0))
            difference = computed_amount - base_amount
            if difference == 0:
                continue
            rider, component_type = group
            name = f"{rider} {component_type}" if rider else component_type
            rows.append({
                "kind": "line",
                "part": part,
                "rider": rider,
                "component_type": component_type,
                "own": base_amount,
                "computed": computed_amount,
                "difference": difference,
                "text": _phrase(
                    name,
                    computed_amount,
                    base_amount,
                    own["schedule_code"],
                    candidate["schedule_code"],
                    difference,
                ),
            })
    rows.sort(key=lambda row: (-abs(row["difference"]), row["part"], row["component_type"], row["rider"] or ""))
    return rows


def candidate_reasons(own, candidate):
    """Part breakdown first, then every line difference behind it.

    Both candidates must be computed. Equal totals produce no reasons.
    """
    if own.get("status") != "computed" or candidate.get("status") != "computed":
        return []
    if _amount(candidate["total"]) == _amount(own["total"]):
        return []
    return part_differences(own, candidate) + line_differences(own, candidate)
