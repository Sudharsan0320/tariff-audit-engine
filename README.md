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
  (`tariff_versions`, `tariff_components`)
- `src/read_pdf.py` — explores a tariff PDF: extracts text and tables
- `src/parse_rider_gen.py` — parses the Dominion Rider GEN PDF into
  draft version + component rows (idempotent: safe to re-run)
- `src/inspect_db.py` — helper: lists the tables in the database
- `data/` — raw PDFs and the SQLite database (never committed to git)
- `tests/` — unit tests mirroring src/
- `docs/` — learning notes

## Status

- Phase 1: project setup — done (scaffold, venv, requirements, first commit, GitHub)
- Phase 2: versioned tariff database — done (tables built; version-chain lookup
  proven: a bill date resolves to exactly one active version)
- Phase 3: PDF tariff ingestion — in progress (text/table extraction working;
  parser writes draft versions; validation gate and activation next)
- Next: validated usage ingestion, config-driven billing engine, unit tests,
  idempotent pipeline, hardened API/Postgres/Docker/CI
