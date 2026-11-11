"""feature_build: manual only. Builds feature-set version v1 (src.feature_pipeline.FEATURE_SET_VERSION)
into schema `features` from the `mart` schema, with the pipeline's checks.

The target follows the B19 guard: TARIFF_DB_URL from AIRFLOW_TARIFF_DB (default the scratch), and the
primary is refused unless AIRFLOW_TARIFF_ALLOW_PRIMARY=yes. The target must already have `mart` (run
mart_build first).
"""

from datetime import datetime

from airflow.decorators import dag, task

import tariff_common as tc


@dag(
    dag_id="feature_build",
    schedule=None,
    start_date=datetime(2026, 11, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["tariff", "manual", "features"],
    doc_md=__doc__,
)
def feature_build():

    @task
    def build_features():
        from src import feature_pipeline as fp

        with tc.connect() as conn:
            result = fp.build(conn)
            hashes = fp.content_hashes(conn, result["version"])
        return tc.record("feature build", {
            "build_id": result["build_id"], "version": result["version"], "bills": result["bills"],
            "features": result["features"], "rows": result["rows"],
            "checks": result["checks"],
            "content_hashes": {table: digest[:16] for table, digest in hashes.items()},
        })

    build_features()


feature_build()
