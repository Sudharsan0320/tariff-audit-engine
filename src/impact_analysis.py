"""B22: tariff impact analysis — one bill's stored determinants priced under two versions of its schedule, BY VERSION ID.

Read-only. Pricing goes through the existing repricing path only: `schedule_review.price_schedule` with a version pin
(riders included exactly as the repricing includes them), and the breakdown comes from `repricing_reasons`. Nothing here
writes an audit run or any row.

The tariff effect is new minus old. Components the evaluator cannot price correctly are named and excluded, never
estimated: today that is a component whose season or tier is carried only in its name (no stored season, no tier bounds),
which the path would bill in every month and on the whole usage. The effect is reported both on the repriced totals and
on the priceable totals (repriced total less excluded lines), and the priceable effect always equals the sum of the
priceable line deltas.

A schedule with a single stored version is reported not analysable; a pair is never forced.
"""

import html
import re
import sys
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

try:
    from src import db, repricing_reasons, schedule_review as sr, store_util as su
except ImportError:
    import db
    import repricing_reasons
    import schedule_review as sr
    import store_util as su

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "data" / "reports" / "b22-impact.html"
CENT = Decimal("0.01")
ANCHOR = {"schedule": "120", "bills": {29: Decimal("95.75"), 30: Decimal("101.67"), 31: Decimal("114.45")}}
SEASON_WORD = re.compile(r"summer|winter", re.I)
TIER_WORD = re.compile(r"first_\d+|over_\d+|excess|first \d+|in excess", re.I)
NOT_ANALYSABLE_SINGLE = "schedule {code} for {utility} has a single stored version; there is no pair to compare."


class ImpactError(RuntimeError):
    """The requested comparison is not analysable; nothing was forced."""


def _d(value):
    return None if value is None else Decimal(str(value))


def version(conn, version_id):
    rows = su.rows(conn, "SELECT id, state, utility, schedule_code, version, effective_from, effective_to, status "
                         "FROM tariff_versions WHERE id = ?", (version_id,))
    if not rows:
        raise ImpactError(f"version {version_id} does not exist")
    return rows[0]


def versions(conn, utility, code):
    return su.rows(conn, "SELECT id, version, effective_from, status FROM tariff_versions WHERE utility = ? AND "
                         "schedule_code = ? ORDER BY effective_from, id", (utility, code))


def components(conn, version_id):
    rows = su.rows(conn, "SELECT * FROM tariff_components WHERE tariff_version_id = ? ORDER BY id", (version_id,))
    for r in rows:                                  # Task A terms (absent on databases without the columns)
        for k in ("season_term", "tier_min_kwh", "tier_max_kwh"):
            r.setdefault(k, None)
    return rows


def _key(c):
    return (c["component_type"], c["billing_category"], c["unit"], c["variant"])


def rate_differences(conn, old_id, new_id):
    """[{component, billing_category, unit, variant, old, new}] for components whose rate differs, appears or disappears."""
    old = {_key(c): _d(c["rate"]) for c in components(conn, old_id)}
    new = {_key(c): _d(c["rate"]) for c in components(conn, new_id)}
    out = []
    for k in sorted(set(old) | set(new), key=lambda k: tuple(str(x) for x in k)):
        if old.get(k) != new.get(k):
            out.append({"component": k[0], "billing_category": k[1], "unit": k[2], "variant": k[3],
                        "old": old.get(k), "new": new.get(k)})
    return out


def rule_differences(conn, old_id, new_id):
    q = "SELECT rule_type, rule_text FROM tariff_rules WHERE tariff_version_id = ?"
    old = {(r["rule_type"], r["rule_text"]) for r in su.rows(conn, q, (old_id,))}
    new = {(r["rule_type"], r["rule_text"]) for r in su.rows(conn, q, (new_id,))}
    return {"only_old": sorted(old - new), "only_new": sorted(new - old)}


def pair_for(conn, utility, code):
    """The latest consecutive pair (older, newer) of a schedule, or ImpactError when it has a single version."""
    vs = versions(conn, utility, code)
    if len(vs) < 2:
        raise ImpactError(NOT_ANALYSABLE_SINGLE.format(code=code, utility=utility))
    return vs[-2]["id"], vs[-1]["id"]


def rank_pairs(conn, utility):
    """Every analysable schedule of `utility`, most differing component rates first (ties by schedule code)."""
    codes = [r["schedule_code"] for r in su.rows(conn, "SELECT DISTINCT schedule_code FROM tariff_versions WHERE "
                                                       "utility = ? ORDER BY schedule_code", (utility,))]
    ranked = []
    for code in codes:
        try:
            old_id, new_id = pair_for(conn, utility, code)
        except ImpactError:
            continue
        ranked.append({"schedule": code, "old_id": old_id, "new_id": new_id,
                       "differing_rates": len(rate_differences(conn, old_id, new_id))})
    ranked.sort(key=lambda r: (-r["differing_rates"], r["schedule"]))
    return ranked


def unpriceable(conn, version_id):
    """{component_type: reason} for components the evaluator cannot price correctly (named, never estimated)."""
    out = {}
    for c in components(conn, version_id):
        name = f"{c['component_type']} {c['billing_category'] or ''}"
        reasons = []
        if c["season"] is None and c["season_term"] is None and SEASON_WORD.search(name):
            reasons.append("season carried only in the component name (no stored season), so the path bills it in every month")
        if (c["tier_min"] is None and c["tier_max"] is None and c["tier_min_kwh"] is None
                and c["tier_max_kwh"] is None and TIER_WORD.search(name)):
            reasons.append("usage tier carried only in the component name (no stored tier bounds), so the path bills it on all usage")
        if reasons:
            out[c["component_type"]] = "; ".join(reasons)
    return out


def _grouped(lines):
    g = defaultdict(Decimal)
    for line in lines or []:
        g[(line.get("rider"), line["component_type"])] += Decimal(str(line["amount"]))
    return g


def _determinant(unit, determinants, band=None):
    if unit == "usd_per_month":
        return Decimal(1), "1 month"
    if unit == "usd_per_kwh" and band is not None and determinants.get("total_consumption") is not None:
        lo, hi = band
        kwh = Decimal(str(determinants["total_consumption"]))
        qty = max(Decimal(0), (kwh if hi is None else min(kwh, Decimal(str(hi)))) - Decimal(str(lo or 0)))
        return qty, f"{qty} kWh (band {lo or 0}–{'∞' if hi is None else hi} of {kwh})"
    if unit == "usd_per_kwh" and determinants.get("total_consumption") is not None:
        kwh = Decimal(str(determinants["total_consumption"]))
        return kwh, f"{kwh} kWh"
    return None, None


def analyse(conn, bill, old_id, new_id, history=None):
    """Price `bill` (a schedule_review bill dict) under versions old_id and new_id of its billed schedule."""
    old_v, new_v = version(conn, old_id), version(conn, new_id)
    code = bill["schedule_code"]
    for v in (old_v, new_v):
        if v["schedule_code"] != code:
            raise ImpactError(f"version {v['id']} is schedule {v['schedule_code']!r}, not the bill's {code!r}")
    if old_v["utility"] != new_v["utility"] or old_id == new_id:
        raise ImpactError("the two versions must be distinct versions of one utility's schedule")
    identity = {"state": old_v["state"], "utility": old_v["utility"]}
    history = sr._history(conn, bill) if history is None else history
    priced = {}
    for side, vid in (("old", old_id), ("new", new_id)):
        cand = sr.price_schedule(conn, bill, history, identity, code, pins={code: vid})
        if cand.get("status") != "computed":
            raise ImpactError(f"version {vid} did not price: {cand.get('reason') or cand.get('blocker')}")
        priced[side] = cand
    old, new = priced["old"], priced["new"]
    excluded = {**unpriceable(conn, old_id), **unpriceable(conn, new_id)}
    rates = {side: defaultdict(list) for side in ("old", "new")}
    units, bands, terms = {}, {}, {}
    for side, vid in (("old", old_id), ("new", new_id)):
        for c in components(conn, vid):
            rates[side][c["component_type"]].append(_d(c["rate"]))
            units[c["component_type"]] = c["unit"]
            if c["tier_min_kwh"] is not None or c["tier_max_kwh"] is not None:
                bands[c["component_type"]] = (_d(c["tier_min_kwh"]), _d(c["tier_max_kwh"]))
            if c["season_term"]:
                terms[c["component_type"]] = c["season_term"]
    g_old = {"schedule": _grouped(old["schedule_lines"]), "riders": _grouped(old["rider_lines"])}
    g_new = {"schedule": _grouped(new["schedule_lines"]), "riders": _grouped(new["rider_lines"])}
    lines = []
    for part in ("schedule", "riders"):
        for key in sorted(set(g_old[part]) | set(g_new[part]), key=lambda k: (k[0] or "", k[1])):
            rider, ctype = key
            a, b = g_old[part].get(key, Decimal(0)), g_new[part].get(key, Decimal(0))
            row = {"part": part, "rider": rider, "component": ctype, "old_amount": a, "new_amount": b, "delta": b - a,
                   "excluded": excluded.get(ctype) if part == "schedule" else None}
            if part == "schedule":
                row["old_rate"] = rates["old"].get(ctype)
                row["new_rate"] = rates["new"].get(ctype)
                qty, label = _determinant(units.get(ctype), bill["determinants"], bands.get(ctype))
                row["determinant"] = label
                row["season_term"] = terms.get(ctype)
                if qty is not None and not row["excluded"] and len(row["old_rate"] or []) == 1 and len(row["new_rate"] or []) == 1:
                    row["hand_old"] = (row["old_rate"][0] * qty).quantize(CENT, ROUND_HALF_UP)
                    row["hand_new"] = (row["new_rate"][0] * qty).quantize(CENT, ROUND_HALF_UP)
                    row["hand_delta"] = row["hand_new"] - row["hand_old"]
                    row["hand_matches_path"] = (row["hand_old"], row["hand_new"]) == (a, b)
            lines.append(row)
    old_total, new_total = Decimal(str(old["total"])), Decimal(str(new["total"]))
    excl_old = sum((r["old_amount"] for r in lines if r["excluded"]), Decimal(0))
    excl_new = sum((r["new_amount"] for r in lines if r["excluded"]), Decimal(0))
    priceable_effect = (new_total - excl_new) - (old_total - excl_old)
    line_sum = sum((r["delta"] for r in lines if not r["excluded"]), Decimal(0))
    if priceable_effect != line_sum:
        raise ImpactError(f"priceable effect {priceable_effect} does not tie to the line deltas {line_sum}")
    return {
        "utility": old_v["utility"], "schedule": code, "bill_id": bill.get("id"),
        "bill_from": str(bill["bill_from"]), "bill_to": str(bill["bill_to"]),
        "determinants": {k: str(v) for k, v in bill["determinants"].items()},
        "old": {"id": old_id, "effective_from": str(old_v["effective_from"]), "status": old_v["status"], "total": old_total},
        "new": {"id": new_id, "effective_from": str(new_v["effective_from"]), "status": new_v["status"], "total": new_total},
        "repriced_effect": new_total - old_total,
        "excluded": {k: v for k, v in excluded.items() if any(r["component"] == k for r in lines)},
        "excluded_old": excl_old, "excluded_new": excl_new,
        "priceable_old": old_total - excl_old, "priceable_new": new_total - excl_new,
        "effect": priceable_effect,
        "lines": lines,
        "rate_differences": rate_differences(conn, old_id, new_id),
        "rule_differences": rule_differences(conn, old_id, new_id),
        "reasons": [r["text"] for r in repricing_reasons.candidate_reasons(old, new)],
    }


def analyse_bill(conn, bill_id, old_id, new_id):
    """A stored bill, by id."""
    return analyse(conn, sr._fetch_bill(conn, bill_id), old_id, new_id)


def price_bill_under(conn, bill_id, version_id):
    """A stored bill's repriced total under one version id of its schedule (the anchor's route)."""
    bill = sr._fetch_bill(conn, bill_id)
    v = version(conn, version_id)
    cand = sr.price_schedule(conn, bill, sr._history(conn, bill), {"state": v["state"], "utility": v["utility"]},
                             bill["schedule_code"], pins={bill["schedule_code"]: version_id})
    return Decimal(str(cand["total"])) if cand.get("status") == "computed" else None


def anchor(conn):
    """Pilot bills 29-31 under Dominion schedule 120's current version: must give 95.75 / 101.67 / 114.45 exactly."""
    current = su.rows(conn, "SELECT id FROM tariff_versions WHERE schedule_code = ? AND status = 'active' "
                            "AND utility = 'Virginia Electric and Power Company'", (ANCHOR["schedule"],))
    if len(current) != 1:
        raise ImpactError(f"schedule 120 has {len(current)} active versions")
    vid = current[0]["id"]
    got = {b: price_bill_under(conn, b, vid) for b in ANCHOR["bills"]}
    return {"version_id": vid, "totals": got, "ok": got == ANCHOR["bills"]}


def witness_bill(kwh=750, bill_from="2026-01-01", bill_to="2026-01-31", schedule="R"):
    """A constructed residential month (no real Pepco bill exists): a customer charge plus a stated kWh."""
    return {"id": None, "account_number": "B22-WITNESS", "bill_from": bill_from, "bill_to": bill_to,
            "billing_days": (date.fromisoformat(bill_to) - date.fromisoformat(bill_from)).days + 1,
            "schedule_code": schedule, "customer_class": "Residential", "meter_number": "B22-WITNESS", "voltage": None,
            "determinants": {"total_consumption": Decimal(str(kwh))}}


def choose_witness(conn, utility="Pepco", bill=None):
    """The top pair by differing rates that the constructed bill can price and whose differing components are not all
    unpriceable; every pair passed over is returned with its reason."""
    skipped = []
    for pair in rank_pairs(conn, utility):
        if pair["differing_rates"] == 0:
            break
        diffs = rate_differences(conn, pair["old_id"], pair["new_id"])
        bad = {**unpriceable(conn, pair["old_id"]), **unpriceable(conn, pair["new_id"])}
        if all(d["component"] in bad for d in diffs):
            skipped.append((pair["schedule"], "every differing component is unpriceable"))
            continue
        b = bill or witness_bill(schedule=pair["schedule"])
        try:
            return pair, analyse(conn, b, pair["old_id"], pair["new_id"], history=[]), skipped
        except ImpactError as error:                # the construction cannot supply an input this schedule needs
            skipped.append((pair["schedule"], f"{pair['differing_rates']} differing rates, but the constructed residential "
                                              f"bill (customer charge + kWh) cannot price it: {error}"))
    raise ImpactError(f"no {utility} pair has a priceable differing rate; skipped {skipped}")


# ---------------------------------------------------------------- report
def _m(x):
    return "" if x is None else f"${Decimal(x):,.2f}" if isinstance(x, Decimal) else html.escape(str(x))


def _rates(rs):
    return ", ".join(str(r) for r in rs) if rs else "—"


def _month_section(label, result):
    e = html.escape
    rows = ""
    for r in result["lines"]:
        if r["part"] != "schedule":
            continue
        hand = (f"{_rates(r['old_rate'])} × {e(r['determinant'])} = {_m(r['hand_old'])}; "
                f"{_rates(r['new_rate'])} × {e(r['determinant'])} = {_m(r['hand_new'])}" if "hand_old" in r else "")
        status = f"<span class=x>excluded — {e(r['excluded'])}</span>" if r["excluded"] else (
            "priceable" + ("" if r.get("hand_matches_path", True) else " (hand ≠ path!)"))
        rows += (f"<tr><td>{e(r['component'])}</td><td>{e(r.get('season_term') or '—')}</td>"
                 f"<td class=n>{_rates(r['old_rate'])}</td><td class=n>{_rates(r['new_rate'])}</td>"
                 f"<td>{e(r['determinant'] or '')}</td><td class=n>{_m(r['old_amount'])}</td>"
                 f"<td class=n>{_m(r['new_amount'])}</td><td class=n>{_m(r['delta'])}</td><td>{hand}</td><td>{status}</td></tr>")
    rider_delta = sum((r["delta"] for r in result["lines"] if r["part"] == "riders"), Decimal(0))
    rider_old = sum((r["old_amount"] for r in result["lines"] if r["part"] == "riders"), Decimal(0))
    n_riders = len({r["rider"] for r in result["lines"] if r["part"] == "riders"})
    o, n = result["old"], result["new"]
    excl = (f"<p>Excluded lines (named above, never estimated): old {_m(result['excluded_old'])}, new "
            f"{_m(result['excluded_new'])}; priceable totals {_m(result['priceable_old'])} → {_m(result['priceable_new'])}.</p>"
            if result["excluded"] else "<p>No component is excluded: every line on the bill prices from stored data.</p>")
    return f"""<h2>{e(label)}: {e(result['bill_from'])} to {e(result['bill_to'])}, {e(result['determinants'].get('total_consumption', '?'))} kWh</h2>
<div class=wrap><table><tr><th>Component</th><th>Season</th><th>Old rate</th><th>New rate</th><th>Determinant</th><th>Path old</th>
<th>Path new</th><th>Line delta</th><th>Hand arithmetic</th><th>Status</th></tr>{rows}</table></div>
<p>Out-of-season components are not on the bill (the path prices only the {e(str(next((r.get('season_term') for r in result['lines'] if r.get('season_term')), 'in-season')))} lines this month).
Riders: {n_riders} riders priced by the path for the same period on both sides ({_m(rider_old)}); rider delta {_m(rider_delta)}.</p>
{excl}
<p>Repriced totals: old {_m(o['total'])} → new {_m(n['total'])}.</p>
<p class=big>Tariff effect, {e(label)}: {_m(result['effect'])} per month</p>
<p>Ties: {_m(result['priceable_new'])} − {_m(result['priceable_old'])} = sum of line deltas = {_m(result['effect'])}.</p>"""


def render(anchor_result, ranking, pair, results, skipped):
    """results: [(label, analyse() result)], e.g. a winter month and a summer twin."""
    e = html.escape
    first = results[0][1]
    o, n = first["old"], first["new"]
    rows_rank = "".join(f"<tr><td>{e(r['schedule'])}</td><td>#{r['old_id']} → #{r['new_id']}</td>"
                        f"<td class=n>{r['differing_rates']}</td></tr>" for r in ranking[:8])
    rows_diff = "".join(f"<tr><td>{e(d['component'])}</td><td>{e(str(d['unit']))}</td><td class=n>{d['old']}</td>"
                        f"<td class=n>{d['new']}</td></tr>" for d in first["rate_differences"])
    anchor_rows = "".join(f"<tr><td>Bill {b}</td><td class=n>{_m(t)}</td><td class=n>{_m(ANCHOR['bills'][b])}</td></tr>"
                          for b, t in anchor_result["totals"].items())
    skipped_txt = "".join(f"<li>{e(s)}: {e(w)}</li>" for s, w in skipped) or "<li>none</li>"
    months = "".join(_month_section(label, r) for label, r in results)
    summary = "; ".join(f"{e(label)} {_m(r['effect'])}" for label, r in results)
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>B22 Tariff Impact</title><style>
:root{{--bg:#fff;--fg:#1d1f23;--mut:#5d636e;--line:#dde1e6;--acc:#1f5fbf;--x:#9a3b00}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e8eaed;--mut:#a3a9b3;--line:#30343b;--acc:#7fb0ff;--x:#ffb27a}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:1080px;margin:auto}}
h1{{font-size:22px}}h2{{font-size:17px;margin-top:28px}}.mut{{color:var(--mut)}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13.5px}}th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
th{{color:var(--mut);font-weight:600}}td.n{{text-align:right;font-variant-numeric:tabular-nums}}.x{{color:var(--x)}}
.big{{font-size:20px;font-weight:600;color:var(--acc)}}</style></head><body>
<h1>B22 — Tariff impact analysis</h1>
<p class=mut>Read-only. Priced through the existing repricing path (schedule_review.price_schedule with a version pin; riders as
the repricing includes them; breakdown from repricing_reasons). Zero model calls. Generated {date.today().isoformat()}.</p>
<p class=big>Schedule {e(pair['schedule'])} #{o['id']} → #{n['id']}, 750 kWh residential month: {summary} per month.</p>

<h2>Dominion position</h2>
<p><b>No Dominion pair exists.</b> Every Dominion (and Appalachian) schedule has a single stored version, so no Dominion schedule can be
compared version-to-version. The July 2025 Dominion impact therefore remains quantified only by B17's before/after analysis until a
prior Dominion book is loaded.</p>

<h2>Anchor (named check)</h2>
<p>Pilot bills 29–31 under Dominion schedule 120's current version (id {anchor_result['version_id']}) through this module:
<b>{'reproduced exactly' if anchor_result['ok'] else 'FAILED'}</b>.</p>
<div class=wrap><table><tr><th>Bill</th><th>Module</th><th>Expected</th></tr>{anchor_rows}</table></div>

<h2>Witness pair selection</h2>
<p>Rule: the Pepco schedule whose latest two versions differ in the most component rates, falling to the next pair when the
constructed bill cannot price a pair or all its differing components are unpriceable. Chosen: <b>Schedule {e(pair['schedule'])}</b>,
#{o['id']} ({o['effective_from']}, {e(o['status'])}) vs #{n['id']} ({n['effective_from']}, {e(n['status'])}).</p>
<div class=wrap><table><tr><th>Schedule</th><th>Pair</th><th>Differing rates</th></tr>{rows_rank}</table></div>
<p>Differing components of the chosen pair (selection evidence):</p>
<div class=wrap><table><tr><th>Component</th><th>Unit</th><th>Old rate</th><th>New rate</th></tr>{rows_diff}</table></div>
<p>Pairs ranked above the chosen one and passed over, with the reason:</p><ul>{skipped_txt}</ul>

<h2>Constructed witness bills</h2>
<p>No real Pepco bill exists, so residential months are constructed: a customer charge plus a stated 750 kWh, in a winter month and
its summer twin. Seasons and tier bands come from the stored terms (Task A: season_term, tier_min_kwh, tier_max_kwh derived from
the component names; the season of a month from the version's stored rule "Summer – Billing months of June through October.
Winter – Billing months of November through May.").</p>
{months}

<h2>Method</h2>
<ul><li>Both versions are priced BY VERSION ID: the pin replaces date coverage for the billed schedule only; everything else in the
path is unchanged.</li>
<li>A seasonal component prices only in its season; a tiered component prices only the kWh inside its band (first 400 = 0–400,
over 400 = 400 and up). Components without stored terms price exactly as before.</li>
<li>A component whose season or tier exists only in its name, with no stored term, would still be excluded and named, never
estimated. The hand arithmetic must equal the path's amounts line by line.</li>
<li>A schedule with one stored version is reported not analysable; no pair is forced. Versions that differ only in rules give a
true zero price effect, and the rule differences are reported instead.</li></ul>
<p class=mut>Rule differences for the pair: {len(first['rule_differences']['only_old'])} only in old,
{len(first['rule_differences']['only_new'])} only in new.</p>
</body></html>"""


WITNESS_MONTHS = (("January 2026 (winter)", "2026-01-01", "2026-01-31"), ("July 2026 (summer)", "2026-07-01", "2026-07-31"))


def main(argv=None):
    conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"))
    try:
        a = anchor(conn)
        print("anchor:", a)
        if not a["ok"]:
            print("STOP: anchor did not reproduce")
            return 2
        ranking = rank_pairs(conn, "Pepco")
        pair, first, skipped = choose_witness(conn, "Pepco")
        results = [(label, analyse(conn, witness_bill(750, f, t, pair["schedule"]), pair["old_id"], pair["new_id"], history=[]))
                   for label, f, t in WITNESS_MONTHS]
    finally:
        conn.close()
    REPORT.write_text(render(a, ranking, pair, results, skipped), encoding="utf-8")
    for label, r in results:
        print(f"{label}: repriced {r['old']['total']} -> {r['new']['total']} | effect {r['effect']} | excluded {sorted(r['excluded'])}")
    print("report:", REPORT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
