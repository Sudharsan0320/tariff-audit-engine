"""Manual-only build DAGs for the analytics layers (B21+), one task each, B19 target guard, no retries.

Each task opens the guarded target (scratch by default), runs the layer's build for the only account in
the marts, and logs a compact success record.
"""

from datetime import datetime

from airflow.decorators import dag, task

import tariff_common as tc

BUILDS = {
    # dag_id: (task_id, module, summary function name)
    "forecast_build": ("build_forecast", "forecasting", "dag_summary"),
    "segment_build": ("build_segments", "segmentation", "dag_summary"),
    "anomaly_scoring": ("score_bills", "anomaly_detection", "dag_summary"),
    "extraction_eval": ("evaluate_books", "extraction_eval", "dag_summary"),
}


def _make(dag_id, task_id, module, summary):
    @dag(dag_id=dag_id, schedule=None, start_date=datetime(2026, 11, 1), catchup=False, max_active_runs=1,
         default_args={"retries": 0}, tags=["tariff", "manual", "analytics"], doc_md=__doc__)
    def _dag():
        @task(task_id=task_id)
        def run():
            import importlib

            mod = importlib.import_module(f"src.{module}")
            with tc.connect() as conn:
                return tc.record(f"{dag_id} build", getattr(mod, summary)(conn))

        run()

    return _dag()


for _dag_id, (_task, _module, _summary) in BUILDS.items():
    globals()[_dag_id] = _make(_dag_id, _task, _module, _summary)
