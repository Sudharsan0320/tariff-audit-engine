import argparse
import sqlite3

if __package__:
    from .validate_draft import DATABASE_PATH, validate_drafts
else:
    from validate_draft import DATABASE_PATH, validate_drafts


def activate_version(conn, version_id):
    """Validate and atomically activate one draft version."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        draft = conn.execute(
            """
            SELECT state, utility, schedule_code, effective_from
            FROM tariff_versions
            WHERE id = ? AND status = 'draft'
            """,
            (version_id,),
        ).fetchone()
        if draft is None:
            raise ValueError(f"Version {version_id} does not exist as a draft.")

        failures = validate_drafts(conn)
        if failures:
            raise ValueError(
                "Draft validation failed:\n- " + "\n- ".join(failures)
            )

        state, utility, schedule_code, effective_from = draft
        active_versions = conn.execute(
            """
            SELECT id, effective_from
            FROM tariff_versions
            WHERE state = ? AND utility = ? AND schedule_code = ? AND status = 'active'
            ORDER BY id
            """,
            (state, utility, schedule_code),
        ).fetchall()
        if len(active_versions) > 1:
            raise ValueError(
                f"Found {len(active_versions)} active versions for "
                f"{state} / {utility} / {schedule_code}; refusing activation."
            )

        if active_versions:
            active_id, active_effective_from = active_versions[0]
            if active_effective_from >= effective_from:
                raise ValueError(
                    f"Draft effective_from {effective_from} must be later than active "
                    f"version {active_id} effective_from {active_effective_from}."
                )
            conn.execute(
                "UPDATE tariff_versions SET effective_to = ? WHERE id = ?",
                (effective_from, active_id),
            )

        conn.execute(
            """
            UPDATE tariff_versions SET status = 'active'
            WHERE id = ? AND status = 'draft'
            """,
            (version_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Validate and activate a draft tariff version."
    )
    parser.add_argument("version_id", type=int, help="ID of the draft version")
    args = parser.parse_args()

    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            activate_version(conn, args.version_id)
    except (sqlite3.Error, ValueError) as error:
        print(f"FAIL: {error}")
        return 1

    print(f"PASS: version {args.version_id} is active.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
