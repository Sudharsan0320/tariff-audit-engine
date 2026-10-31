"""HTTP API over the tariff audit engine (Phase 8d).

No business logic lives here: reads are plain SELECTs, actions call the same
functions the pipeline stages use, and every gate refusal is returned as a 4xx
carrying the actual failure list. The database is chosen by TARIFF_DB_URL /
TARIFF_DB_PATH exactly like the stages. Posture (E5): local single-user tool,
no authentication, bind to localhost.
"""

from decimal import Decimal
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

try:
    from src import db
    from src.activate_version import activate_with_alert
    from src.audit_bills import run_audits
    from src.audit_report import (
        AuditReportError,
        AuditRunNotFound,
        load_report_data,
        render_report,
    )
    from src.ingest_usage_history import ingest_usage_pdf
    from src.line_validation import BillNotFound, ValidationRefused, validate_bill_lines
    from src.anomaly import AccountNotFound as AnomalyAccountNotFound, AnomalyError, account_anomalies
    from src.lookback import AccountNotFound, LookbackError, account_lookback
    from src import weather_check
    from src.parse_review import ItemNotFound, ParseReviewError, list_items, resolve_item
    from src.parse_rider import parse_rider
    from src.schedule_comparison import (
        ComparisonRefused,
        load_comparison_data,
        render_comparison,
    )
    from src.schedule_review import (
        RepricingError,
        ReviewRefused,
        get_review_state,
        record_decision,
        select_schedule,
    )
    from src.tariff_alerts import AlertError, build_alert
    from src.tariff_diff import DiffError, diff_versions
    from src.validate_draft import validate_drafts
except ImportError:
    import db
    from activate_version import activate_with_alert
    from audit_bills import run_audits
    from audit_report import (
        AuditReportError,
        AuditRunNotFound,
        load_report_data,
        render_report,
    )
    from ingest_usage_history import ingest_usage_pdf
    from line_validation import BillNotFound, ValidationRefused, validate_bill_lines
    from anomaly import AccountNotFound as AnomalyAccountNotFound, AnomalyError, account_anomalies
    from lookback import AccountNotFound, LookbackError, account_lookback
    import weather_check
    from parse_review import ItemNotFound, ParseReviewError, list_items, resolve_item
    from parse_rider import parse_rider
    from schedule_comparison import (
        ComparisonRefused,
        load_comparison_data,
        render_comparison,
    )
    from schedule_review import (
        RepricingError,
        ReviewRefused,
        get_review_state,
        record_decision,
        select_schedule,
    )
    from tariff_alerts import AlertError, build_alert
    from tariff_diff import DiffError, diff_versions
    from validate_draft import validate_drafts

ROOT = Path(__file__).resolve().parents[1]
UPLOAD_DIR = Path(os.environ.get("TARIFF_UPLOAD_DIR") or ROOT / "data" / "uploads")
PAGE = ROOT / "src" / "static" / "index.html"

app = FastAPI(title="Tariff Audit Engine", version="1.0")


def target():
    return db.default_target(ROOT / "data" / "tariffs.db")


def query(sql, params=()):
    """Run one read-only query and return a list of dicts."""
    conn = db.connect(target(), readonly=True)
    try:
        cursor = conn.execute(sql, params)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def refuse(status, message, failures=None, **extra):
    detail = {"message": message, "failures": failures or []}
    detail.update(extra)
    raise HTTPException(status_code=status, detail=detail)


def save_upload(upload: UploadFile):
    data = upload.file.read()
    if not data.startswith(b"%PDF"):
        refuse(422, "The uploaded file is not a PDF.", ["File does not start with %PDF."])
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = Path(upload.filename or "upload.pdf").name
    path = UPLOAD_DIR / f"{hashlib.sha256(data).hexdigest()[:12]}-{safe}"
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------- reads
@app.get("/health")
def health():
    try:
        query("SELECT 1 AS ok")
    except db.Error as error:
        raise HTTPException(status_code=503, detail=f"database unavailable: {error}")
    return {"status": "ok", "database": db.describe(target())}


@app.get("/tariff-versions")
def list_versions(status: Optional[str] = None, schedule_code: Optional[str] = None):
    sql = "SELECT * FROM tariff_versions WHERE 1 = 1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if schedule_code:
        sql += " AND schedule_code = ?"
        params.append(schedule_code)
    return query(sql + " ORDER BY id", params)


@app.get("/tariff-versions/{version_id}")
def get_version(version_id: int):
    versions = query("SELECT * FROM tariff_versions WHERE id = ?", (version_id,))
    if not versions:
        refuse(404, f"No tariff version {version_id}.")
    return {
        **versions[0],
        "components": query(
            "SELECT * FROM tariff_components WHERE tariff_version_id = ? ORDER BY id",
            (version_id,),
        ),
        "rules": query(
            "SELECT * FROM tariff_rules WHERE tariff_version_id = ? ORDER BY id",
            (version_id,),
        ),
    }


@app.get("/accounts")
def list_accounts():
    return query("SELECT * FROM accounts ORDER BY account_number")


@app.get("/accounts/{account_number}/lookback")
def get_account_lookback(account_number: str):
    conn = db.connect(target(), readonly=True)
    try:
        return _plain(account_lookback(conn, account_number))
    except AccountNotFound as error:
        refuse(404, str(error))
    except (LookbackError, RepricingError) as error:
        refuse(422, "The lookback was refused.", [str(error)])
    finally:
        conn.close()


@app.get("/accounts/{account_number}/anomalies")
def get_account_anomalies(account_number: str):
    conn = db.connect(target(), readonly=True)
    try:
        result = account_anomalies(conn, account_number)
        weather = weather_check.load_default_weather()
        if weather is not None:
            result = weather_check.annotate(result, weather)
        return _plain(result)
    except AnomalyAccountNotFound as error:
        refuse(404, str(error))
    except (AnomalyError, RepricingError, weather_check.WeatherError) as error:
        refuse(422, "The anomaly scan was refused.", [str(error)])
    finally:
        conn.close()


@app.get("/bills/{bill_id}/line-validation")
def get_line_validation(bill_id: int):
    conn = db.connect(target(), readonly=True)
    try:
        return _plain(validate_bill_lines(conn, bill_id))
    except BillNotFound as error:
        refuse(404, str(error))
    except ValidationRefused as error:
        refuse(422, "The line validation was refused.", [str(error)])
    finally:
        conn.close()


@app.get("/bills")
def list_bills(account_number: Optional[str] = None):
    sql = "SELECT * FROM bills"
    params = []
    if account_number:
        sql += " WHERE account_number = ?"
        params.append(account_number)
    return query(sql + " ORDER BY bill_from, id", params)


@app.get("/bills/{bill_id}")
def get_bill(bill_id: int):
    bills = query("SELECT * FROM bills WHERE id = ?", (bill_id,))
    if not bills:
        refuse(404, f"No bill {bill_id}.")
    return {
        **bills[0],
        "charges": query("SELECT * FROM bill_charges WHERE bill_id = ? ORDER BY id", (bill_id,)),
        "determinants": query(
            "SELECT * FROM bill_determinants WHERE bill_id = ? ORDER BY id", (bill_id,)
        ),
    }


def audit_view(bill_id):
    runs = query("SELECT * FROM audit_runs WHERE bill_id = ? ORDER BY id", (bill_id,))
    for run in runs:
        run["report"] = json.loads(run.pop("report_json"))
        run["lines"] = query(
            "SELECT * FROM audit_lines WHERE audit_run_id = ? ORDER BY id", (run["id"],)
        )
        for line in run["lines"]:
            line["detail"] = json.loads(line["detail"]) if line["detail"] else None
    return {"bill_id": bill_id, "runs": runs}


@app.get("/bills/{bill_id}/audit")
def get_audit(bill_id: int):
    if not query("SELECT 1 AS x FROM bills WHERE id = ?", (bill_id,)):
        refuse(404, f"No bill {bill_id}.")
    return audit_view(bill_id)


@app.get("/bills/{bill_id}/audit/report")
def get_audit_report(bill_id: int):
    conn = db.connect(target(), readonly=True)
    try:
        run, lines, components, mapping = load_report_data(conn, bill_id=bill_id)
        markdown = render_report(run, lines, components, mapping)
    except AuditRunNotFound as error:
        refuse(404, str(error))
    except AuditReportError as error:
        refuse(422, "The audit report was refused.", [str(error)])
    finally:
        conn.close()
    return Response(content=markdown, media_type="text/markdown")


def _bill_or_404(bill_id):
    if not query("SELECT 1 AS x FROM bills WHERE id = ?", (bill_id,)):
        refuse(404, f"No bill {bill_id}.")


def _plain(value):
    """JSON-safe copy that keeps money exact (Decimal -> string, never float)."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


class DecisionBody(BaseModel):
    schedule_code: str
    decision: str
    reviewer: str
    note: Optional[str] = None


class SelectBody(BaseModel):
    schedule_code: str


@app.get("/bills/{bill_id}/schedule-review")
def get_schedule_review(bill_id: int):
    _bill_or_404(bill_id)
    conn = db.connect(target(), readonly=True)
    try:
        return _plain(get_review_state(conn, bill_id))
    except (ReviewRefused, RepricingError) as error:
        refuse(422, "The schedule review was refused.", [str(error)])
    finally:
        conn.close()


@app.post("/bills/{bill_id}/schedule-review/decision")
def post_schedule_decision(bill_id: int, body: DecisionBody):
    _bill_or_404(bill_id)
    try:
        with db.connect(target()) as conn:
            row = record_decision(
                conn, bill_id, body.schedule_code, body.decision, body.reviewer, body.note
            )
    except ReviewRefused as error:
        refuse(422, "The decision was refused.", [str(error)])
    return row


@app.post("/bills/{bill_id}/schedule-review/select")
def post_schedule_select(bill_id: int, body: SelectBody):
    _bill_or_404(bill_id)
    try:
        with db.connect(target()) as conn:
            row = select_schedule(conn, bill_id, body.schedule_code)
    except (ReviewRefused, RepricingError) as error:
        refuse(422, "The selection was refused.", [str(error)])
    return row


@app.get("/bills/{bill_id}/schedule-comparison")
def get_schedule_comparison(bill_id: int):
    _bill_or_404(bill_id)
    conn = db.connect(target(), readonly=True)
    try:
        markdown = render_comparison(*load_comparison_data(conn, bill_id))
    except (ComparisonRefused, RepricingError) as error:
        refuse(422, "The schedule comparison was refused.", [str(error)])
    finally:
        conn.close()
    return Response(content=markdown, media_type="text/markdown")


class ResolveBody(BaseModel):
    reviewer: str
    note: str


@app.get("/parse-review")
def get_parse_review(status: Optional[str] = None):
    conn = db.connect(target(), readonly=True)
    try:
        return _plain(list_items(conn, status))
    except ParseReviewError as error:
        refuse(422, "The parse-review query was refused.", [str(error)])
    except db.Error as error:
        refuse(422, "The parse-review queue is unavailable.",
               [f"{error}; run src/create_db.py to add the table."])
    finally:
        conn.close()


@app.post("/parse-review/{item_id}/resolve")
def post_parse_review_resolve(item_id: int, body: ResolveBody):
    try:
        with db.connect(target()) as conn:
            return _plain(resolve_item(conn, item_id, body.reviewer, body.note))
    except ItemNotFound as error:
        refuse(404, str(error))
    except ParseReviewError as error:
        refuse(422, "The resolution was refused.", [str(error)])


# ---------------------------------------------------------------- actions
@app.post("/bills/{bill_id}/audit")
def audit_bill(bill_id: int):
    if not query("SELECT 1 AS x FROM bills WHERE id = ?", (bill_id,)):
        refuse(404, f"No bill {bill_id}.")
    try:
        with db.connect(target()) as conn:
            result = run_audits(conn, bill_ids=[bill_id])
    except ValueError as error:
        refuse(422, "The audit was refused.", [str(error)])
    return {"statuses": result["statuses"], **audit_view(bill_id)}


@app.post("/tariff-versions/{version_id}/activate")
def activate(version_id: int):
    try:
        with db.connect(target()) as conn:
            outcome = activate_with_alert(conn, version_id)
    except ValueError as error:
        refuse(422, "Activation refused.", [str(error)])
    return {
        "version_id": version_id,
        "status": "active",
        "alert_path": outcome["alert_path"],
        "warnings": outcome["warnings"],
    }


def _version_or_404(version_id):
    if not query("SELECT 1 AS x FROM tariff_versions WHERE id = ?", (version_id,)):
        refuse(404, f"No tariff version {version_id}.")


@app.get("/tariff-versions/{new_id}/diff")
def get_version_diff(new_id: int, against: int):
    _version_or_404(new_id)
    _version_or_404(against)
    conn = db.connect(target(), readonly=True)
    try:
        return _plain(diff_versions(conn, against, new_id))
    except DiffError as error:
        refuse(422, "The version diff was refused.", [str(error)])
    finally:
        conn.close()


@app.get("/tariff-versions/{new_id}/alert")
def get_version_alert(new_id: int, against: int, bill: int):
    _version_or_404(new_id)
    _version_or_404(against)
    _bill_or_404(bill)
    conn = db.connect(target(), readonly=True)
    try:
        markdown = build_alert(conn, against, new_id, bill)
    except (AlertError, RepricingError) as error:
        refuse(422, "The change alert was refused.", [str(error)])
    finally:
        conn.close()
    return Response(content=markdown, media_type="text/markdown")


@app.post("/uploads/bill")
def upload_bill(file: UploadFile = File(...)):
    path = save_upload(file)
    try:
        with db.connect(target()) as conn:
            draft, status, validation, _ = ingest_usage_pdf(conn, path)
    except Exception as error:  # unreadable or non-EAP PDF: a refusal, not a server fault
        refuse(422, "The bill PDF could not be parsed.", [f"{type(error).__name__}: {error}"])
    if validation["failures"]:
        refuse(
            422,
            "The usage gate refused this bill history.",
            validation["failures"],
            warnings=validation["warnings"],
        )
    return {
        "status": status,
        "account_number": draft.get("account", {}).get("account_number"),
        "bills_in_file": len(draft.get("bills", [])),
        "warnings": validation["warnings"],
        "information": validation.get("information", []),
    }


@app.post("/uploads/tariff")
def upload_tariff(
    kind: str = Form(..., description="'schedule' or 'rider'"),
    code: str = Form(..., description="Schedule or rider code, e.g. 110 or T-CM"),
    file: UploadFile = File(...),
):
    if kind not in ("schedule", "rider"):
        refuse(422, "kind must be 'schedule' or 'rider'.", [f"Got {kind!r}."])
    path = save_upload(file)
    if kind == "rider":
        try:
            version_id, count = parse_rider(code, pdf_path=path, database_path=target())
        except Exception as error:
            refuse(422, "The rider could not be parsed.", [f"{type(error).__name__}: {error}"])
    else:
        env = {k: v for k, v in os.environ.items() if k not in ("TARIFF_DB_URL", "TARIFF_DB_PATH")}
        env["TARIFF_DB_URL" if db.is_postgres(target()) else "TARIFF_DB_PATH"] = str(target())
        result = subprocess.run(
            [sys.executable, str(ROOT / "src" / "parse_schedule.py"), code, "--pdf", str(path)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            lines = (result.stdout + result.stderr).strip().splitlines()
            refuse(422, "The schedule could not be parsed.", lines[-5:])
        wanted = code
        found = query(
            "SELECT id FROM tariff_versions WHERE schedule_code = ? ORDER BY id DESC", (wanted,)
        )
        if not found:
            refuse(422, "The parser wrote no version.", [f"No version for {code}."])
        version_id, count = found[0]["id"], None
    with db.connect(target(), readonly=True) as conn:
        version = conn.execute(
            "SELECT status FROM tariff_versions WHERE id = ?", (version_id,)
        ).fetchone()
    if version and version[0] != "draft":
        return {"version_id": version_id, "status": version[0], "note": "already active; nothing changed"}
    with db.connect(target(), readonly=True) as conn:
        failures = validate_drafts(conn, version_id)
    if failures:
        refuse(
            422,
            "The validation gate refused this draft; it stays a draft.",
            failures,
            version_id=version_id,
        )
    return {"version_id": version_id, "status": "draft", "components": count, "validated": True}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(PAGE)
