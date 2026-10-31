# Phase 6 — Test Study Summary

Phase 6 moved the local suite from 57 organically accumulated cases to 191
intentional checks. The tests remain in the gitignored `tests/` directory;
`pytest-cov` is installed in the local virtualenv only. No production behavior
or dependency manifest changed.

## What the tests prove

- **Pytest mechanics:** every test is marked `unit` by default, with the single
  full workflow explicitly marked `integration`. The unit-only run is fast;
  the integration case is isolated behind a temporary working directory and
  SQLite database.
- **Arrange–Act–Assert:** rule tests create the minimum draft or usage input
  that violates one control, invoke the validator/engine/loader, and assert
  the matching refusal reason. Edge tests separately prove allowed cases,
  such as zero ordinary rates, negative credit rates, warning-only W1, and
  conditional G-CM with no election.
- **Money tests:** expected amounts are calculated from fixed determinants
  and known rates outside the engine. The design doc records the products,
  marginal tier math, line-level rounding, and pilot sums before listing the
  corresponding tests.

The current rule matrix and baseline test inventory are in
[`phase6-gap-map.md`](../tests/phase6-gap-map.md). It records a named breaking
test for every tariff validator/activator, usage gate, engine, and audit rule.

## Golden and end-to-end evidence

The golden tests assert component and rule counts for all 41 versions,
including the 245 schedule / 181 Amendment 12 rider / 22 Rider GEN component
split; 112 tariff rules; and the 31-bill / 894-charge usage load. The pilot
like-for-like audit totals are $95.75, $101.67, and $114.45.

The integration test rebuilds the schema and executes the PDF parse, tariff
validation and activation, rule load, EAP ingestion, and pilot audit against a
database created under `tmp_path`. It hashes the live `tariffs.db` before and
after and requires equality. It passes without writing to the live database.

## Coverage and verification

Final full run: **191 passed in 8.76s**, with **90% total line coverage**.
Per-module measurements and the named CLI/scratch exclusions are in the
design document. The core parser, validator, activator, loader, engine, and
audit logic meet the 90% per-module floor after excluding CLI entry-point
glue. The `load_rules.py` duplicate-code check after an exact set-and-length
guard is reviewed as unreachable; the module measures 98% raw coverage.

```sh
# Full suite with per-module coverage
PYTHONPATH=. venv/bin/python -m pytest --cov=src --cov-report=term-missing -q

# Unit only: 190 passed in 0.23s
PYTHONPATH=. venv/bin/python -m pytest -m unit -q

# Integration only: 1 passed in 5.28s
PYTHONPATH=. venv/bin/python -m pytest -m integration -q
```

Three manual mutations were each caught by the intended test and restored:

| Mutation | Test | Result |
|---|---|---|
| August Energy DIS rate $0.01670 → $0.01671 | `test_august_direct_line_amounts_reproduce_hand_checked_fixture` | Expected $12.21 changed to $12.22; failed. |
| `ROUND_HALF_UP` → `ROUND_HALF_EVEN` | `test_decimal_rounding_uses_half_up` | $1.225 became $1.22 instead of $1.23; failed. |
| Schedule 110 first tier boundary 150 → 151 kWh/kW | `test_tiered_evaluation_uses_stored_marginal_blocks` | $30.64 became $30.66; failed. |

After restoring all mutations, the complete suite passed. Tests remain
local-only; CI and automated mutation/property-based tooling were not added.
