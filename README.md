# Tariff Audit Engine

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
- `src/ingest_usage.py` — earlier account-profile scaffold, retained separately
- `data/` — raw PDFs and the SQLite database (never committed to git)
- `tests/` — unit tests mirroring src/
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
- Next: config-driven billing and Phase 5 recompute-and-compare audits, using
  the fully covered 2025 VE-120 periods as the pilot set
