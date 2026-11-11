"""account_audit: manual only. Runs the bill audit for one account's configured pilot bills.

Run config: {"account_number": "001172485896"}.
Entry point: audit_bills.run_audits(conn) with its default pilot selection, exactly as the CLI runs it.

run_audits has no per-account selection: it audits the reviewed mapping's pilot bills (or explicit
bill ids, where one uncovered bill fails the whole run). This DAG therefore does not choose bills.
Before writing anything it checks, read-only, that every bill in the configured pilot belongs to the
requested account, and refuses otherwise.
"""

from datetime import datetime, timedelta

from airflow.decorators import dag, task
from airflow.models.param import Param

import tariff_common as tc


@dag(
    dag_id="account_audit",
    schedule=None,
    start_date=datetime(2026, 11, 1),
    catchup=False,
    max_active_runs=1,
    params={"account_number": Param("", type="string", description="Stored account number")},
    tags=["tariff", "manual"],
    doc_md=__doc__,
)
def account_audit():

    @task(retries=1, retry_delay=timedelta(minutes=1))
    def audit_account(**context):
        import json

        from src import audit_bills

        account = str(context["params"].get("account_number") or "").strip()
        if not account:
            raise ValueError("Run config must give account_number.")
        with tc.connect() as conn:
            known = tc.scalar(conn, "SELECT COUNT(*) FROM accounts WHERE account_number = ?", (account,))
            if not known:
                raise ValueError(f"No stored account {account}.")
            mapping, _hash = audit_bills.load_mapping()
            pilot = audit_bills._pilot_bill_ids(conn, mapping)
            owners = {row[0] for row in conn.execute(
                f"SELECT DISTINCT account_number FROM bills WHERE id IN ({', '.join('?' for _ in pilot)})",
                tuple(pilot)).fetchall()} if pilot else set()
            if not pilot or owners != {account}:
                raise ValueError(
                    f"The configured pilot bills {pilot} belong to {sorted(owners)}, not only {account}; "
                    "run_audits has no per-account selection, so nothing was audited.")
            runs_before = tc.scalar(conn, "SELECT COUNT(*) FROM audit_runs")
            lines_before = tc.scalar(conn, "SELECT COUNT(*) FROM audit_lines")
            result = audit_bills.run_audits(conn)
            runs_after = tc.scalar(conn, "SELECT COUNT(*) FROM audit_runs")
            lines_after = tc.scalar(conn, "SELECT COUNT(*) FROM audit_lines")
            totals = {}
            for bill_id in result["bill_ids"]:
                report = conn.execute(
                    "SELECT report_json FROM audit_runs WHERE bill_id = ? AND mapping_version = ? "
                    "ORDER BY id DESC LIMIT 1", (bill_id, result["mapping_version"])).fetchone()
                totals[bill_id] = json.loads(report[0])["like_for_like_totals"] if report else None
        return tc.record("audit_account", {
            "account_number": account,
            "mapping_version": result["mapping_version"],
            "bill_ids": result["bill_ids"],
            "statuses": result["statuses"],
            "audit_runs_written": runs_after - runs_before,
            "audit_lines_written": lines_after - lines_before,
            "like_for_like_totals": totals,
            "audit_snapshot_hash": result["audit_snapshot_hash"],
        })

    audit_account()


account_audit()
