"""Staff accounts and sessions. Passwords are salted PBKDF2 hashes, never plaintext."""

import hashlib
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

PBKDF2_ITERATIONS = 200_000
SESSION_HOURS = 12
ROLES = ("reviewer", "master")


class StaffAuthError(ValueError):
    """Login or session use is not allowed."""


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS utility_scopes (
            utility TEXT PRIMARY KEY,
            scope TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS staff_accounts (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('reviewer', 'master')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS staff_account_scopes (
            account_id INTEGER NOT NULL,
            scope TEXT NOT NULL,
            PRIMARY KEY (account_id, scope),
            FOREIGN KEY (account_id) REFERENCES staff_accounts(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS staff_sessions (
            token_hash TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (account_id) REFERENCES staff_accounts(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS staff_decisions (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('approve', 'correct', 'dismiss')),
            outcome TEXT NOT NULL CHECK (outcome IN ('applied', 'rejected_by_gate')),
            note TEXT,
            corrected_payload TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (account_id) REFERENCES staff_accounts(id)
        )
    """)
    conn.commit()


def allow_decision_statuses(conn):
    """Let a queue item record an applied decision. No-op where the table has no such check."""
    if isinstance(conn, sqlite3.Connection):
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'parse_review_items'"
        ).fetchone()
        if table is None or not table[0]:
            return
        sql = table[0]
        check = re.search(r"CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)\s*\)", sql, re.I)
        if check is None:
            return
        statuses = {value.casefold() for value in re.findall(r"'([^']+)'", check.group(1))}
        required = {"open", "resolved", "approved", "dismissed", "corrected"}
        if required <= statuses:
            return
        if statuses != {"open", "resolved"}:
            raise RuntimeError("Cannot safely extend the parse_review_items status constraint.")

        updated_sql = re.sub(
            r"(status\s+IN\s*\()\s*'open'\s*,\s*'resolved'\s*(\))",
            r"\1'open', 'resolved', 'approved', 'dismissed', 'corrected'\2",
            sql,
            count=1,
            flags=re.I,
        )
        if updated_sql == sql:
            raise RuntimeError("Could not extend the parse_review_items status constraint.")
        new_table = re.sub(
            r"^(CREATE\s+TABLE\s+)(?:IF\s+NOT\s+EXISTS\s+)?([\"`]?parse_review_items[\"`]?)",
            r"\1parse_review_items_staff_upgrade",
            updated_sql,
            count=1,
            flags=re.I,
        )
        if new_table == updated_sql:
            raise RuntimeError("Could not prepare the parse_review_items status migration.")

        columns = [row[1] for row in conn.execute("PRAGMA table_info(parse_review_items)")]
        if not columns or "status" not in columns:
            raise RuntimeError("parse_review_items has no readable status column.")
        column_list = ", ".join(f'"{column}"' for column in columns)
        indexes_and_triggers = [
            row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE tbl_name = 'parse_review_items' AND type IN ('index', 'trigger') "
                "AND sql IS NOT NULL"
            )
        ]
        conn.execute("SAVEPOINT staff_status_migration")
        try:
            conn.execute(new_table)
            conn.execute(
                f"INSERT INTO parse_review_items_staff_upgrade ({column_list}) "
                f"SELECT {column_list} FROM parse_review_items"
            )
            conn.execute("DROP TABLE parse_review_items")
            conn.execute(
                "ALTER TABLE parse_review_items_staff_upgrade RENAME TO parse_review_items"
            )
            for statement in indexes_and_triggers:
                conn.execute(statement)
            conn.execute("RELEASE staff_status_migration")
        except Exception:
            conn.execute("ROLLBACK TO staff_status_migration")
            conn.execute("RELEASE staff_status_migration")
            raise
        return

    conn.execute(
        "ALTER TABLE parse_review_items DROP CONSTRAINT IF EXISTS parse_review_items_status_check"
    )
    conn.execute(
        "ALTER TABLE parse_review_items ADD CONSTRAINT parse_review_items_status_check "
        "CHECK (status IN ('open', 'resolved', 'approved', 'dismissed', 'corrected'))"
    )
    conn.commit()


def hash_password(password, salt=None, iterations=PBKDF2_ITERATIONS):
    if not password:
        raise StaffAuthError("A password is required.")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def password_matches(password, stored):
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
    except (AttributeError, ValueError):
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    candidate = hash_password(password, bytes.fromhex(salt_hex), int(iterations))
    return secrets.compare_digest(candidate, stored)


def _now():
    return datetime.now(timezone.utc)


def seed_electric_utilities(conn):
    """Record each utility that has no scope row yet as electric.

    Later departments are additional rows. Reading an item never assumes electric.
    """
    conn.execute(
        "INSERT INTO utility_scopes(utility, scope) "
        "SELECT DISTINCT utility, 'electric' FROM tariff_versions "
        "WHERE utility NOT IN (SELECT utility FROM utility_scopes)"
    )
    conn.commit()


def create_account(conn, username, password, role, scopes):
    if role not in ROLES:
        raise StaffAuthError(f"Role must be one of {list(ROLES)}.")
    if not scopes:
        raise StaffAuthError("An account needs at least one scope.")
    existing = conn.execute(
        "SELECT id FROM staff_accounts WHERE username = ?", (username,)).fetchone()
    if existing:
        conn.execute("DELETE FROM staff_sessions WHERE account_id = ?", (existing[0],))
        conn.execute("DELETE FROM staff_account_scopes WHERE account_id = ?", (existing[0],))
        conn.execute("DELETE FROM staff_accounts WHERE id = ?", (existing[0],))
    created = _now().isoformat()
    cursor = conn.execute(
        "INSERT INTO staff_accounts(username, password_hash, role, active, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        (username, hash_password(password), role, created),
    )
    account_id = cursor.lastrowid
    for scope in sorted(set(scopes)):
        conn.execute(
            "INSERT INTO staff_account_scopes(account_id, scope) VALUES (?, ?)",
            (account_id, scope),
        )
    conn.commit()
    return account_id


def authenticate(conn, username, password):
    row = conn.execute(
        "SELECT id, password_hash, role, active FROM staff_accounts WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None or not row[3] or not password_matches(password, row[1]):
        raise StaffAuthError("The username or password is wrong.")
    return {"id": row[0], "username": username, "role": row[2], "scopes": _scopes(conn, row[0])}


def _scopes(conn, account_id):
    return [row[0] for row in conn.execute(
        "SELECT scope FROM staff_account_scopes WHERE account_id = ? ORDER BY scope",
        (account_id,),
    ).fetchall()]


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(conn, account_id):
    token = secrets.token_urlsafe(32)
    expires = (_now() + timedelta(hours=SESSION_HOURS)).isoformat()
    conn.execute(
        "INSERT INTO staff_sessions(token_hash, account_id, expires_at) VALUES (?, ?, ?)",
        (_token_hash(token), account_id, expires),
    )
    conn.commit()
    return token


def end_session(conn, token):
    if token:
        conn.execute("DELETE FROM staff_sessions WHERE token_hash = ?", (_token_hash(token),))
        conn.commit()


def account_for_token(conn, token):
    if not token:
        return None
    row = conn.execute(
        "SELECT a.id, a.username, a.role, a.active, s.expires_at "
        "FROM staff_sessions s JOIN staff_accounts a ON a.id = s.account_id "
        "WHERE s.token_hash = ?",
        (_token_hash(token),),
    ).fetchone()
    if row is None or not row[3]:
        return None
    expires = datetime.fromisoformat(row[4])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= _now():
        conn.execute("DELETE FROM staff_sessions WHERE token_hash = ?", (_token_hash(token),))
        conn.commit()
        return None
    return {"id": row[0], "username": row[1], "role": row[2], "scopes": _scopes(conn, row[0])}
