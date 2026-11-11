"""tariff_book_ingestion: manual only. Intake of one book file, then the routed adapter (the packet
coordinator for AI-route utilities, the hand-written parsers for Dominion).

Run config: {"book_path": "APCo-Tariff-28.pdf"} (a file in data/raw, or an absolute path inside it).
Entry points: document_intake.save_upload, identify_document, record_intake, route_tariff.
"""

from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task
from airflow.models.param import Param

import tariff_common as tc


@dag(
    dag_id="tariff_book_ingestion",
    schedule=None,
    start_date=datetime(2026, 11, 1),
    catchup=False,
    max_active_runs=1,
    params={"book_path": Param("", type="string", description="Book file in data/raw")},
    tags=["tariff", "manual"],
    doc_md=__doc__,
)
def tariff_book_ingestion():

    @task
    def intake(**context):
        from src import ai_extract, document_intake

        name = str(context["params"].get("book_path") or "").strip()
        if not name:
            raise ValueError("Run config must give book_path.")
        raw = Path(tc.RAW_DIR).resolve()
        path = (raw / name).resolve() if not Path(name).is_absolute() else Path(name).resolve()
        if raw not in path.parents or not path.is_file():
            raise ValueError(f"book_path must name a file inside {raw}: {name!r}")
        stored, sha256, size = document_intake.save_upload(
            path.read_bytes(), path.name, Path(__import__("os").environ["TARIFF_UPLOAD_DIR"]))
        detection = document_intake.identify_document(stored)
        with tc.connect(readonly=True) as conn:
            before = tc.standing_counts(conn)
        return tc.record("intake", {
            "book": path.name, "stored_as": str(stored), "sha256": sha256, "bytes": size,
            "detection": {k: detection.get(k) for k in (
                "doc_kind", "utility", "utility_id", "state", "version_label", "effective_date",
                "rider_designation", "reasons")},
            "model_key_configured": ai_extract.api_key() is not None,
            "before": before,
        })

    @task(retries=1, retry_delay=timedelta(minutes=1))
    def route_and_run(intake_record, **context):
        from src import document_intake

        stored = intake_record["stored_as"]
        detection = document_intake.identify_document(stored)
        result = document_intake.route_tariff(stored, detection, target=tc.target())
        plain = document_intake.plain_words(detection, result["status"], result=result)
        document_intake.record_intake(stored, intake_record["book"], detection, result["status"], plain)
        with tc.connect(readonly=True) as conn:
            after = tc.standing_counts(conn)
        before = intake_record["before"]
        return tc.record("route_and_run", {
            "status": result["status"],
            "versions_created": result.get("versions_created"),
            "versions_existing": result.get("versions_existing"),
            "versions_held_as_draft": len(result.get("stayed_draft") or []),
            "queued_for_review_reported": result.get("queued_for_review"),
            "queue_items_created": after["queue_items_total"] - before["queue_items_total"],
            "declined": [d.get("schedule_code") for d in result.get("declined") or []],
            "message": result.get("message"),
            "plain_outcome": plain.get("outcome"),
            "after": after,
        })

    route_and_run(intake())


tariff_book_ingestion()
