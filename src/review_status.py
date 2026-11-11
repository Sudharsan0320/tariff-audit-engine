"""The one definition of the parse-review status constraint.

`create_db.py` creates `parse_review_items` with STATUS_CHECK. Operator actions
(src/operator_actions.py) close items as dismissed or corrected; Stage 8's resolve_item closes them as
resolved; 'approved' stays in the set for rows closed before the staff console was removed (2026-11-11). Every
database (fresh, clone, primary) carries exactly this check, under the Postgres name
CONSTRAINT_NAME. Nothing here alters a table.
"""

import re
import sqlite3

STATUSES = ("open", "resolved", "approved", "dismissed", "corrected")
CONSTRAINT_NAME = "parse_review_items_status_check"
STATUS_CHECK = "CHECK (status IN (" + ", ".join(f"'{status}'" for status in STATUSES) + "))"

NOT_WIDENED = (
    "parse_review_items' status constraint does not allow exactly "
    + ", ".join(STATUSES)
    + "; widen it in its own backup-gated step before using operator actions."
)


def allowed_statuses(conn):
    """The statuses the database's check allows, or None when the table has no status check."""
    if isinstance(conn, sqlite3.Connection):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'parse_review_items'").fetchone()
        if row is None or not row[0]:
            return None
        found = re.search(r"CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)\s*\)", row[0], re.I)
        return None if found is None else frozenset(re.findall(r"'([^']+)'", found.group(1)))
    row = conn.execute(
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
        "WHERE c.conrelid = 'parse_review_items'::regclass AND c.contype = 'c' AND c.conname = ?",
        (CONSTRAINT_NAME,)).fetchone()
    return None if row is None else frozenset(re.findall(r"'([^']+)'::text", row[0]))


def verify(conn):
    """A no-op when the check matches STATUSES (or the table has none); otherwise raise. Never alters."""
    allowed = allowed_statuses(conn)
    if allowed is not None and allowed != frozenset(STATUSES):
        raise RuntimeError(NOT_WIDENED)
    return allowed
