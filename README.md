# Tariff Audit Engine

A production-grade rebuild of an electricity tariff billing audit tool.
It recalculates customer bills under multiple rate schedules to find savings.

Built from scratch to practice: versioned data, validated ingestion,
tested billing logic, and an idempotent pipeline.

## Project structure

- `src/` — pipeline and billing engine code
- `data/` — raw, interim, processed (never committed to git)
- `tests/` — unit tests mirroring src/
- `docs/` — learning notes

## Status

Phase 1: project setup — done.
Phase 2: versioned tariff database - in progress
