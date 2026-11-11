"""Shared helpers for the tariff DAGs. Orchestration only: every task calls an existing src/ entry point.

The tariff database is the one TARIFF_DB_URL names. Compose sets it from AIRFLOW_TARIFF_DB, whose
default is the B19 scratch clone. As a second guard, any task refuses to run against the database
named by TARIFF_PRIMARY_DB_NAME unless AIRFLOW_TARIFF_ALLOW_PRIMARY is exactly "yes".
"""

import json
import logging
import os
from urllib.parse import urlparse

log = logging.getLogger("tariff")

RAW_DIR = "/opt/tariff/data/raw"


class TargetRefused(RuntimeError):
    """The task would have run against a database it is not allowed to touch."""


def target():
    url = os.environ.get("TARIFF_DB_URL", "").strip()
    if not url:
        raise TargetRefused("TARIFF_DB_URL is not set; refusing to fall back to a default database.")
    name = urlparse(url).path.lstrip("/")
    primary = os.environ.get("TARIFF_PRIMARY_DB_NAME", "").strip()
    if primary and name == primary and os.environ.get("AIRFLOW_TARIFF_ALLOW_PRIMARY") != "yes":
        raise TargetRefused(f"TARIFF_DB_URL names the primary database ({name}); refusing.")
    log.info("tariff database: %s", name)
    return url


def connect(readonly=False):
    from src import db
    return db.connect(target(), readonly=readonly)


def scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def standing_counts(conn):
    """Per-company version / component / rule counts, open queue items, structured-rule coverage. Reads only."""
    def by_utility(sql):
        return {row[0]: row[1] for row in conn.execute(sql).fetchall()}

    return {
        "versions": by_utility("SELECT utility, COUNT(*) FROM tariff_versions GROUP BY utility ORDER BY utility"),
        "versions_active": by_utility(
            "SELECT utility, COUNT(*) FROM tariff_versions WHERE status = 'active' GROUP BY utility ORDER BY utility"),
        "components": by_utility(
            "SELECT v.utility, COUNT(*) FROM tariff_components c JOIN tariff_versions v ON v.id = c.tariff_version_id "
            "GROUP BY v.utility ORDER BY v.utility"),
        "rules": by_utility(
            "SELECT v.utility, COUNT(*) FROM tariff_rules r JOIN tariff_versions v ON v.id = r.tariff_version_id "
            "GROUP BY v.utility ORDER BY v.utility"),
        "structured_rules": by_utility(
            "SELECT v.utility, COUNT(*) FROM structured_tariff_rules s JOIN tariff_versions v "
            "ON v.id = s.tariff_version_id GROUP BY v.utility ORDER BY v.utility"),
        "versions_with_structured_rules": by_utility(
            "SELECT v.utility, COUNT(DISTINCT s.tariff_version_id) FROM structured_tariff_rules s JOIN tariff_versions v "
            "ON v.id = s.tariff_version_id GROUP BY v.utility ORDER BY v.utility"),
        "queue_items_total": scalar(conn, "SELECT COUNT(*) FROM parse_review_items"),
        "queue_items_open": scalar(conn, "SELECT COUNT(*) FROM parse_review_items WHERE status = 'open'"),
        "audit_runs": scalar(conn, "SELECT COUNT(*) FROM audit_runs"),
        "audit_lines": scalar(conn, "SELECT COUNT(*) FROM audit_lines"),
    }


def record(title, payload):
    """Log the success record as one JSON block and return it (it also lands in XCom)."""
    log.info("%s\n%s", title, json.dumps(payload, indent=2, sort_keys=True, default=str))
    return payload
