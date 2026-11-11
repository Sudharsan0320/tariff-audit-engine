"""Task A: Pepco season and tier bounds as data, derived deterministically from stored component type strings.

Same class of stored-data transformation as the billing-category mapping: no PDF, no extraction, no model.
Three additive columns on tariff_components (pre-existing columns are never written):

- season_term   TEXT     'summer' / 'winter' as named in the string. (The table already has a `season` column, used
                         by Dominion with billing_month_seasons rules; it is left untouched, so the new term gets its
                         own column.)
- tier_min_kwh  DECIMAL  first_N -> 0;    over_N -> N
- tier_max_kwh  DECIMAL  first_N -> N;    over_N -> NULL (no upper bound)

Partial terms are stored as encoded (a season without a tier, or the reverse). A name carrying two seasons or two tier
readings is listed as ambiguous and left NULL; tier words on a component not priced per kWh are listed and left NULL.
Names matching nothing keep NULLs and are listed. Only Pepco components are touched. Idempotent.
"""

import re
import sys
from collections import Counter
from decimal import Decimal

try:
    from src import db, store_util as su
except ImportError:
    import db
    import store_util as su

UTILITY = "Pepco"
COLUMNS = {"season_term": "TEXT", "tier_min_kwh": "DECIMAL", "tier_max_kwh": "DECIMAL"}
SEASON = re.compile(r"(?<![a-z])(summer|winter)(?![a-z])")
TIER = re.compile(r"(?<![a-z0-9])(first|over)_(\d+)(?![0-9])")


def derive(component_type, unit="usd_per_kwh"):
    """(season, tier_min, tier_max, note). note is None when the name was read cleanly (even partially)."""
    name = str(component_type or "").lower()
    seasons = sorted(set(SEASON.findall(name)))
    tiers = TIER.findall(name)
    if len(seasons) > 1 or len(tiers) > 1:
        return None, None, None, "ambiguous: matches two readings"
    season = seasons[0] if seasons else None
    lo = hi = None
    if tiers:
        if unit != "usd_per_kwh":
            return season, None, None, f"tier words but unit {unit} is not per kWh; tier left NULL"
        word, n = tiers[0]
        lo, hi = (Decimal(0), Decimal(n)) if word == "first" else (Decimal(n), None)
    if season is None and not tiers:
        return None, None, None, "no known pattern"
    return season, lo, hi, None


def ensure_columns(conn):
    have = _columns(conn)
    for name, kind in COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE tariff_components ADD COLUMN {name} {kind}")
    conn.commit()


def _columns(conn):
    if su.is_sqlite(conn):
        return {r[1] for r in conn.execute("PRAGMA table_info(tariff_components)").fetchall()}
    return {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns "
                                       "WHERE table_name = 'tariff_components'").fetchall()}


def plan(conn):
    rows = su.rows(conn, "SELECT c.id, c.component_type, c.unit FROM tariff_components c JOIN tariff_versions v "
                         "ON v.id = c.tariff_version_id WHERE v.utility = ? ORDER BY c.id", (UTILITY,))
    out = []
    for r in rows:
        season, lo, hi, note = derive(r["component_type"], r["unit"])
        out.append({"id": r["id"], "component_type": r["component_type"], "unit": r["unit"],
                    "season": season, "tier_min": lo, "tier_max": hi, "note": note})
    return out


def coverage(planned):
    def kind(p):
        tier = p["tier_min"] is not None or p["tier_max"] is not None
        return "both" if p["season"] and tier else "season only" if p["season"] else "tier only" if tier else "neither"
    counts = Counter(kind(p) for p in planned)
    neither = Counter((p["component_type"], p["unit"], p["note"]) for p in planned if kind(p) == "neither")
    partial_notes = Counter((p["component_type"], p["note"]) for p in planned if kind(p) != "neither" and p["note"])
    return {"total": len(planned), "counts": dict(counts), "neither": sorted(neither.items()),
            "partial_notes": sorted(partial_notes.items())}


def apply(conn):
    ensure_columns(conn)
    planned = plan(conn)
    try:
        for p in planned:
            if p["season"] is None and p["tier_min"] is None and p["tier_max"] is None:
                continue
            conn.execute("UPDATE tariff_components SET season_term = ?, tier_min_kwh = ?, tier_max_kwh = ? WHERE id = ?",
                         (p["season"], su.param(conn, p["tier_min"]), su.param(conn, p["tier_max"]), p["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return coverage(planned)


def main(argv=None):
    conn = db.connect(db.default_target("data/tariffs.db"))
    try:
        cov = apply(conn)
    finally:
        conn.close()
    print(cov["total"], cov["counts"])
    for (name, unit, note), n in cov["neither"]:
        print(f"  neither: {name} ({unit}) x{n}: {note}")
    for (name, note), n in cov["partial_notes"]:
        print(f"  partial: {name} x{n}: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
