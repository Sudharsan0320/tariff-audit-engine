"""Idempotent pipeline orchestrator (Phase 7, D1-D4).

Runs each stage's existing CLI as a subprocess, in dependency order, against one
database (passed to every stage through TARIFF_DB_PATH). Stops at the first
failure. Every run writes one JSON manifest to the log directory (never the
database) and prints a compact summary.

Stage status values: done (no change) | done (N rows changed) |
done (N tables created) | skipped | FAILED | not run.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_MANIFEST = ROOT / "docs" / "pipeline-manifest.json"
DEFAULT_DATABASE = ROOT / "data" / "tariffs.db"
DEFAULT_LOG_DIR = ROOT / "data" / "logs"
STAGES = (
    "create_schema",
    "parse_gen",
    "parse_schedules",
    "create_cross_reference",
    "parse_riders",
    "load_rules",
    "validate",
    "activate",
    "ingest_bills",
    "enrich_determinants",
    "audit",
)
TAIL_LINES = 25


class StageFailure(Exception):
    def __init__(self, command, returncode, output):
        super().__init__(f"{command} exited {returncode}")
        self.command = command
        self.returncode = returncode
        self.output = output


# ---------------------------------------------------------------- snapshots
def _is_timestamp(column):
    return column.endswith("_at")


def database_snapshot(database):
    """Per-table content digests; timestamp (*_at) columns are the only exclusion."""
    database = Path(database)
    if not database.exists():
        return {"schema": "", "tables": {}}
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = {}
        schema_parts = []
        names = [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for name in names:
            info = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            schema_parts.append(f"{name}:{','.join(row[1] for row in info)}")
            columns = [row[1] for row in info if not _is_timestamp(row[1])]
            keys = [row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]]
            order = ", ".join(f'"{c}"' for c in (keys or columns))
            selected = ", ".join(f'"{c}"' for c in columns)
            digests = []
            for row in conn.execute(f'SELECT {selected} FROM "{name}" ORDER BY {order}'):
                payload = json.dumps(row, default=str, separators=(",", ":"))
                digests.append(hashlib.sha256(payload.encode()).hexdigest())
            tables[name] = {
                "rows": len(digests),
                "hash": hashlib.sha256("".join(digests).encode()).hexdigest(),
                "_digests": Counter(digests),
            }
        return {"schema": "|".join(schema_parts), "tables": tables}
    finally:
        conn.close()


def content_hash(snapshot):
    lines = [snapshot["schema"]]
    lines += [f"{n}:{t['rows']}:{t['hash']}" for n, t in sorted(snapshot["tables"].items())]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def diff_snapshots(before, after):
    """Return (rows changed per table, tables created)."""
    changed = {}
    for name, table in after["tables"].items():
        old = before["tables"].get(name)
        old_digests = old["_digests"] if old else Counter()
        added = sum((table["_digests"] - old_digests).values())
        removed = sum((old_digests - table["_digests"]).values())
        if max(added, removed):
            changed[name] = max(added, removed)
    created = len(set(after["tables"]) - set(before["tables"]))
    return changed, created


def table_rows(snapshot):
    return {name: table["rows"] for name, table in sorted(snapshot["tables"].items())}


# ----------------------------------------------------------------- manifest
def load_manifest(path):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("tariff_pdfs", "inventory", "mapping", "schedules", "riders", "usage_pdfs"):
        if key not in manifest:
            raise ValueError(f"Pipeline manifest is missing '{key}'.")
    for key in ("schedules", "riders", "usage_pdfs"):
        values = manifest[key]
        if not isinstance(values, list) or not values or len(set(values)) != len(values):
            raise ValueError(f"Manifest '{key}' must be a non-empty list without duplicates.")
    return manifest


STAGE_FILES = {
    "parse_gen": lambda m: [m["tariff_pdfs"]["rider_gen"]],
    "parse_schedules": lambda m: [m["tariff_pdfs"]["amendment"]],
    "parse_riders": lambda m: [m["tariff_pdfs"]["amendment"]],
    "create_cross_reference": lambda m: [m["inventory"]],
    "load_rules": lambda m: [m["inventory"]],
    "ingest_bills": lambda m: m["usage_pdfs"],
    "enrich_determinants": lambda m: m["usage_pdfs"],
    "audit": lambda m: [m["mapping"]],
}


def preflight(manifest, selected):
    missing = []
    for stage in selected:
        for rel in STAGE_FILES.get(stage, lambda m: [])(manifest):
            if not (ROOT / rel).is_file():
                missing.append(rel)
    if missing:
        raise ValueError("Manifest input files not found: " + ", ".join(sorted(set(missing))))


# ------------------------------------------------------------------- stages
class Context:
    def __init__(self, manifest, database, record):
        self.manifest = manifest
        self.database = Path(database)
        self.record = record
        self.notes = {}

    def run(self, script, *args):
        command = [sys.executable, str(SRC / script), *map(str, args)]
        label = " ".join([script, *map(str, args)])
        env = dict(os.environ, TARIFF_DB_PATH=str(self.database))
        started = time.monotonic()
        result = subprocess.run(
            command, cwd=ROOT, env=env, capture_output=True, text=True, check=False
        )
        output = (result.stdout + result.stderr).strip().splitlines()
        self.record.append(
            {
                "command": label,
                "returncode": result.returncode,
                "seconds": round(time.monotonic() - started, 2),
                "output_tail": output[-TAIL_LINES:],
            }
        )
        if result.returncode != 0:
            raise StageFailure(label, result.returncode, output)
        return output

    def draft_ids(self):
        conn = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        try:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM tariff_versions WHERE status = 'draft' ORDER BY id"
                )
            ]
        finally:
            conn.close()

    def status_count(self, status):
        conn = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM tariff_versions WHERE status = ?", (status,)
            ).fetchone()[0]
        finally:
            conn.close()


def stage_create_schema(ctx):
    ctx.database.parent.mkdir(parents=True, exist_ok=True)
    ctx.run("create_db.py")


def stage_parse_gen(ctx):
    ctx.run("parse_rider_gen.py")


def stage_parse_schedules(ctx):
    for code in ctx.manifest["schedules"]:
        ctx.run("parse_schedule.py", code)


def stage_create_cross_reference(ctx):
    ctx.run("create_cross_reference_versions.py")


def stage_parse_riders(ctx):
    for code in ctx.manifest["riders"]:
        ctx.run("parse_rider.py", code)


def stage_load_rules(ctx):
    ctx.run("load_rules.py")


def stage_validate(ctx):
    ctx.run("validate_draft.py")


def stage_activate(ctx):
    # Gate first: if any draft is invalid, nothing is activated.
    ctx.run("validate_draft.py")
    drafts = ctx.draft_ids()
    for version_id in drafts:
        ctx.run("activate_version.py", version_id)
    ctx.notes = {
        "activated_ids": drafts,
        "already_active": ctx.status_count("active") - len(drafts),
    }


def stage_ingest_bills(ctx):
    for pdf in ctx.manifest["usage_pdfs"]:
        ctx.run("ingest_usage_history.py", pdf)


def stage_enrich_determinants(ctx):
    for pdf in ctx.manifest["usage_pdfs"]:
        ctx.run("enrich_determinants.py", pdf)


def stage_audit(ctx):
    ctx.run("audit_bills.py", "--mapping", ROOT / ctx.manifest["mapping"])


STAGE_FUNCTIONS = {
    "create_schema": stage_create_schema,
    "parse_gen": stage_parse_gen,
    "parse_schedules": stage_parse_schedules,
    "create_cross_reference": stage_create_cross_reference,
    "parse_riders": stage_parse_riders,
    "load_rules": stage_load_rules,
    "validate": stage_validate,
    "activate": stage_activate,
    "ingest_bills": stage_ingest_bills,
    "enrich_determinants": stage_enrich_determinants,
    "audit": stage_audit,
}


def plan(manifest, selected):
    """Human-readable commands per selected stage (for --dry-run)."""
    commands = {
        "create_schema": ["create_db.py"],
        "parse_gen": ["parse_rider_gen.py"],
        "parse_schedules": [f"parse_schedule.py {c}" for c in manifest["schedules"]],
        "create_cross_reference": ["create_cross_reference_versions.py"],
        "parse_riders": [f"parse_rider.py {c}" for c in manifest["riders"]],
        "load_rules": ["load_rules.py"],
        "validate": ["validate_draft.py"],
        "activate": [
            "validate_draft.py (gate)",
            "activate_version.py <id> for each draft, listed from the database at run time",
        ],
        "ingest_bills": [f"ingest_usage_history.py {p}" for p in manifest["usage_pdfs"]],
        "enrich_determinants": [f"enrich_determinants.py {p}" for p in manifest["usage_pdfs"]],
        "audit": [f"audit_bills.py --mapping {manifest['mapping']}"],
    }
    return {stage: commands[stage] for stage in selected}


def select_stages(stage, from_stage):
    if stage and from_stage:
        raise ValueError("--stage and --from-stage cannot be combined.")
    for name in [*(stage or []), *([from_stage] if from_stage else [])]:
        if name not in STAGES:
            raise ValueError(f"Unknown stage '{name}'. Stages: {', '.join(STAGES)}")
    if stage:
        return [name for name in STAGES if name in stage]
    if from_stage:
        return list(STAGES[STAGES.index(from_stage):])
    return list(STAGES)


# --------------------------------------------------------------- run + report
def summarize(stage_result):
    if stage_result["status"] != "done":
        return stage_result["status"]
    if stage_result["rows_changed"]:
        return f"done ({stage_result['rows_changed']} rows changed)"
    if stage_result["tables_created"]:
        return f"done ({stage_result['tables_created']} tables created)"
    return "done (no change)"


def run_pipeline(manifest_path, database, selected, log_dir):
    started = datetime.now(timezone.utc)
    run = {
        "started_at": started.isoformat(),
        "database": str(Path(database).resolve()),
        "manifest": str(Path(manifest_path).resolve()),
        "selected_stages": selected,
        "result": "success",
        "stages": [],
        "failure": None,
    }
    results = {stage: {"name": stage, "status": "skipped"} for stage in STAGES}
    snapshot = database_snapshot(database)
    run["before_hash"] = content_hash(snapshot)
    run["before_rows"] = table_rows(snapshot)
    try:
        manifest_file = Path(manifest_path)
        run["manifest_sha256"] = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
        manifest = load_manifest(manifest_file)
        preflight(manifest, selected)
    except (OSError, ValueError) as error:
        run["result"] = "failed"
        run["failure"] = {"stage": "preflight", "reason": str(error)}
        manifest = None

    if manifest is not None:
        for stage in selected:
            record = []
            ctx = Context(manifest, database, record)
            entry = results[stage]
            entry["commands"] = record
            try:
                STAGE_FUNCTIONS[stage](ctx)
                entry["status"] = "done"
            except StageFailure as error:
                entry["status"] = "FAILED"
                run["result"] = "failed"
                run["failure"] = {
                    "stage": stage,
                    "command": error.command,
                    "returncode": error.returncode,
                    "reason": error.output[-TAIL_LINES:],
                }
            except Exception as error:  # orchestrator-side errors (e.g. listing drafts)
                entry["status"] = "FAILED"
                run["result"] = "failed"
                run["failure"] = {"stage": stage, "reason": f"{type(error).__name__}: {error}"}
            after = database_snapshot(database)
            changed, created = diff_snapshots(snapshot, after)
            entry.update(
                {
                    "rows_changed": sum(changed.values()),
                    "tables_changed": changed,
                    "tables_created": created,
                    "before_hash": content_hash(snapshot),
                    "after_hash": content_hash(after),
                    "notes": ctx.notes,
                }
            )
            snapshot = after
            if entry["status"] == "FAILED":
                break

    for stage in selected:
        if results[stage]["status"] == "skipped":
            results[stage]["status"] = "not run"

    final = database_snapshot(database)
    run["after_hash"] = content_hash(final)
    run["after_rows"] = table_rows(final)
    for stage in STAGES:
        results[stage]["summary"] = summarize(results[stage])
        run["stages"].append(results[stage])
    run["finished_at"] = datetime.now(timezone.utc).isoformat()

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%S%fZ")
    log_path = log_dir / f"pipeline-{stamp}.json"
    log_path.write_text(json.dumps(run, indent=2, default=str) + "\n", encoding="utf-8")
    return run, log_path


def print_summary(run, log_path):
    print(f"Pipeline {run['result'].upper()} — database {run['database']}")
    for stage in run["stages"]:
        print(f"  {stage['name']:<24} {stage['summary']}")
    if run["failure"]:
        failure = run["failure"]
        print(f"\nFAILED at stage: {failure['stage']}")
        if "command" in failure:
            print(f"  command: {failure['command']} (exit {failure['returncode']})")
        reason = failure["reason"]
        for line in (reason[-6:] if isinstance(reason, list) else [reason]):
            print(f"  | {line}")
        print("  (full output tail is in the run manifest)")
        print(
            f"  database left at content hash {run['after_hash'][:16]} "
            f"(before: {run['before_hash'][:16]}); earlier stages' work is intact."
        )
    print(f"\ncontent hash before: {run['before_hash'][:16]}  after: {run['after_hash'][:16]}")
    print(f"run manifest: {log_path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--stage", action="append", help="Run only this stage (repeatable).")
    parser.add_argument("--from-stage", help="Run this stage and every later stage.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan; execute nothing.")
    args = parser.parse_args(argv)
    try:
        selected = select_stages(args.stage, args.from_stage)
    except ValueError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 2

    if args.dry_run:
        try:
            manifest = load_manifest(args.manifest)
        except (OSError, ValueError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 2
        print(f"DRY RUN — database {args.database.resolve()}; nothing is executed.")
        for stage, commands in plan(manifest, selected).items():
            print(f"  {stage}")
            for command in commands:
                print(f"    - {command}")
        skipped = [s for s in STAGES if s not in selected]
        if skipped:
            print(f"  skipped: {', '.join(skipped)}")
        return 0

    run, log_path = run_pipeline(args.manifest, args.database, selected, args.log_dir)
    print_summary(run, log_path)
    return 0 if run["result"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
