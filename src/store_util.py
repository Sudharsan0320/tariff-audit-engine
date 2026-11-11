"""Shared helpers for the analytics stores (B21+): dialect-aware schema attach, exact values, content hashes.

Postgres uses real schemas; SQLite (test fixtures) attaches one database per schema name. Money and
measure columns are DECIMAL on Postgres (the db shim rewrites NUMERIC to DOUBLE PRECISION) and decimal
text on SQLite.
"""

import hashlib
import json
import sqlite3
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def is_sqlite(conn):
    return isinstance(conn, sqlite3.Connection)


def num_type(conn):
    return "TEXT" if is_sqlite(conn) else "DECIMAL"


def attach(conn, paths=None):
    """SQLite only: attach each schema name in `paths` ({name: file or None for memory}) if not attached."""
    if not is_sqlite(conn):
        return
    names = {row[1] for row in conn.execute("PRAGMA database_list").fetchall()}
    for name, path in (paths or {}).items():
        if name not in names:
            conn.execute(f"ATTACH DATABASE ? AS {name}", (str(path or ":memory:"),))


def ensure_schema(conn, schema):
    if not is_sqlite(conn):
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def dec(value):
    if value is None or value == "":
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def cents(value):
    return None if value is None else dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def param(conn, value):
    return str(value) if isinstance(value, Decimal) and is_sqlite(conn) else value


def rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, r)) for r in cursor.fetchall()]


def insert(conn, table, records):
    if not records:
        return 0
    cols = list(records[0])
    conn.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                     [tuple(param(conn, r[c]) for c in cols) for r in records])
    return len(records)


def table_hash(conn, table, order, exclude=()):
    """sha256 over a table's rows in `order`, skipping `exclude` columns (e.g. timestamps)."""
    cursor = conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
    names = [c[0] for c in cursor.description]
    digest = hashlib.sha256()
    for row in cursor.fetchall():
        values = [str(dec(v).normalize()) if isinstance(v, Decimal) else v
                  for n, v in zip(names, row) if n not in exclude]
        digest.update(json.dumps(values, default=str).encode() + b"\n")
    return digest.hexdigest()
