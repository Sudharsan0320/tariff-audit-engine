"""B17 DS analysis layer: answers the agreed questions over the B20 marts and the B27 feature store.

Guardrails:
- reads only `mart.*` and `features.*` tables (READS lists them; a test parses the SQL here to enforce it);
- writes no database state in any schema; the command's only output is the HTML report file;
- never touches audit verdicts: the audit section describes the stored current audit lines, and the
  anomaly scan is descriptive statistics for auditor attention, not a verdict and not ML (that is B18);
- figures resting on partially classified charges always carry their coverage.

Arithmetic is Decimal throughout (pandas frames use object dtype). Quotients are quantized for display.
Season partition: the stored month-range codes `may_sep` (May–September) and `oct_apr` (October–April)
split the year exactly, so a bill is "summer" when its end month carries `may_sep` and "winter" when it
carries `oct_apr`. (The other stored pair, `may_oct` / `nov_apr`, overlaps it in May and October.)
"""

import argparse
import html
import json
import sys
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, getcontext
from pathlib import Path

import pandas as pd

try:
    from src import db, feature_pipeline
except ImportError:
    import db
    import feature_pipeline

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "data" / "reports" / "b17-ds-analysis.html"
AMENDMENT_DATE = date(2025, 7, 1)
ZERO = Decimal("0")
CENT = Decimal("0.01")
SUMMER, WINTER = "may_sep", "oct_apr"
TOP_N = 5
READS = ("mart.fact_bill", "mart.dim_date", "mart.dim_account", "mart.fact_charge", "mart.dim_component",
         "mart.fact_audit_line", "mart.mart_build_log", "features.feature_values",
         "features.feature_definitions", "features.feature_build_log")

getcontext().prec = 34


def dec(value):
    if value is None or value == "":
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def q(value, places="0.01"):
    return None if value is None else value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def div(a, b):
    return None if a is None or b in (None, ZERO) else a / b


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _frame(rows, columns):
    return pd.DataFrame([[r.get(c) for c in columns] for r in rows], columns=columns, dtype=object)


# ---------------------------------------------------------------- shared reads (mart + features only)
SQL_BILLS = ("SELECT f.bill_key, f.billed_schedule_code, f.billed_total, f.kwh, f.billing_days, f.audit_run_id, "
             "df.calendar_date AS bill_from, dt.calendar_date AS bill_to, dt.year, dt.month, dt.year_month, "
             "dt.rate_seasons FROM mart.fact_bill f JOIN mart.dim_account a ON a.account_key = f.account_key "
             "JOIN mart.dim_date df ON df.date_key = f.bill_from_key JOIN mart.dim_date dt ON dt.date_key = f.bill_to_key "
             "WHERE a.account_number = ? ORDER BY dt.calendar_date, f.bill_key")


def bills(conn, account):
    rows = _rows(conn, SQL_BILLS, (account,))
    features = feature_pipeline.frame(conn, account)
    for r in rows:
        for name in ("kwh_per_day", "billed_per_kwh", "charges_classified_pct", "season_codes"):
            r[name] = features.loc[r["bill_key"], name] if r["bill_key"] in features.index else None
        codes = set((r["season_codes"] or "").split(","))
        r["season"] = "summer" if SUMMER in codes else "winter" if WINTER in codes else None
        r["billed_total"] = dec(r["billed_total"])
        r["kwh"] = dec(r["kwh"])
    return rows


def _season_label(code_set):
    return code_set or "none"


# ---------------------------------------------------------------- Q1 trend and seasonality
def trend_and_seasonality(conn, account):
    rows = bills(conn, account)
    monthly = _frame([{"year_month": r["year_month"], "bill_key": r["bill_key"], "schedule": r["billed_schedule_code"],
                       "season": r["season"], "kwh": r["kwh"], "kwh_per_day": q(r["kwh_per_day"], "0.001"),
                       "billed_total": r["billed_total"], "billed_per_kwh": q(r["billed_per_kwh"], "0.00001")}
                      for r in rows],
                     ["year_month", "bill_key", "schedule", "season", "kwh", "kwh_per_day", "billed_total",
                      "billed_per_kwh"])
    years = []
    for year in sorted({r["year"] for r in rows}):
        mine = [r for r in rows if r["year"] == year]
        kwh = sum((r["kwh"] for r in mine), ZERO)
        billed = sum((r["billed_total"] for r in mine), ZERO)
        days = sum(r["billing_days"] for r in mine)
        years.append({"view": f"year {year}", "bills": len(mine), "months": f"{mine[0]['year_month']}..{mine[-1]['year_month']}",
                      "kwh": kwh, "billed_total": billed, "kwh_per_day": q(div(kwh, Decimal(days)), "0.001"),
                      "billed_per_kwh": q(div(billed, kwh), "0.00001")})
    for season in ("summer", "winter"):
        mine = [r for r in rows if r["season"] == season]
        kwh = sum((r["kwh"] for r in mine), ZERO)
        billed = sum((r["billed_total"] for r in mine), ZERO)
        days = sum(r["billing_days"] for r in mine)
        years.append({"view": f"{season} ({SUMMER if season == 'summer' else WINTER})", "bills": len(mine),
                      "months": ", ".join(sorted({r["year_month"][5:] for r in mine})), "kwh": kwh,
                      "billed_total": billed, "kwh_per_day": q(div(kwh, Decimal(days)), "0.001") if days else None,
                      "billed_per_kwh": q(div(billed, kwh), "0.00001")})
    summary_frame = _frame(years, ["view", "bills", "months", "kwh", "billed_total", "kwh_per_day", "billed_per_kwh"])

    # least-squares slope of kWh/day and billed/kWh per bill (index 0..n-1), exact Decimal
    def slope(values):
        pts = [(Decimal(i), v) for i, v in enumerate(values) if v is not None]
        n = Decimal(len(pts))
        mx = sum((x for x, _ in pts), ZERO) / n
        my = sum((y for _, y in pts), ZERO) / n
        sxx = sum(((x - mx) ** 2 for x, _ in pts), ZERO)
        return div(sum(((x - mx) * (y - my) for x, y in pts), ZERO), sxx)

    kpd_slope = slope([r["kwh_per_day"] for r in rows])
    rate_slope = slope([r["billed_per_kwh"] for r in rows])
    s = next(y for y in years if y["view"].startswith("summer"))
    w = next(y for y in years if y["view"].startswith("winter"))
    full = [y for y in years if y["view"].startswith("year ") and y["bills"] == 12]
    summary = (
        f"{len(rows)} bills, {rows[0]['year_month']} to {rows[-1]['year_month']}. Winter bills ({w['bills']}) average "
        f"{w['kwh_per_day']} kWh/day against {s['kwh_per_day']} kWh/day in summer ({s['bills']} bills): usage is "
        f"winter-peaking (pooled: season kWh / season days). Across all bills the least-squares trend is {q(kpd_slope, '0.001')} kWh/day per bill and "
        f"{q(rate_slope, '0.00001') if rate_slope is not None else '—'} $/kWh per bill in billed cost per kWh "
        f"(${q(div(s['billed_total'] + w['billed_total'], s['kwh'] + w['kwh']), '0.00001')}/kWh overall). "
        + (f"The one complete calendar year, {full[0]['view'][5:]}, billed ${full[0]['billed_total']} for {full[0]['kwh']} kWh."
           if full else "")
    )
    return {"yearly_and_season": summary_frame, "monthly": monthly}, summary


# ---------------------------------------------------------------- Q2 what drives the billed total
SQL_CHARGES = ("SELECT dt.year, fc.bill_key, fc.line_label, fc.amount, fc.component_key, c.component_type, "
               "c.billing_category FROM mart.fact_charge fc JOIN mart.fact_bill f ON f.bill_key = fc.bill_key "
               "JOIN mart.dim_account a ON a.account_key = f.account_key JOIN mart.dim_date dt ON dt.date_key = fc.bill_to_key "
               "LEFT JOIN mart.dim_component c ON c.component_key = fc.component_key WHERE a.account_number = ? "
               "ORDER BY dt.year, fc.charge_key")


def billed_drivers(conn, account):
    rows = _rows(conn, SQL_CHARGES, (account,))
    features = feature_pipeline.frame(conn, account)
    for r in rows:
        r["amount"] = dec(r["amount"])
    years = []
    for year in sorted({r["year"] for r in rows}):
        mine = [r for r in rows if r["year"] == year]
        total = sum((r["amount"] for r in mine), ZERO)
        classified = sum((r["amount"] for r in mine if r["component_key"] is not None), ZERO)
        bill_keys = sorted({r["bill_key"] for r in mine})
        pct = [features.loc[b, "charges_classified_pct"] for b in bill_keys if b in features.index]
        years.append({"year": year, "bills": len(bill_keys), "billed_total": total, "classified": classified,
                      "unclassified": total - classified,
                      "unclassified_share_pct": q(div((total - classified) * 100, total)),
                      "bills_with_any_classification": sum(1 for p in pct if p and p > 0),
                      "mean_feature_classified_pct": q(div(sum(pct, ZERO), Decimal(len(pct))) if pct else None)})
    by_year = _frame(years, ["year", "bills", "billed_total", "classified", "unclassified", "unclassified_share_pct",
                             "bills_with_any_classification", "mean_feature_classified_pct"])
    cats = {}
    for r in rows:
        if r["component_key"] is None:
            continue
        key = (r["year"], r["billing_category"] or "(base schedule, no category)", r["component_type"])
        cats[key] = cats.get(key, ZERO) + r["amount"]
    classified_detail = _frame([{"year": y, "billing_category": c, "component_type": t, "amount": a}
                                for (y, c, t), a in sorted(cats.items(), key=lambda kv: (kv[0][0], -kv[1]))],
                               ["year", "billing_category", "component_type", "amount"])
    labels = {}
    for r in rows:
        if r["component_key"] is None:
            labels[(r["year"], r["line_label"])] = labels.get((r["year"], r["line_label"]), ZERO) + r["amount"]
    label_rows = []
    for (year, label), amount in labels.items():
        label_rows.append({"year": year, "printed_label": label, "amount": amount})
    label_rows.sort(key=lambda r: (r["year"], -r["amount"]))
    unclassified_labels = _frame(label_rows, ["year", "printed_label", "amount"])
    parts = [f"{y['year']}: ${y['billed_total']} billed, ${y['classified']} classified, ${y['unclassified']} "
             f"unclassified ({y['unclassified_share_pct']}% unclassified)" for y in years]
    summary = ("Classification comes from the current audit lines, so only audited bills have any: "
               + "; ".join(parts) + ". No year is fully classified; the unclassified part is listed by its printed "
               "bill label, which describes the bill but is not a tariff classification.")
    return {"by_year": by_year, "classified_detail": classified_detail,
            "unclassified_by_printed_label": unclassified_labels}, summary


# ---------------------------------------------------------------- Q3 audit outcomes
SQL_AUDIT = ("SELECT l.bill_key, l.audit_run_id, l.line_label, l.disposition, l.verdict, l.billed_amount, "
             "l.expected_amount, l.delta, c.component_type, c.billing_category FROM mart.fact_audit_line l "
             "JOIN mart.fact_bill f ON f.bill_key = l.bill_key JOIN mart.dim_account a ON a.account_key = f.account_key "
             "LEFT JOIN mart.dim_component c ON c.component_key = l.component_key WHERE a.account_number = ? "
             "ORDER BY l.bill_key, l.audit_line_key")


def audit_outcomes(conn, account):
    rows = _rows(conn, SQL_AUDIT, (account,))
    for r in rows:
        for k in ("billed_amount", "expected_amount", "delta"):
            r[k] = dec(r[k])
    per_bill = []
    for bill_key in sorted({r["bill_key"] for r in rows}):
        mine = [r for r in rows if r["bill_key"] == bill_key]
        in_scope = [r for r in mine if r["verdict"] != "OUT-OF-SCOPE"]
        oos = [r for r in mine if r["verdict"] == "OUT-OF-SCOPE"]
        matched = sum(1 for r in in_scope if r["verdict"] == "MATCH")
        per_bill.append({
            "bill_key": bill_key, "audit_run_id": mine[0]["audit_run_id"], "lines": len(mine),
            "in_scope_lines": len(in_scope), "matched": matched,
            "match_rate_pct": q(div(Decimal(matched) * 100, Decimal(len(in_scope)))),
            "billed_in_scope": sum((r["billed_amount"] or ZERO for r in in_scope), ZERO),
            "expected": sum((r["expected_amount"] or ZERO for r in in_scope), ZERO),
            "sum_abs_delta": sum((abs(r["delta"]) for r in in_scope if r["delta"] is not None), ZERO),
            "out_of_scope_lines": "; ".join(f"{r['line_label']} ${r['billed_amount']}" for r in oos),
        })
    groups = {}
    for r in rows:
        if r["verdict"] == "OUT-OF-SCOPE":
            continue
        key = (r["disposition"], r["billing_category"] or ("(no component)" if r["component_type"] is None
                                                            else "(base schedule, no category)"),
               r["component_type"] or "(none)")
        g = groups.setdefault(key, {"lines": 0, "billed": ZERO, "expected": ZERO, "abs_delta": ZERO, "verdicts": set()})
        g["lines"] += 1
        g["billed"] += r["billed_amount"] or ZERO
        g["expected"] += r["expected_amount"] or ZERO
        g["abs_delta"] += abs(r["delta"]) if r["delta"] is not None else ZERO
        g["verdicts"].add(r["verdict"])
    by_component = _frame([{"disposition": d, "billing_category": c, "component_type": t, "lines": g["lines"],
                            "billed": g["billed"], "expected": g["expected"], "sum_abs_delta": g["abs_delta"],
                            "verdicts": ",".join(sorted(g["verdicts"]))}
                           for (d, c, t), g in sorted(groups.items())],
                          ["disposition", "billing_category", "component_type", "lines", "billed", "expected",
                           "sum_abs_delta", "verdicts"])
    pb = _frame(per_bill, ["bill_key", "audit_run_id", "lines", "in_scope_lines", "matched", "match_rate_pct",
                           "billed_in_scope", "expected", "sum_abs_delta", "out_of_scope_lines"])
    summary = ("Describes the stored current audit lines only; no verdict is recomputed or adjusted. "
               + " ".join(f"Bill {p['bill_key']}: {p['matched']} of {p['in_scope_lines']} in-scope lines MATCH "
                          f"({p['match_rate_pct']}%), billed ${p['billed_in_scope']} vs expected ${p['expected']}, "
                          f"total |delta| ${p['sum_abs_delta']}; out of scope: {p['out_of_scope_lines']} (statutory tax)."
                          for p in per_bill))
    return {"per_bill": pb, "by_component": by_component}, summary


# ---------------------------------------------------------------- Q4 the July 2025 rate change
def rate_change(conn, account):
    rows = bills(conn, account)
    for r in rows:
        r["from_d"], r["to_d"] = date.fromisoformat(r["bill_from"]), date.fromisoformat(r["bill_to"])
    straddle = [r for r in rows if r["from_d"] < AMENDMENT_DATE <= r["to_d"]]
    after = [r for r in rows if r["from_d"] >= AMENDMENT_DATE]
    before = [r for r in rows if r["to_d"] < AMENDMENT_DATE]
    after_months = {r["month"] for r in after}
    matched = [r for r in before if r["year"] == max(r2["year"] for r2 in after) - 1 and r["month"] in after_months]

    def group(name, mine):
        billed = sum((r["billed_total"] for r in mine), ZERO)
        kwh = sum((r["kwh"] for r in mine), ZERO)
        n = Decimal(len(mine))
        return {"group": name, "bills": ", ".join(str(r["bill_key"]) for r in mine),
                "schedules": ",".join(sorted({r["billed_schedule_code"] for r in mine})), "n": len(mine),
                "billed": billed, "kwh": kwh, "rate_per_kwh": div(billed, kwh),
                "mean_billed": div(billed, n), "mean_kwh": div(kwh, n)}

    groups = [group("before: same months, prior year", matched), group("before: all clean bills", before),
              group("after (Amendment No. 12)", after)]
    a = groups[2]
    splits = []
    for b in groups[:2]:
        if not b["n"] or not a["n"]:
            continue
        delta = a["mean_billed"] - b["mean_billed"]
        price = (a["rate_per_kwh"] - b["rate_per_kwh"]) * a["mean_kwh"]
        volume = b["rate_per_kwh"] * (a["mean_kwh"] - b["mean_kwh"])
        splits.append({"comparison": f"after vs {b['group']}", "mean_billed_before": q(b["mean_billed"]),
                       "mean_billed_after": q(a["mean_billed"]), "difference": q(delta), "price_effect": q(price),
                       "volume_effect": q(volume), "rate_before": q(b["rate_per_kwh"], "0.00001"),
                       "rate_after": q(a["rate_per_kwh"], "0.00001"),
                       "rate_change_pct": q(div((a["rate_per_kwh"] - b["rate_per_kwh"]) * 100, b["rate_per_kwh"])),
                       "split_sums_to_difference": q(price + volume) == q(delta)})
    display = [{**g, "rate_per_kwh": q(g["rate_per_kwh"], "0.00001"), "mean_billed": q(g["mean_billed"]),
                "mean_kwh": q(g["mean_kwh"])} for g in groups]
    s0 = splits[0]
    st = straddle[0] if straddle else None
    summary = (
        f"Amendment No. 12 took effect {AMENDMENT_DATE}. "
        + (f"Bill {st['bill_key']} ({st['bill_from']} to {st['bill_to']}) straddles that date and is excluded from the "
           "clean comparison. " if st else "")
        + f"Comparison {s0['comparison']}: the "
        f"effective rate moved from ${s0['rate_before']}/kWh to ${s0['rate_after']}/kWh ({s0['rate_change_pct']}%). The "
        f"average bill changed by ${s0['difference']}: ${s0['price_effect']} from price and ${s0['volume_effect']} from "
        f"usage (price effect = rate change x after usage; volume effect = before rate x usage change; the two sum "
        f"exactly to the difference)."
        + (" The prior-year comparison bills include schedule 110 periods before the account moved to 120, so the "
           "rate change mixes the schedule move with the amendment." if "110" in groups[0]["schedules"] else "")
    )
    return {"groups": _frame(display, ["group", "bills", "schedules", "n", "billed", "kwh", "rate_per_kwh",
                                       "mean_billed", "mean_kwh"]),
            "price_volume_split": _frame(splits, list(splits[0].keys())),
            "straddling": _frame([{"bill_key": r["bill_key"], "bill_from": r["bill_from"], "bill_to": r["bill_to"],
                                   "billed_total": r["billed_total"], "kwh": r["kwh"]} for r in straddle],
                                 ["bill_key", "bill_from", "bill_to", "billed_total", "kwh"])}, summary


# ---------------------------------------------------------------- Q5 descriptive anomaly scan
def anomaly_scan(conn, account, top_n=TOP_N):
    rows = bills(conn, account)

    def stats(values):
        values = [v for v in values if v is not None]
        n = len(values)
        mean = sum(values, ZERO) / Decimal(n)
        var = sum(((v - mean) ** 2 for v in values), ZERO) / Decimal(n - 1) if n > 1 else None
        return mean, (var.sqrt() if var else None)

    out = []
    norms = {}
    for season in ("summer", "winter"):
        mine = [r for r in rows if r["season"] == season]
        if len(mine) < 2:
            continue
        norms[season] = {m: stats([r[m] for r in mine]) for m in ("kwh_per_day", "billed_per_kwh")}
        for r in mine:
            z = {}
            for m in ("kwh_per_day", "billed_per_kwh"):
                mean, sd = norms[season][m]
                z[m] = div(r[m] - mean, sd) if r[m] is not None and sd else None
            score = max((abs(v) for v in z.values() if v is not None), default=ZERO)
            out.append({"bill_key": r["bill_key"], "bill_to": r["bill_to"], "season": season,
                        "schedule": r["billed_schedule_code"], "kwh_per_day": q(r["kwh_per_day"], "0.001"),
                        "z_kwh_per_day": q(z["kwh_per_day"], "0.001"),
                        "billed_per_kwh": q(r["billed_per_kwh"], "0.00001"),
                        "z_billed_per_kwh": q(z["billed_per_kwh"], "0.001"), "max_abs_z": q(score, "0.001")})
    out.sort(key=lambda r: (-r["max_abs_z"], r["bill_key"]))
    ranked = _frame(out, ["bill_key", "bill_to", "season", "schedule", "kwh_per_day", "z_kwh_per_day",
                          "billed_per_kwh", "z_billed_per_kwh", "max_abs_z"])
    norm_rows = [{"season": s, "bills": sum(1 for r in rows if r["season"] == s),
                  "mean_kwh_per_day": q(norms[s]["kwh_per_day"][0], "0.001"),
                  "sd_kwh_per_day": q(norms[s]["kwh_per_day"][1], "0.001"),
                  "mean_billed_per_kwh": q(norms[s]["billed_per_kwh"][0], "0.00001"),
                  "sd_billed_per_kwh": q(norms[s]["billed_per_kwh"][1], "0.00001")} for s in ("summer", "winter") if s in norms]
    top = out[:top_n]
    summary = ("Descriptive statistics for auditor attention, not audit verdicts and not ML. z-scores use each "
               "season's own mean and sample standard deviation of per-bill values (so season means here are per-bill "
               "averages, which differ slightly from the pooled kWh/day in the trend section). Top bills: "
               + "; ".join(f"bill {r['bill_key']} ({r['bill_to']}, {r['season']}): kWh/day {r['kwh_per_day']} "
                           f"(z {r['z_kwh_per_day']}), $/kWh {r['billed_per_kwh']} (z {r['z_billed_per_kwh']})"
                           for r in top) + ".")
    return {"season_norms": _frame(norm_rows, list(norm_rows[0].keys())), "ranked": ranked}, summary


QUESTIONS = [
    ("trend", "Trend and seasonality", trend_and_seasonality),
    ("drivers", "What drives the billed total", billed_drivers),
    ("audit", "Audit outcomes (descriptive)", audit_outcomes),
    ("rate_change", "The July 2025 rate change", rate_change),
    ("anomaly", "Descriptive anomaly scan (not verdicts, not ML)", anomaly_scan),
]


# ---------------------------------------------------------------- report
def provenance(conn):
    mart = conn.execute("SELECT build_id, built_at FROM mart.mart_build_log ORDER BY build_id DESC LIMIT 1").fetchone()
    feat = conn.execute("SELECT build_id, feature_set_version, built_at FROM features.feature_build_log "
                        "WHERE feature_set_version = ? ORDER BY build_id DESC LIMIT 1",
                        (feature_pipeline.FEATURE_SET_VERSION,)).fetchone()
    return {"mart_build_id": mart[0], "mart_built_at": mart[1], "feature_build_id": feat[0],
            "feature_set_version": feat[1], "feature_built_at": feat[2]}


def _cell(value):
    if value is None:
        return '<td class="null">—</td>'
    if isinstance(value, bool):
        return f"<td>{'yes' if value else 'no'}</td>"
    if isinstance(value, (Decimal, int)):
        return f'<td class="num">{html.escape(str(value))}</td>'
    return f"<td>{html.escape(str(value))}</td>"


def _table(df):
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = "".join("<tr>" + "".join(_cell(v) for v in row) + "</tr>" for row in df.itertuples(index=False))
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _bar_line_svg(labels, bars, line, bar_label, line_label, seasons):
    """Monthly billed bars (winter darker) with a kWh/day line, as inline SVG. Values are Decimal."""
    w, h, pad = 900, 260, 40
    n = len(labels)
    bw = (w - 2 * pad) / max(n, 1)
    bmax = max(bars) if bars else Decimal(1)
    lvals = [v for v in line if v is not None]
    lmax = max(lvals) if lvals else Decimal(1)
    parts = [f'<svg viewBox="0 0 {w} {h + 40}" role="img" aria-label="{html.escape(bar_label)} and {html.escape(line_label)} by month">']
    for i, (label, b, season) in enumerate(zip(labels, bars, seasons)):
        bh = float(b / bmax) * (h - pad)
        x = pad + i * bw
        cls = "bar winter" if season == "winter" else "bar"
        parts.append(f'<rect class="{cls}" x="{x + 2:.1f}" y="{h - bh:.1f}" width="{bw - 4:.1f}" height="{bh:.1f}">'
                     f'<title>{html.escape(label)}: ${b}</title></rect>')
        if i % 3 == 0:
            parts.append(f'<text class="axis" x="{x + bw / 2:.1f}" y="{h + 16}" text-anchor="middle">{html.escape(label)}</text>')
    pts = []
    for i, v in enumerate(line):
        if v is not None:
            pts.append(f"{pad + i * bw + bw / 2:.1f},{h - float(v / lmax) * (h - pad):.1f}")
    parts.append(f'<polyline class="line" points="{" ".join(pts)}"/>')
    parts.append(f'<text class="axis" x="{pad}" y="14">bars: {html.escape(bar_label)} (max ${bmax}) · line: '
                 f'{html.escape(line_label)} (max {q(lmax, "0.1")}) · darker bars: winter (oct_apr)</text></svg>')
    return "".join(parts)


def _stacked_svg(years, classified, unclassified):
    w, h, pad = 520, 220, 40
    total_max = max((c + u for c, u in zip(classified, unclassified)), default=Decimal(1))
    bw = (w - 2 * pad) / max(len(years), 1)
    parts = [f'<svg viewBox="0 0 {w} {h + 40}" role="img" aria-label="Billed total by year, classified vs unclassified">']
    for i, (y, c, u) in enumerate(zip(years, classified, unclassified)):
        x = pad + i * bw
        uh = float(u / total_max) * (h - pad)
        ch = float(c / total_max) * (h - pad)
        parts.append(f'<rect class="bar unclassified" x="{x + 10:.1f}" y="{h - uh:.1f}" width="{bw - 20:.1f}" height="{uh:.1f}">'
                     f'<title>{y} unclassified ${u}</title></rect>')
        parts.append(f'<rect class="bar" x="{x + 10:.1f}" y="{h - uh - ch:.1f}" width="{bw - 20:.1f}" height="{ch:.1f}">'
                     f'<title>{y} classified ${c}</title></rect>')
        parts.append(f'<text class="axis" x="{x + bw / 2:.1f}" y="{h + 16}" text-anchor="middle">{y}: ${c + u}</text>')
    parts.append(f'<text class="axis" x="{pad}" y="14">solid: classified by audit line · hatched grey: unclassified</text></svg>')
    return "".join(parts)


CSS = """
:root { --bg:#fff; --fg:#1d2330; --dim:#5d6575; --line:#d9dde5; --bar:#3b6fd8; --bar2:#1f3f86; --gap:#b9bfcb; --accent:#c2410c; }
@media (prefers-color-scheme: dark) { :root { --bg:#12151c; --fg:#e7e9ee; --dim:#9aa2b1; --line:#2b313d; --bar:#5d8ef0; --bar2:#9db8f5; --gap:#4a5160; --accent:#fb923c; } }
body { background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; margin:0; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 60px; }
h1 { font-size:28px; margin:0 0 4px; } h2 { font-size:21px; margin:34px 0 6px; border-top:1px solid var(--line); padding-top:22px; }
h3 { font-size:15px; margin:18px 0 4px; color:var(--dim); text-transform:uppercase; letter-spacing:.05em; }
.lead, .note, footer { color:var(--dim); } .summary { background:color-mix(in srgb, var(--bar) 10%, transparent); padding:12px 14px; border-radius:10px; }
.label { display:inline-block; font-size:12px; font-weight:700; padding:2px 8px; border-radius:999px; border:1px solid var(--accent); color:var(--accent); }
.scroll { overflow-x:auto; } table { border-collapse:collapse; width:100%; font-size:13px; margin:6px 0; }
th, td { border-bottom:1px solid var(--line); padding:5px 8px; text-align:left; white-space:nowrap; }
th { color:var(--dim); font-weight:600; } td.num { text-align:right; font-variant-numeric:tabular-nums; } td.null { color:var(--dim); }
svg { width:100%; height:auto; margin:8px 0; } .bar { fill:var(--bar); } .bar.winter { fill:var(--bar2); } .bar.unclassified { fill:var(--gap); }
.line { fill:none; stroke:var(--accent); stroke-width:2.5; } .axis { fill:var(--dim); font-size:11px; }
footer { border-top:1px solid var(--line); margin-top:40px; padding-top:14px; font-size:13px; }
"""


def render_report(conn, account):
    sections = []
    results = {}
    for key, title, fn in QUESTIONS:
        frames, summary = fn(conn, account)
        results[key] = (frames, summary)
        chart = ""
        if key == "trend":
            m = frames["monthly"]
            chart = _bar_line_svg(list(m["year_month"]), list(m["billed_total"]), list(m["kwh_per_day"]),
                                  "billed total per bill", "kWh per day", list(m["season"]))
        elif key == "drivers":
            by = frames["by_year"]
            chart = _stacked_svg(list(by["year"]), list(by["classified"]), list(by["unclassified"]))
        label = ('<p><span class="label">Descriptive statistics — not audit verdicts, not ML</span></p>'
                 if key == "anomaly" else
                 '<p><span class="label">Describes stored audits — no verdict recomputed</span></p>' if key == "audit" else "")
        tables = "".join(f"<h3>{html.escape(name.replace('_', ' '))}</h3>{_table(df)}" for name, df in frames.items())
        sections.append(f'<section id="{key}"><h2>{html.escape(title)}</h2>{label}'
                        f'<p class="summary">{html.escape(summary)}</p>{chart}{tables}</section>')
    prov = provenance(conn)
    toc = "".join(f'<li><a href="#{k}">{html.escape(t)}</a></li>' for k, t, _ in QUESTIONS)
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>B17 DS Analysis</title><style>{CSS}</style></head>
<body><main>
<h1>B17 data-science analysis</h1>
<p class="lead">Account {html.escape(account)} · read only from the B20 marts and the B27 feature store · no database state written.</p>
<ol>{toc}</ol>
{''.join(sections)}
<footer>Feature-set version {html.escape(str(prov['feature_set_version']))} (feature build {prov['feature_build_id']},
{html.escape(str(prov['feature_built_at']))}) · mart build {prov['mart_build_id']} ({html.escape(str(prov['mart_built_at']))}) ·
tables read: {html.escape(', '.join(READS))}.</footer>
</main></body></html>
"""
    return doc, results, prov


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write the B17 DS analysis report (read-only).")
    parser.add_argument("--account", default=None, help="account number (default: the only account in the marts)")
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"), readonly=True)
    try:
        account = args.account
        if account is None:
            accounts = [r[0] for r in conn.execute("SELECT account_number FROM mart.dim_account ORDER BY account_number")]
            if len(accounts) != 1:
                raise SystemExit(f"Pass --account; the marts hold {len(accounts)} accounts.")
            account = accounts[0]
        doc, _results, prov = render_report(conn, account)
    finally:
        conn.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(doc, encoding="utf-8")
    print(json.dumps({"report": str(args.out), "bytes": len(doc.encode()), "account": account, **prov},
                     default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
