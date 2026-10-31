"""B15: the per-utility parser contract as a tested interface.

An adapter is any callable `parse_fn(source, target)` that reads `source` and writes
draft tariff versions into the database at `target` (a path or URL, as for `db.connect`).
`check_parser_contract` runs it against scratch databases and returns the list of contract
failures; an empty list means the adapter conforms.

Clauses:
  (i)   it writes only `draft` versions (and at least one, from the valid sample);
  (ii)  every component it writes carries a citation (source_page >= 1 and a source_cell);
  (iii) its drafts pass `validate_drafts` (the refusal list is returned, not hidden);
  (iv)  running it twice changes nothing;
  (v)   given an unreadable/foreign source it refuses with an ordinary exception and writes nothing.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

try:
    from src import db
except ImportError:
    import db

if __package__:
    from .parse_rider import parse_rider
    from .validate_draft import validate_drafts
else:
    from parse_rider import parse_rider
    from validate_draft import validate_drafts

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("tariff_versions", "tariff_components", "tariff_rules")
# Raised by a crash, not a refusal.
NOT_A_REFUSAL = (TypeError, AttributeError, NameError, KeyError, IndexError, ZeroDivisionError)


def sqlite_scratch_factory(directory):
    """conn_factory: each call returns the path of a fresh, empty, schema-initialised SQLite file."""
    directory = Path(directory)
    counter = {"n": 0}

    def factory():
        counter["n"] += 1
        path = directory / f"contract-scratch-{counter['n']}.db"
        env = {k: v for k, v in os.environ.items() if k != "TARIFF_DB_URL"}
        env["TARIFF_DB_PATH"] = str(path)
        subprocess.run([sys.executable, str(ROOT / "src" / "create_db.py")], cwd=ROOT, env=env,
                       check=True, capture_output=True)
        return path

    return factory


def rider_adapter(rider_code):
    """Adapt parse_rider(rider_code, pdf_path, database_path) to the contract signature."""
    def adapter(source, target):
        return parse_rider(rider_code, pdf_path=source, database_path=target)
    return adapter


def _snapshot(target):
    conn = db.connect(target, readonly=True)
    try:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()]
                for t in TABLES}
    finally:
        conn.close()


def _counts(snapshot):
    return {t: len(rows) for t, rows in snapshot.items()}


def _has_citation(page, cell):
    return (isinstance(page, int) and not isinstance(page, bool) and page >= 1
            and isinstance(cell, str) and bool(cell.strip()))


def check_parser_contract(parse_fn, sample_source, conn_factory):
    failures = []
    target = conn_factory()
    try:
        parse_fn(sample_source, target)
    except Exception as error:
        return [f"sample: the adapter raised on the valid sample source: "
                f"{type(error).__name__}: {error}"]
    first = _snapshot(target)

    conn = db.connect(target, readonly=True)
    try:
        versions = conn.execute("SELECT id, schedule_code, status FROM tariff_versions ORDER BY id").fetchall()
        components = conn.execute(
            "SELECT id, tariff_version_id, source_page, source_cell FROM tariff_components ORDER BY id"
        ).fetchall()
        draft_failures = validate_drafts(conn)
    finally:
        conn.close()

    if not versions:
        failures.append("(i) wrote no version from the valid sample source")
    for version_id, code, status in versions:
        if status != "draft":
            failures.append(f"(i) wrote version {version_id} (schedule {code}) with status "
                            f"{status!r}; only 'draft' is allowed")
    uncited = [c[0] for c in components if not _has_citation(c[2], c[3])]
    if uncited:
        failures.append(f"(ii) {len(uncited)} of {len(components)} components carry no citation "
                        f"(source_page + source_cell), e.g. component ids {uncited[:5]}")
    failures += [f"(iii) validate_drafts: {message}" for message in draft_failures]

    try:
        parse_fn(sample_source, target)
    except Exception as error:
        failures.append(f"(iv) the second run raised: {type(error).__name__}: {error}")
    else:
        second = _snapshot(target)
        if second != first:
            failures.append(f"(iv) the second run changed stored data: {_counts(first)} -> {_counts(second)}")

    foreign_target = conn_factory()
    before = _snapshot(foreign_target)
    with tempfile.TemporaryDirectory() as folder:
        foreign = Path(folder) / ("foreign" + Path(str(sample_source)).suffix)
        foreign.write_bytes(b"\x00\xffthis is not a tariff source\x00" * 8)
        try:
            parse_fn(foreign, foreign_target)
        except NOT_A_REFUSAL as error:
            failures.append(f"(v) crashed on a foreign source instead of refusing: "
                            f"{type(error).__name__}: {error}")
        except Exception:
            pass
        else:
            failures.append("(v) accepted a foreign source without refusing")
    after = _snapshot(foreign_target)
    if after != before:
        failures.append(f"(v) wrote data from a foreign source: {_counts(before)} -> {_counts(after)}")
    return failures
