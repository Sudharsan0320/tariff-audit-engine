"""mart_build: manual only. Full-refresh build of the B20 `mart` schema, then reconciliation.

Entry point: mart_build.build(conn), which commits only when every reconciliation check passes and
otherwise raises and writes nothing. The target is the B19 one: TARIFF_DB_URL from AIRFLOW_TARIFF_DB
(default the scratch), and the primary is refused unless AIRFLOW_TARIFF_ALLOW_PRIMARY=yes.
"""

from datetime import datetime

from airflow.decorators import dag, task

import tariff_common as tc


@dag(
    dag_id="mart_build",
    schedule=None,
    start_date=datetime(2026, 11, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["tariff", "manual", "marts"],
    doc_md=__doc__,
)
def mart_build():

    @task
    def build_and_reconcile():
        from src import mart_build as mb

        with tc.connect() as conn:
            result = mb.build(conn)
            hashes = mb.content_hashes(conn)
        report = result["reconciliation"]
        return tc.record("mart build", {
            "build_id": result["build_id"],
            "passed": report["passed"],
            "current_runs": report["current_runs"],
            "checks": [{"check": c["check"], "passed": c["passed"],
                        "detail": c["detail"] if "orphans" not in c["detail"]
                        else {"orphans_total": sum(c["detail"]["orphans"].values())}}
                       for c in report["checks"]],
            "source_counts": result["source_counts"],
            "mart_counts": result["mart_counts"],
            "content_hashes": {table: digest[:16] for table, digest in hashes.items()},
        })

    build_and_reconcile()


mart_build()
