import argparse
try:
    from src import db
except ImportError:
    import db

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

        failures = validate_drafts(conn, version_id)
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


def activate_with_alert(conn, version_id):
    """Activate, then best-effort write the change alert against the prior version.

    Returns {"alert_path": ..., "warnings": [...]}; an alert failure never fails the activation.
    """
    warnings = []
    prior = conn.execute(
        """
        SELECT p.id, p.schedule_code FROM tariff_versions p JOIN tariff_versions n
          ON n.state = p.state AND n.utility = p.utility AND n.schedule_code = p.schedule_code
        WHERE n.id = ? AND p.id <> n.id AND p.status = 'active' AND p.effective_from < n.effective_from
        ORDER BY p.effective_from DESC, p.id DESC LIMIT 1
        """,
        (version_id,),
    ).fetchone()
    activate_version(conn, version_id)
    if prior is None:
        return {"alert_path": None, "warnings": warnings}
    try:
        if __package__:
            from .tariff_alerts import sample_bill_id_for, write_alert
        else:
            from tariff_alerts import sample_bill_id_for, write_alert
        path = write_alert(conn, prior[0], version_id, sample_bill_id_for(conn, prior[1]))
    except Exception as error:
        conn.rollback()
        warnings.append(f"Change alert not written: {type(error).__name__}: {error}")
        return {"alert_path": None, "warnings": warnings}
    return {"alert_path": str(path), "warnings": warnings}


def main():
    parser = argparse.ArgumentParser(
        description="Validate and activate a draft tariff version."
    )
    parser.add_argument("version_id", type=int, help="ID of the draft version")
    args = parser.parse_args()

    try:
        with db.connect(DATABASE_PATH) as conn:
            result = activate_with_alert(conn, args.version_id)
    except (db.Error, ValueError) as error:
        print(f"FAIL: {error}")
        return 1

    print(f"PASS: version {args.version_id} is active.")
    if result["alert_path"]:
        print(f"Change alert: {result['alert_path']}")
    for warning in result["warnings"]:
        print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
