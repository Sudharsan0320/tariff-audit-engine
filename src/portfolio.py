"""Multi-site portfolio view (B14).

Sites are registered accounts. The ranking is built from stored audit runs only, by reusing
the account lookback; it never runs an audit and never estimates. A registered site with no
audited bills is listed with zeros and a `no_audits` note, never dropped.
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
    from .audit_bills import DEFAULT_MAPPING_PATH
    from .audit_report import _markdown_cell, _safe_filename
    from .billing_engine import round_money
    from .lookback import AUDITED, account_lookback
else:
    from audit_bills import DEFAULT_MAPPING_PATH
    from audit_report import _markdown_cell, _safe_filename
    from billing_engine import round_money
    from lookback import AUDITED, account_lookback

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "data" / "reports"
NO_AUDITS = "no_audits"
NO_AUDITS_NOTE = (
    "no audited bills are stored for this account; nothing is estimated, and the site "
    "ranks after audited sites at the same total"
)


class PortfolioError(ValueError):
    """The request cannot be honoured."""


def _rows(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def register_site(conn, name, account_number, note=None):
    name = (name or "").strip()
    account_number = (account_number or "").strip()
    if not name:
        raise PortfolioError("A site needs a name.")
    if not account_number:
        raise PortfolioError("A site needs an account number.")
    if not conn.execute(
        "SELECT 1 FROM accounts WHERE account_number = ?", (account_number,)
    ).fetchone():
        raise PortfolioError(f"Unknown account number {account_number}; nothing was registered.")
    if conn.execute("SELECT 1 FROM sites WHERE account_number = ?", (account_number,)).fetchone():
        raise PortfolioError(f"Account {account_number} is already registered as a site.")
    site_id = conn.execute(
        "INSERT INTO sites(name, account_number, note) VALUES (?, ?, ?)",
        (name, account_number, note),
    ).lastrowid
    conn.commit()
    return {"id": site_id, "name": name, "account_number": account_number, "note": note}


def portfolio_ranking(conn, mapping_path=DEFAULT_MAPPING_PATH):
    sites = _rows(conn.execute("SELECT id, name, account_number, note FROM sites ORDER BY id"))
    ranked = []
    for site in sites:
        lookback = account_lookback(conn, site["account_number"], mapping_path)
        audited = [b for b in lookback["bills"] if b["status"] == AUDITED]
        zero = Decimal(0)
        row = {
            "site_id": site["id"],
            "name": site["name"],
            "account_number": site["account_number"],
            "note": site["note"],
            "bills_audited": len(audited),
            "total_billed": round_money(sum((b["billed"] for b in audited), zero)),
            "total_recomputed": round_money(sum((b["recomputed"] for b in audited), zero)),
            "total_overcharged": round_money(sum((b["overcharge"] for b in audited), zero)),
            "worst_bill_overcharge": round_money(max((b["overcharge"] for b in audited), default=zero)),
            "worst_bill_id": None,
            "status_note": None,
        }
        if audited:
            worst = max(audited, key=lambda b: (b["overcharge"], -b["bill_id"]))
            if worst["overcharge"] > 0:
                row["worst_bill_id"] = worst["bill_id"]
        else:
            row["status_note"] = NO_AUDITS
            row["no_audits_note"] = NO_AUDITS_NOTE
        ranked.append(row)
    ranked.sort(key=lambda r: (-r["total_overcharged"], r["status_note"] == NO_AUDITS, r["name"]))
    for position, row in enumerate(ranked, 1):
        row["rank"] = position
    return {
        "sites": ranked,
        "totals": {
            "sites": len(ranked),
            "sites_without_audits": sum(1 for r in ranked if r["status_note"] == NO_AUDITS),
            "bills_audited": sum(r["bills_audited"] for r in ranked),
            "total_overcharged": round_money(sum((r["total_overcharged"] for r in ranked), Decimal(0))),
        },
        "basis": (
            "Built from stored audit runs only (like-for-like totals); overcharge = billed − "
            "recomputed per bill, counted only when positive. Ranked by total overcharged, "
            "then audited sites before sites with no audits, then name."
        ),
    }


def _money(value):
    return format(Decimal(value), "f")


def render_portfolio(result):
    out = [
        "# Portfolio ranking",
        "",
        result["basis"],
        "",
        "| Rank | Site | Account | Bills audited | Billed | Recomputed | Overcharged | Worst bill | Note |",
        "|---:|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in result["sites"]:
        out.append(
            f"| {r['rank']} | {_markdown_cell(r['name'])} | {r['account_number']} | {r['bills_audited']} "
            f"| {_money(r['total_billed'])} | {_money(r['total_recomputed'])} | {_money(r['total_overcharged'])} "
            f"| {_money(r['worst_bill_overcharge'])} | {r['status_note'] or ''} |"
        )
    t = result["totals"]
    out += ["", f"Sites: {t['sites']} · audited bills: {t['bills_audited']} · total overcharged: ${_money(t['total_overcharged'])}"]
    return "\n".join(out) + "\n"


def ranking_block(result):
    lines = []
    for r in result["sites"]:
        lines.append(
            f"{r['rank']}. {r['name']} ({r['account_number']}): {r['bills_audited']} audited bill(s), "
            f"billed ${_money(r['total_billed'])}, recomputed ${_money(r['total_recomputed'])}, "
            f"overcharged ${_money(r['total_overcharged'])}, worst bill ${_money(r['worst_bill_overcharge'])}"
            + (f" [{r['status_note']}]" if r["status_note"] else "")
        )
    t = result["totals"]
    lines.append(f"Total overcharged across {t['sites']} site(s): ${_money(t['total_overcharged'])}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Register portfolio sites and print the ranking.")
    parser.add_argument("--register", help='"Name|account|note" (note optional)')
    args = parser.parse_args(argv)
    try:
        conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"))
        try:
            if args.register:
                parts = [p.strip() for p in args.register.split("|")]
                if len(parts) not in (2, 3):
                    raise PortfolioError('--register takes "Name|account|note" (note optional).')
                site = register_site(conn, parts[0], parts[1], parts[2] if len(parts) == 3 else None)
                print(f"Registered site {site['id']}: {site['name']} ({site['account_number']})")
            result = portfolio_ranking(conn)
        finally:
            conn.close()
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / "portfolio-ranking.md"
        path.write_text(render_portfolio(result), encoding="utf-8")
    except (ValueError, *db.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(ranking_block(result))
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
