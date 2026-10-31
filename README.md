# Tariff Audit Engine

[![CI](https://github.com/Sudharsan0320/tariff-audit-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/Sudharsan0320/tariff-audit-engine/actions/workflows/ci.yml)

A production-grade rebuild of an electricity tariff billing audit tool.
It recalculates customer bills under multiple rate schedules to find savings.

Built from scratch to practice: versioned data, validated ingestion,
tested billing logic, and an idempotent pipeline.

Key design: tariffs are fully data-driven — no state or schedule is hardcoded.
Rates are sourced from utility PDFs via per-utility parsers, stored as
versioned rows with a draft → active lifecycle and a validation gate.

## Project structure

- `src/create_db.py` — creates the versioned tariff database
  (`tariff_versions`, `tariff_components`, `tariff_rules`)
- `src/read_pdf.py` — explores a tariff PDF: extracts text and tables
- `src/parse_rider_gen.py` — parses the Dominion Rider GEN PDF into
  draft version + component rows (idempotent: safe to re-run)
- `src/parse_rider.py` — parses the Amendment 12 individual rider sheets into
  versioned component rows
- `src/load_rules.py` — loads the reviewed schedule/rider rules inventory
  into `tariff_rules` with per-version count checks
- `src/validate_draft.py` — checks every draft version, components, and rules;
  reports failures without changing the database
- `src/activate_version.py` — validates and activates a draft by version ID,
  closing the previous active version in the same transaction
- `src/inspect_db.py` — helper: lists the tables in the database
- `src/normalize_schedule.py` — maps bill-side rate codes onto tariff
  schedule codes (`VE-110` → `110`)
- `src/parse_account_profile.py` — extracts a Dominion EAP account profile
  from native PDF text (no OCR)
- `src/parse_usage.py` — extracts an EAP draft with monthly history,
  charge-line rows, and annual-summary witnesses; writes no database rows
- `src/validate_usage.py` — validates monthly usage drafts against the U1 schema
- `src/ingest_usage_history.py` — validates and transactionally loads U1
  accounts, bills, and bill charges; exact re-ingestion is a no-op
- `src/billing_engine.py` — resolves active tariff versions and evaluates
  tariff components with `Decimal`
- `src/audit_bills.py` — maps bill labels, recomputes pilot bills, and stores
  idempotent audit runs and line verdicts
- `src/ingest_usage.py` — earlier account-profile scaffold, retained separately
- `data/` — raw PDFs and the SQLite database (never committed to git)
- `tests/` — unit tests mirroring src/
- `docs/bill-label-mapping.json` — reviewed bill-label dispositions and
  applicability notes
- `docs/` — learning notes

Run the lifecycle gate, then activate a version only after it passes:

```sh
python src/load_rules.py
python src/validate_draft.py
python src/activate_version.py <version_id>
```

Review a native-PDF Dominion EAP draft, then run the validated U1 loader:

```sh
python src/create_db.py
python src/parse_usage.py data/raw/1172485896__EAP_Report.pdf
python src/ingest_usage_history.py data/raw/1172485896__EAP_Report.pdf
python src/audit_bills.py
```

## Status

- Phase 1: project setup — done (scaffold, venv, requirements, first commit, GitHub)
- Phase 2: versioned tariff database — done (tables built; version-chain lookup
  proven: a bill date resolves to exactly one active version)
- Phase 3: PDF tariff ingestion — complete; 40 Amendment 12 v12 versions
  active (41 total active versions), with 448 components and 112 tariff rules
- Amendment 12 schedules: 24 active versions with 245 components
- Amendment 12 riders: 16 active versions with 181 components; the separate
  April `RIDER GEN` v1 remains active with 22 components
- Phase 4: validated native-PDF usage ingestion — complete. New Kent has 31
  stored bills (9 partial-source 2023, 12 complete 2024, 10 YTD 2025) and 894
  retained detail charge rows. V6 excludes the duplicate `Basic Customer Chg`
  line after exact per-bill reconciliation; V7a requires detail = subtotal =
  total, while V7b reports annual variances without blocking. The 2024 and 2025
  billed variances are +$10.03 and +$6.20 with exact kWh; 2023 Jan–Mar is absent
  from the source and is not synthesized. Repeat ingestion is a no-op with an
  unchanged three-table snapshot hash. The legacy profile scaffold remains
  separate; CSV ingestion is not used, and scanned PDFs/OCR remain out of scope.
- Phase 5: complete for the August–October 2025 VE-120 pilot. Generic bill
  determinants are stored in `bill_determinants`; the enrichment added 96
  rows while preserving the `accounts`, `bills`, and `bill_charges` snapshot.
  The checked-in [`bill-label-mapping.json`](docs/bill-label-mapping.json)
  dispositions all 32 pilot labels. The current mapping version has three
  runs and 96 line verdicts in `audit_runs`/`audit_lines` (prior mapping
  versions remain separately recorded); every pilot line is accounted for,
  all 16 direct mappings reproduce their billed amounts, and the three
  like-for-like totals match exactly ($95.75, $101.67, and $114.45).
  Tax remains itemized and out of scope. The optional G-CM contribution is
  not applied without election data; minimum charge is not evaluated without
  a contracted amount. Schedule 110 is repriced for information, but
  eligibility is not established, so no cheaper-rate flag is raised. A
  separate price-anomaly check is unavailable because stored bill lines do
  not include printed unit rates.
- Phase 6: testing — complete. Suite grew 57 → 191 tests with 90% coverage and hand-computed money values; golden tests freeze the 41 versions' counts, 112 rules, 31 bills / 894 charges, and the pilot totals; deliberate mutations are caught. Tests stay local-only.
- Phase 7: complete. One command, `python src/run_pipeline.py`, builds the
  whole database from the checked-in [`pipeline-manifest.json`](docs/pipeline-manifest.json)
  (schema, parse, rules, validate, activate, ingest, enrich, audit), stops at
  the first failing stage, and writes a JSON run record to `data/logs/`. A
  re-run changes nothing: a fresh build matches the live database's content
  (surrogate ids, timestamps, superseded audit history, and the legacy
  `usage_accounts` row excluded), and a run against the live database was a
  verified no-op. `--stage`, `--from-stage`, and `--dry-run` are available.
- Phase 8: complete. Postgres is the primary database and runs as a Docker
  stack (`docker compose up`: `postgres` + `api`); `data/tariffs.db` is kept as
  the frozen pre-Phase-8 SQLite artifact. `src/db.py` selects the database from
  `TARIFF_DB_URL` (default: the SQLite file, so the existing workflow is
  unchanged). A FastAPI app (`src/api.py`) exposes versions, bills and audit
  verdicts and accepts tariff and bill PDF uploads through the same parsers and
  gates (refusals are 4xx with the failure list); `/` serves a one-file upload
  page and `/docs` the API explorer. The audit snapshot hash no longer depends
  on surrogate ids. GitHub Actions runs a build-and-boot smoke on every push
  (not the test suite, which stays local). The API binds to localhost with no
  authentication — a deliberate single-user posture. Run it:
  `cp .env.example .env && docker compose up -d`, then
  `docker compose run --rm api python src/run_pipeline.py` and open
  `http://127.0.0.1:8000/`.
- Path C is decided: a personal portfolio engine (OpenTariff stays a possible
  future contribution). Later work is recorded below.

## Post-roadmap continuation

Each stage is documented in [`docs/tariff-extraction-design.md`](docs/tariff-extraction-design.md).

- B1 — evidence citations: every tariff component carries its source page and cell.
- B2 — audit report: a deterministic Markdown report per audit run, with a reconciliation guard.
- B16 — all-schedule repricing, a human review step for schedule choice, and a side-by-side comparison.
- Web UI — one dark, keyboard-driven page for tariffs, bills, audits, schedule review and uploads.
- B3 + B4 — tariff version diff and change alerts with a sample-bill impact.
- Lifecycle fix — a replaced version becomes `superseded`, so a schedule can be re-versioned repeatedly.
- B5 + B6 — back-bill lookback over stored audits and line-item validation flags.
- B7 — reconciliation of parsed rates against the public URDB dataset (live run: zero counterparts, as expected for these schedules).
- B9 + B15 — parse-review queue for shaky parse results, and a tested contract every parser adapter must meet (a CSV adapter proves it).
- B11 + B12 — anomaly flags (unit cost, usage, schedule change) with degree-day weather context.
- B14 — multi-site portfolio ranking by total overcharged.

Final suite: 343 tests, all passing (local-only).
