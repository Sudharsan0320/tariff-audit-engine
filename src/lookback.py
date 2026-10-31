"""Back-bill lookback: what stored audit runs say about an account's past bills.

Read-only. It reads existing audit runs and never runs a new audit. A bill with no
covering tariff version is listed as such and is never estimated.
"""

import argparse
from decimal import Decimal
import json
import sys
from pathlib import Path

try:
    from src import db
except ImportError:
    import db

if __package__:
    from .audit_bills import DEFAULT_MAPPING_PATH, load_mapping
    from .audit_report import _markdown_cell, _safe_filename
    from .billing_engine import round_money
    from .schedule_review import _covering_version
else:
    from audit_bills import DEFAULT_MAPPING_PATH, load_mapping
    from audit_report import _markdown_cell, _safe_filename
    from billing_engine import round_money
    from schedule_review import _covering_version

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "data" / "reports"
AUDITED = "audited"
NO_COVERING_VERSION = "no_covering_version"
NOT_AUDITED = "not_audited"


class LookbackError(ValueError):
    """The lookback cannot be built from the stored data."""


class AccountNotFound(LookbackError):
    """No such account."""


def _rows(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _latest_run(conn, bill_id):
    runs = _rows(
        conn.execute(
            "SELECT id, mapping_version, report_json FROM audit_runs WHERE bill_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (bill_id,),
        )
    )
    return runs[0] if runs else None


def _totals(run):
    report = run["report_json"]
    report = json.loads(report) if isinstance(report, (str, bytes)) else report
    try:
        totals = report["like_for_like_totals"]
        return {key: Decimal(str(totals[key])) for key in ("billed", "expected", "delta")}
    except (KeyError, TypeError, ArithmeticError) as error:
        raise LookbackError(
            f"Audit run {run['id']} has no usable like-for-like totals: {error!r}."
        ) from error


def account_lookback(conn, account_number, mapping_path=DEFAULT_MAPPING_PATH):
    if not conn.execute(
        "SELECT 1 FROM accounts WHERE account_number = ?", (account_number,)
    ).fetchone():
        raise AccountNotFound(f"No account {account_number}.")
    identity = load_mapping(mapping_path)[0]["tariff_identity"]
    bills = _rows(
        conn.execute(
            "SELECT id, bill_from, bill_to, schedule_code FROM bills "
            "WHERE account_number = ? ORDER BY bill_from, bill_to, id",
            (account_number,),
        )
    )
    rows = []
    for bill in bills:
        row = {
            "bill_id": bill["id"],
            "bill_from": str(bill["bill_from"]),
            "bill_to": str(bill["bill_to"]),
            "schedule_code": bill["schedule_code"],
        }
        run = _latest_run(conn, bill["id"])
        if run is not None:
            totals = _totals(run)
            row.update(
                status=AUDITED,
                run_id=run["id"],
                billed=totals["billed"],
                recomputed=totals["expected"],
                delta=totals["delta"],
                overcharge=max(totals["billed"] - totals["expected"], Decimal(0)),
            )
        elif _covering_version(conn, bill["schedule_code"], bill, identity) is None:
            row.update(
                status=NO_COVERING_VERSION,
                reason=(
                    f"no active version of schedule {bill['schedule_code']} covers "
                    f"{row['bill_from']} through {row['bill_to']}; the bill is not estimated"
                ),
            )
        else:
            row.update(
                status=NOT_AUDITED,
                reason="a covering version exists but no audit run is stored; run the audit first",
            )
        rows.append(row)

    audited = [r for r in rows if r["status"] == AUDITED]
    not_reauditable = [r for r in rows if r["status"] != AUDITED]
    return {
        "account_number": account_number,
        "bills": rows,
        "totals": {
            "audited_bills": len(audited),
            "overcharged_bills": sum(1 for r in audited if r["overcharge"] > 0),
            "total_overcharged": round_money(sum((r["overcharge"] for r in audited), Decimal(0))),
            "net_delta": round_money(sum((r["delta"] for r in audited), Decimal(0))),
            "not_reauditable_bills": len(not_reauditable),
            "not_reauditable_reasons": {
                status: sum(1 for r in not_reauditable if r["status"] == status)
                for status in (NO_COVERING_VERSION, NOT_AUDITED)
                if any(r["status"] == status for r in not_reauditable)
            },
        },
    }


def _money(value):
    return "—" if value is None else format(Decimal(value), "f")


def totals_block(result):
    t = result["totals"]
    reasons = ", ".join(f"{n} {k}" for k, n in t["not_reauditable_reasons"].items()) or "none"
    return "\n".join(
        [
            f"Account:                   {result['account_number']}",
            f"Audited bills:             {t['audited_bills']}",
            f"Total overcharged:         ${_money(t['total_overcharged'])} "
            f"({t['overcharged_bills']} bill(s) with a positive difference)",
            f"Not re-auditable bills:    {t['not_reauditable_bills']} ({reasons})",
        ]
    )


def render_lookback(result):
    t = result["totals"]
    out = [
        f"# Back-bill lookback — account {result['account_number']}",
        "",
        "Built from the stored audit runs only; no audit was run for this report. "
        "Overcharge = billed − recomputed, counted only when positive.",
        "",
        "## Totals",
        "",
        "| Measure | Value |",
        "|---|---:|",
        f"| Audited bills | {t['audited_bills']} |",
        f"| Bills with an overcharge | {t['overcharged_bills']} |",
        f"| **Total overcharged** | **${_money(t['total_overcharged'])}** |",
        f"| Net difference (billed − recomputed) | ${_money(t['net_delta'])} |",
        f"| Not re-auditable bills | {t['not_reauditable_bills']} |",
        "",
        "## Bills in period order",
        "",
        "| Bill | Period | Schedule | Status | Billed (like-for-like) | Recomputed | Difference | Overcharge |",
        "|---:|---|---|---|---:|---:|---:|---:|",
    ]
    for r in result["bills"]:
        out.append(
            f"| {r['bill_id']} | {r['bill_from']} → {r['bill_to']} | {_markdown_cell(r['schedule_code'])} "
            f"| {r['status']} | {_money(r.get('billed'))} | {_money(r.get('recomputed'))} "
            f"| {_money(r.get('delta'))} | {_money(r.get('overcharge'))} |"
        )
    pending = [r for r in result["bills"] if r["status"] != AUDITED]
    if pending:
        out += ["", "## Bills not re-auditable", ""]
        out += [
            f"- Bill {r['bill_id']} ({r['bill_from']} → {r['bill_to']}): `{r['status']}` — "
            f"{_markdown_cell(r['reason'])}"
            for r in pending
        ]
        out += [
            "",
            "These bills are never repriced against a version that was not in force; they become "
            "re-auditable as historical versions are loaded.",
        ]
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write a back-bill lookback report for one account.")
    parser.add_argument("--account", required=True)
    args = parser.parse_args(argv)
    try:
        conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"), readonly=True)
        try:
            result = account_lookback(conn, args.account)
        finally:
            conn.close()
        path = REPORT_DIR / f"{_safe_filename(args.account)}-lookback.md"
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(render_lookback(result), encoding="utf-8")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(totals_block(result))
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
