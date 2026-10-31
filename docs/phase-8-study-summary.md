# Phase 8 — Service Study Summary

Phase 8 turned the scripts-and-a-file engine into a service: Postgres as the
database, a FastAPI layer, a one-file upload page, a Docker stack, and a CI
build-and-boot check.

## Concepts exercised

- **A connection factory instead of an ORM.** `src/db.py` absorbs every dialect
  difference (placeholders, `PRAGMA`, `BEGIN IMMEDIATE`, `lastrowid`, DDL
  types) in one module, so the proven stages kept their SQL and shape.
- **Rebuild, don't migrate.** The Phase 7 pipeline built Postgres from the
  sources; equality with the frozen SQLite was proved by content comparison,
  not by a dump-and-convert.
- **Content, not surrogate keys.** Redefining the snapshot hash over natural
  content made two independent builds identical with no id exclusions.
- **Thin API.** Reads are SELECTs; actions call the same functions the stages
  use; gate refusals are 4xx carrying the real failure list.
- **Containers as a whole system.** The pipeline runs in the container too;
  named volumes keep the data across `down`/`up`.
- **CI scoped to a claim.** It proves "builds and boots on a clean machine",
  nothing more (tests stay local).

## Things the work taught

- Cross-engine comparison needs normalization: NULL ordering, `5` vs `5.0`,
  text collation.
- A `--locale=C` Postgres without `--encoding=UTF8` is SQL_ASCII and returns
  `bytes`; only running inside the container exposed it.
- Uploading a tariff reuses parsers whose semantics are fixed (Amendment 12);
  a different tariff document would need parser work, not API work.

## Result

216 tests passing (local). Fresh Postgres builds reproduce the golden state,
re-runs are no-ops, the API and page serve real work, and the SQLite file is
the frozen rollback artifact. Open: GitHub CI status and the red-X check are
confirmed by pushing.
