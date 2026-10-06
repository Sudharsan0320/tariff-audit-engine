# Tariff Database Schema (v1)

Design worked out 2026-10-06. History is never deleted or overwritten:
updates always add rows, never change existing ones.

## Table: tariff_versions

One row per tariff version.

- id (INTEGER PRIMARY KEY) — unique number per row. Needed because the same
  state + utility + schedule can have many versions over time; the id points
  at exactly one row with zero ambiguity.
- state (TEXT) — e.g. 'TX', 'VA'.
- utility (TEXT) — e.g. 'Austin Energy'.
- schedule_code (TEXT) — e.g. 'R-1', '120'.
  Together, state + utility + schedule_code identify one specific rate plan
  (its identity across time). Versions hang off that identity.
- version (INTEGER) — human-friendly label: v1, v2, v3. Dates do the lookup;
  the number does the naming ("we billed with v3").
- effective_from (DATE) — first day this version applies.
- effective_to (DATE, nullable) — last day this version applies. Empty means
  this version is currently in force. When a new version arrives, the old
  row's effective_to is set to the new row's effective_from.
- status (TEXT) — 'draft' or 'active'. New parses start as draft (quarantine)
  and become active only after validation checks pass and a human reviews.
  Drafts are never used in calculations.

## Table: tariff_components

One row per charge component. One version has many components (one-to-many).

- id (INTEGER PRIMARY KEY)
- tariff_version_id (INTEGER, FOREIGN KEY -> tariff_versions.id) — which
  version this component belongs to. Without it, components would be an
  unsorted pile with no way to reconstruct one complete tariff.
- component_type (TEXT) — customer_charge, distribution, es_tier,
  rider_kwh, rider_kw.
- billing_category (TEXT, nullable) — 'non_demand', 'demand', or empty when
  the component applies to both. Needed because e.g. schedule 110 has
  separate non-demand and demand customer charges.
- rate (REAL) — the rate value in dollars.
- tier_min_kwh, tier_max_kwh (REAL, nullable) — filled for es_tier rows
  (e.g. 0-150, 150-300); empty for flat components like customer_charge.
- season (TEXT, nullable) — 'summer'/'winter' where the tariff is seasonal;
  empty otherwise.

## How a bill finds its tariff

Given a bill dated D for state S, utility U, schedule C:
find the tariff_versions row where state=S, utility=U, schedule_code=C,
status='active', effective_from <= D and (effective_to is empty or D < effective_to).
Then load all tariff_components rows for that version's id.
