# Phase 7 — Pipeline Study Summary

Phase 7 replaced the human who ran each stage in the right order with one
command, `python src/run_pipeline.py`, and proved that re-running it is safe.

## Orchestration concepts exercised

- **Programs talking through files and a database.** Each stage stays an
  independent CLI; the orchestrator only sequences them, captures output and
  exit status, and stops at the first failure.
- **Idempotency as the resume mechanism.** After a failure, fixing the input
  and re-running the same command completes the build. No bookkeeping of what
  already ran is needed because finished stages report `no change`.
- **Measuring effects instead of trusting messages.** A stage that prints
  "PASS: loaded 112 rules" can still change nothing; the orchestrator
  decides `no change` from before/after content snapshots.
- **Run records outside the data.** Manifests live in `data/logs/`, so a run
  never changes the database it reports on.
- **Explicit input manifest.** Inputs are named, ordered and checked in; a
  new account joins by adding a line, never by dropping a file in a folder.

## What the audit found and what was fixed first

The stage-safety audit on a database copy found four defects, each fixed as a
named step before the orchestrator existed: parsers created duplicate drafts
when a version was already active; a mistyped schedule code exited 0 and
created an empty draft (RG-CM, the one legitimate zero-page schedule, now
comes from a data-driven cross-reference stage); stages could not target
another database (`TARIFF_DB_PATH`); and `bills.source_file` stored one
machine's absolute path (now project-relative, with a one-time backed-up
migration that also re-keyed the three current audit hashes).

## Acceptance evidence

- Fresh build from empty: golden counts reproduced; second run changed nothing.
- Fresh build equals the live database in content, with named exclusions:
  timestamps, surrogate ids, superseded audit history, the legacy
  `usage_accounts` row, and id-embedding hashes (a Phase 8 item).
- Sabotaged run stopped at the right stage, left earlier work intact and later
  stages untouched; the same command resumed to the golden state.
- Run against the live database: every stage `no change`; content hash and
  file hash identical before and after.

```sh
# Full pipeline against the live database
venv/bin/python src/run_pipeline.py
# Plan only
venv/bin/python src/run_pipeline.py --dry-run
# Debug or resume
venv/bin/python src/run_pipeline.py --stage load_rules --stage validate
venv/bin/python src/run_pipeline.py --from-stage ingest_bills
# Compare two databases by content
venv/bin/python src/compare_databases.py data/tariffs.db <other.db>
# Tests (local-only)
PYTHONPATH=. venv/bin/python -m pytest -q
```

Test suite: 207 passed (206 unit, 1 integration).
