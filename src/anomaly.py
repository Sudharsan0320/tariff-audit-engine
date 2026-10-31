"""Account anomaly scan (B11): flags for review, never verdicts.

Read-only over stored bills. Per bill it computes kWh, total billed and the implied unit
cost (Decimal, $/kWh), then raises flags:

* unit_cost_jump  - unit cost deviates from the median of the trailing same-schedule bills
                    by more than UNIT_COST_DEVIATION;
* usage_spike     - kWh changes by more than USAGE_CHANGE vs the previous bill while the
                    schedule is unchanged;
* schedule_change - informational: the schedule differs from the previous bill.

A flag says "look here"; it never claims a billing error. Bills whose tariff version is not
loaded are marked, because a unit-cost shift across tariff eras is not an error.
"""

import argparse
from decimal import Decimal
import sys
from pathlib import Path

try:
    from src import db
except ImportError:
    import db

if __package__:
    from .audit_bills import DEFAULT_MAPPING_PATH, load_mapping
    from .audit_report import _markdown_cell, _safe_filename
    from .schedule_review import _covering_version
    from . import weather_check
else:
    from audit_bills import DEFAULT_MAPPING_PATH, load_mapping
    from audit_report import _markdown_cell, _safe_filename
    from schedule_review import _covering_version
    import weather_check

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "data" / "reports"

UNIT_COST_DEVIATION = Decimal("0.20")
USAGE_CHANGE = Decimal("0.50")
TRAILING_WINDOW = 6
MIN_BASELINE_BILLS = 3
UNIT_COST_PLACES = Decimal("0.000001")
PERCENT_PLACES = Decimal("0.1")

UNIT_COST_JUMP = "unit_cost_jump"
USAGE_SPIKE = "usage_spike"
SCHEDULE_CHANGE = "schedule_change"
ERA_NOTE = (
    "this bill's tariff version is not loaded, so it was billed under terms this system "
    "has not seen; a unit-cost shift here may be an era change, not an error"
)


class AnomalyError(ValueError):
    """The anomaly scan cannot be built from the stored data."""


class AccountNotFound(AnomalyError):
    """No such account."""


def median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _rows(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _percent(ratio):
    return (ratio * 100).quantize(PERCENT_PLACES)


def account_anomalies(
    conn,
    account_number,
    unit_cost_deviation=UNIT_COST_DEVIATION,
    usage_change=USAGE_CHANGE,
    trailing_window=TRAILING_WINDOW,
    min_baseline_bills=MIN_BASELINE_BILLS,
    mapping_path=DEFAULT_MAPPING_PATH,
):
    if not conn.execute(
        "SELECT 1 FROM accounts WHERE account_number = ?", (account_number,)
    ).fetchone():
        raise AccountNotFound(f"No account {account_number}.")
    identity = load_mapping(mapping_path)[0]["tariff_identity"]
    bills = _rows(
        conn.execute(
            "SELECT id, bill_from, bill_to, schedule_code, total_consumption_kwh, total_billed "
            "FROM bills WHERE account_number = ? ORDER BY bill_from, bill_to, id",
            (account_number,),
        )
    )

    out = []
    previous = None
    for bill in bills:
        kwh = None if bill["total_consumption_kwh"] is None else Decimal(str(bill["total_consumption_kwh"]))
        billed = None if bill["total_billed"] is None else Decimal(str(bill["total_billed"]))
        unit_cost = None
        if kwh and billed is not None:
            unit_cost = (billed / kwh).quantize(UNIT_COST_PLACES)
        covered = _covering_version(conn, bill["schedule_code"], bill, identity) is not None
        row = {
            "bill_id": bill["id"],
            "bill_from": str(bill["bill_from"]),
            "bill_to": str(bill["bill_to"]),
            "schedule_code": bill["schedule_code"],
            "kwh": kwh,
            "total_billed": billed,
            "unit_cost": unit_cost,
            "tariff_version_loaded": covered,
            "flags": [],
        }

        if previous is not None and previous["schedule_code"] != bill["schedule_code"]:
            row["flags"].append(
                {
                    "flag": SCHEDULE_CHANGE,
                    "informational": True,
                    "reason": (
                        f"schedule changed from {previous['schedule_code']} to {bill['schedule_code']}; "
                        "context for the figures around this bill, not an alarm"
                    ),
                    "numbers": {"from": previous["schedule_code"], "to": bill["schedule_code"]},
                }
            )

        if unit_cost is not None:
            baseline = [
                r["unit_cost"]
                for r in out
                if r["schedule_code"] == bill["schedule_code"] and r["unit_cost"] is not None
            ][-trailing_window:]
            if len(baseline) >= min_baseline_bills:
                base = median(baseline)
                if base > 0:
                    deviation = (unit_cost - base) / base
                    if abs(deviation) > unit_cost_deviation:
                        flag = {
                            "flag": UNIT_COST_JUMP,
                            "informational": False,
                            "reason": (
                                f"unit cost ${unit_cost}/kWh is {_percent(deviation)}% from the trailing "
                                f"{len(baseline)}-bill same-schedule median ${base.quantize(UNIT_COST_PLACES)}/kWh "
                                f"(threshold ±{_percent(unit_cost_deviation)}%); flagged for review"
                            ),
                            "numbers": {
                                "unit_cost": unit_cost,
                                "baseline_median": base.quantize(UNIT_COST_PLACES),
                                "baseline_bills": len(baseline),
                                "deviation_percent": _percent(deviation),
                                "threshold_percent": _percent(unit_cost_deviation),
                            },
                        }
                        if not covered:
                            flag["era_note"] = ERA_NOTE
                        row["flags"].append(flag)

        if (
            previous is not None
            and previous["schedule_code"] == bill["schedule_code"]
            and previous["kwh"]
            and kwh is not None
        ):
            change = (kwh - previous["kwh"]) / previous["kwh"]
            if abs(change) > usage_change:
                direction = "increase" if change > 0 else "decrease"
                row["flags"].append(
                    {
                        "flag": USAGE_SPIKE,
                        "informational": False,
                        "reason": (
                            f"kWh {'rose' if change > 0 else 'fell'} {_percent(abs(change))}% vs the previous bill "
                            f"({previous['kwh']} → {kwh}); threshold {_percent(usage_change)}%; "
                            "flagged for review"
                        ),
                        "numbers": {
                            "kwh": kwh,
                            "previous_kwh": previous["kwh"],
                            "change_percent": _percent(change),
                            "threshold_percent": _percent(usage_change),
                            "direction": direction,
                        },
                    }
                )
        out.append(row)
        previous = row

    flagged = [r for r in out if any(not f["informational"] for f in r["flags"])]
    return {
        "account_number": account_number,
        "thresholds": {
            "unit_cost_deviation_percent": _percent(unit_cost_deviation),
            "usage_change_percent": _percent(usage_change),
            "trailing_window_bills": trailing_window,
            "min_baseline_bills": min_baseline_bills,
        },
        "bills": out,
        "summary": {
            "bills": len(out),
            "bills_flagged_for_review": len(flagged),
            "unit_cost_jump": sum(1 for r in out for f in r["flags"] if f["flag"] == UNIT_COST_JUMP),
            "usage_spike": sum(1 for r in out for f in r["flags"] if f["flag"] == USAGE_SPIKE),
            "schedule_change": sum(1 for r in out for f in r["flags"] if f["flag"] == SCHEDULE_CHANGE),
            "bills_without_loaded_tariff_version": sum(1 for r in out if not r["tariff_version_loaded"]),
        },
        "caveats": [
            "Flags are for review, never verdicts; none of them asserts a billing error.",
            "Bills whose tariff version is not loaded were billed under terms this system has not "
            "seen: a unit-cost shift there is an era change, not an error.",
            "A schedule change is context only; unit-cost and usage comparisons never cross schedules.",
        ],
    }


def _flag_line(bill, flag):
    parts = [f"- Bill {bill['bill_id']} ({bill['bill_from']} → {bill['bill_to']}, schedule {bill['schedule_code']}): "
             f"`{flag['flag']}`{' (informational)' if flag['informational'] else ''} — {flag['reason']}"]
    if "weather" in flag:
        parts.append(f"  - weather: `{flag['weather']['annotation']}` — {flag['weather']['why']}")
    if "era_note" in flag:
        parts.append(f"  - era note: {flag['era_note']}")
    return "\n".join(parts)


def summary_block(result):
    s = result["summary"]
    lines = [
        f"Account:                       {result['account_number']}",
        f"Bills scanned:                 {s['bills']}",
        f"Bills flagged for review:      {s['bills_flagged_for_review']}",
        f"  unit_cost_jump:              {s['unit_cost_jump']}",
        f"  usage_spike:                 {s['usage_spike']}",
        f"schedule_change (info):        {s['schedule_change']}",
        f"Bills with no loaded version:  {s['bills_without_loaded_tariff_version']}",
    ]
    for bill in result["bills"]:
        for flag in bill["flags"]:
            lines.append(f"  bill {bill['bill_id']} {bill['bill_to']} {flag['flag']}: {flag['reason']}")
            if "weather" in flag:
                lines.append(f"      weather: {flag['weather']['annotation']} ({flag['weather']['why']})")
    return "\n".join(lines)


def render_anomalies(result):
    t = result["thresholds"]
    out = [
        f"# Anomaly scan — account {result['account_number']}",
        "",
        "Flags are **for review**, never verdicts. Built from stored bills only.",
        "",
        "## Thresholds",
        "",
        f"- Unit-cost jump: deviation from the trailing {t['trailing_window_bills']}-bill "
        f"same-schedule median above ±{t['unit_cost_deviation_percent']}% "
        f"(needs at least {t['min_baseline_bills']} baseline bills).",
        f"- Usage spike: kWh change vs the previous bill above {t['usage_change_percent']}% "
        "with the schedule unchanged.",
        "- Schedule change: informational only.",
        "",
        "## Flags",
        "",
    ]
    flagged = [(b, f) for b in result["bills"] for f in b["flags"]]
    out += [_flag_line(b, f) for b, f in flagged] if flagged else ["No flags."]
    weather = result.get("weather")
    if weather:
        out += ["", "## Weather normalisation", ""]
        out.append(f"Degree days: base {weather['base_temperature_f']}°F, CDD + HDD summed over each bill period.")
        if "top_quartile_threshold" in weather:
            out.append(
                f"Quartiles across {weather['periods_with_data']} of {weather['periods_total']} periods: "
                f"Q1 {weather['q1']:.1f}, median {weather['median']:.1f}, "
                f"top-quartile threshold (Q3) {weather['top_quartile_threshold']:.1f}."
            )
        out.append(weather["rule"])
    out += ["", "## Bills", ""]
    out += [
        "| Bill | Period | Schedule | kWh | Total billed | $/kWh | Tariff version loaded | Degree days |",
        "|---:|---|---|---:|---:|---:|---|---:|",
    ]
    for b in result["bills"]:
        dd = (b.get("weather") or {}).get("degree_days")
        out.append(
            f"| {b['bill_id']} | {b['bill_from']} → {b['bill_to']} | {_markdown_cell(b['schedule_code'])} "
            f"| {b['kwh']} | {b['total_billed']} | {b['unit_cost'] if b['unit_cost'] is not None else '—'} "
            f"| {'yes' if b['tariff_version_loaded'] else 'no'} | {'—' if dd is None else f'{dd:.1f}'} |"
        )
    out += ["", "## Caveats", ""] + [f"- {c}" for c in result["caveats"]]
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scan one account's bills for anomalies (for review).")
    parser.add_argument("--account", required=True)
    parser.add_argument("--weather", help="daily weather JSON (default: the fetched file when present)")
    args = parser.parse_args(argv)
    try:
        conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"), readonly=True)
        try:
            result = account_anomalies(conn, args.account)
        finally:
            conn.close()
        weather = (
            weather_check.load_weather(args.weather) if args.weather else weather_check.load_default_weather()
        )
        if weather is not None:
            result = weather_check.annotate(result, weather)
        path = REPORT_DIR / f"{_safe_filename(args.account)}-anomalies.md"
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(render_anomalies(result), encoding="utf-8")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(summary_block(result))
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
