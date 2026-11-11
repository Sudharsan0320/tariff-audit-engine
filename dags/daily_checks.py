"""daily_checks: a daily schedule, created paused. Read-only integrity checks plus the Stage 8 review scan.

The checks task writes nothing; it logs a run report of per-company version / component / rule counts,
open queue items and structured-rule coverage. The scan task runs parse_review.scan_for_review, which
writes only the queue items its own signals find (none when everything is already queued).
Then the B24 monitoring run writes its findings to schema `monitor` (after the scan, so queue numbers
include it). No retries.
"""

from datetime import datetime

from airflow.decorators import dag, task

import tariff_common as tc


@dag(
    dag_id="daily_checks",
    schedule="@daily",
    start_date=datetime(2026, 11, 1),
    catchup=False,
    is_paused_upon_creation=True,
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["tariff", "checks"],
    doc_md=__doc__,
)
def daily_checks():

    @task
    def integrity_report():
        with tc.connect(readonly=True) as conn:
            counts = tc.standing_counts(conn)
        return tc.record("daily integrity report", counts)

    @task
    def review_scan(report):
        from src import parse_review

        with tc.connect() as conn:
            before = tc.scalar(conn, "SELECT COUNT(*) FROM parse_review_items")
            scan = parse_review.scan_for_review(conn)
            after = tc.scalar(conn, "SELECT COUNT(*) FROM parse_review_items")
        return tc.record("review scan", {
            "created_by_signal": scan["created"],
            "total_created": scan["total_created"],
            "queue_items_before": before,
            "queue_items_after": after,
            "open_queue_items_in_report": report["queue_items_open"],
        })

    @task
    def monitoring(scan):
        from src import monitoring as mon

        with tc.connect() as conn:
            result = mon.run(conn)
        return tc.record("monitoring run", {**result, "scan_total_created": scan["total_created"]})

    monitoring(review_scan(integrity_report()))


daily_checks()
