"""Create the staff tables and the three verification accounts on the database named by the environment.

Passwords come from the environment and are stored only as salted hashes.
"""

import os
import sys

try:
    from src import db
    from src.staff_accounts import (
    allow_decision_statuses,
    create_account,
    ensure_tables,
    seed_electric_utilities,
)
except ImportError:
    import db
    from staff_accounts import (
    allow_decision_statuses,
    create_account,
    ensure_tables,
    seed_electric_utilities,
)

ROOT_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tariffs.db")

ACCOUNTS = (
    ("electric.reviewer", "STAFF_SEED_ELECTRIC_REVIEWER_PASSWORD", "reviewer", ["electric"]),
    ("master", "STAFF_SEED_MASTER_PASSWORD", "master", ["electric"]),
    ("water.reviewer", "STAFF_SEED_WATER_REVIEWER_PASSWORD", "reviewer", ["water"]),
)


def seed(conn):
    ensure_tables(conn)
    allow_decision_statuses(conn)
    seed_electric_utilities(conn)
    created = []
    for username, env_name, role, scopes in ACCOUNTS:
        password = os.environ.get(env_name, "").strip()
        if not password:
            raise SystemExit(f"{env_name} is not set.")
        create_account(conn, username, password, role, scopes)
        created.append(username)
    return created


def main():
    target = db.default_target(ROOT_DEFAULT)
    conn = db.connect(target)
    try:
        created = seed(conn)
    finally:
        conn.close()
    print("Seeded staff accounts:", ", ".join(created))
    return 0


if __name__ == "__main__":
    sys.exit(main())
