"""Staff review site. Separate from the customer API: its own app, pages, and port.

Logged-out pages redirect to login. Logged-out data calls are refused.
Queue reads go through staff_queue, which enforces scope. Actions go through
staff_actions: a correction is stored only when the pipeline verifier accepts it.
"""

import os
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

try:
    from src import db
    from src.staff_accounts import (
        StaffAuthError,
        account_for_token,
        authenticate,
        end_session,
        start_session,
    )
    from src.staff_actions import (
        StaffActionError,
        approve_item,
        correct_item,
        decisions_for,
        dismiss_item,
    )
    from src.staff_queue import StaffQueueError, item_detail, open_queue
except ImportError:
    import db
    from staff_accounts import (
        StaffAuthError,
        account_for_token,
        authenticate,
        end_session,
        start_session,
    )
    from staff_actions import (
        StaffActionError,
        approve_item,
        correct_item,
        decisions_for,
        dismiss_item,
    )
    from staff_queue import StaffQueueError, item_detail, open_queue

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "staff_static"
COOKIE = "staff_session"
SIGN_IN_REQUIRED = "Sign in is required."

app = FastAPI(title="Tariff staff review", version="1.0")
app.mount("/staff-static", StaticFiles(directory=STATIC_DIR), name="staff-static")


def target():
    return db.default_target(ROOT / "data" / "tariffs.db")


def _connect():
    return db.connect(target())


def _token(request: Request):
    return request.cookies.get(COOKIE)


def _account(conn, request: Request):
    return account_for_token(conn, _token(request))


def _page(title, body):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="stylesheet" href="/staff-static/staff.css">
</head>
<body>
<header>
  <a class="brand" href="/">Staff review</a>
  <span class="site">Separate from the customer site</span>
</header>
<main>
{body}
</main>
</body>
</html>
"""


def _login_page(message=""):
    note = f'<p class="warn">{escape(message)}</p>' if message else ""
    return HTMLResponse(_page("Sign in", f"""
<h1>Sign in</h1>
<p class="lead">Staff queue. A reviewer sees only the departments assigned to the account.</p>
{note}
<form method="post" action="/login">
  <label>Username <input name="username" autocomplete="username" required></label>
  <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
  <button type="submit">Sign in</button>
</form>
"""))


def _queue_page(account, items):
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td><a href=\"/items/{item['id']}\">{item['id']}</a></td>"
            f"<td>{escape(item['company'] or '')}</td>"
            f"<td>{escape(item['schedule'] or '')}</td>"
            f"<td>{escape(item['kind'])}</td>"
            f"<td>{escape(item['reason'])}</td>"
            f"<td>{escape(item['page'] or 'No cited page')}</td>"
            f"<td>{escape(item['value'] or 'No extracted value')}</td>"
            "</tr>"
        )
    body = "\n".join(rows) or "<tr><td colspan=\"7\">No open items in your departments.</td></tr>"
    return HTMLResponse(_page("Open queue", f"""
<div class="bar">
  <p class="who">{escape(account['username'])} · {escape(account['role'])} · {escape(", ".join(account['scopes']))}</p>
  <form method="post" action="/logout"><button type="submit">Log out</button></form>
</div>
<p class="count">Open items <strong id="open-count">{len(items)}</strong></p>
<p><a href="/decisions">Decisions</a></p>
<table>
  <thead><tr><th>Item</th><th>Company</th><th>Schedule</th><th>Kind</th><th>Reason</th><th>Page</th><th>Extracted value</th></tr></thead>
  <tbody>
  {body}
  </tbody>
</table>
"""))


def _detail_page(account, item):
    fields = [
        ("Company", item["company"]),
        ("Schedule", item["schedule"]),
        ("Kind", item["kind"]),
        ("Reason", item["reason"]),
        ("Cited page", item["page"] or "No cited page"),
        ("Extracted value", item["value"] or "No extracted value"),
        ("Status", item["status"]),
        ("Signal", item["signal"]),
        ("Field", item["field"]),
        ("Source reference", item["source_ref"]),
        ("Stored detail", item["detail"]),
    ]
    lines = "\n".join(
        f"<dt>{escape(label)}</dt><dd>{escape('' if value is None else str(value))}</dd>"
        for label, value in fields
    )
    return HTMLResponse(_page(f"Item {item['id']}", f"""
<div class="bar">
  <p class="who">{escape(account['username'])}</p>
  <form method="post" action="/logout"><button type="submit">Log out</button></form>
</div>
<p><a href="/">Back to the queue</a></p>
<h1>Item {item['id']}</h1>
{item.get('_banner') or ''}
<dl>
{lines}
</dl>
{_action_forms(item)}
{_decision_list(item.get('decisions') or [])}
"""))


def _decision_list(decisions):
    if not decisions:
        return "<h2>Decisions</h2><p>No decisions yet.</p>"
    rows = []
    for record in decisions:
        rows.append(
            "<tr>"
            f"<td>{escape(record['created_at'])}</td>"
            f"<td>{escape(record['username'])}</td>"
            f"<td>{escape(record['action'])}</td>"
            f"<td>{escape(record['outcome'])}</td>"
            f"<td>{escape(record.get('note') or '')}</td>"
            "</tr>"
        )
    return (
        "<h2>Decisions</h2><table><thead><tr><th>When</th><th>Reviewer</th>"
        "<th>Action</th><th>Outcome</th><th>Note</th></tr></thead><tbody>"
        + "\n".join(rows) + "</tbody></table>"
    )


def _action_forms(item):
    if item.get("status") != "open":
        return ""
    forms = f"""
<h2>Actions</h2>
<form method="post" action="/items/{item['id']}/dismiss">
  <label>Dismiss, with a note <input name="note" required></label>
  <button type="submit">Dismiss</button>
</form>
<form method="post" action="/items/{item['id']}/approve">
  <label>Approve, note optional <input name="note"></label>
  <button type="submit">Approve</button>
</form>
"""
    field = item.get("correct_field")
    if field:
        options = item.get("correct_options") or []
        if field in ("unit", "rate_year") and options:
            choices = "".join(
                f'<option value="{escape(str(value), quote=True)}">'
                f"{escape(str(value))}</option>"
                for value in options
            )
            value_input = f'<select name="value" required>{choices}</select>'
        else:
            value_input = '<input name="value" required>'
        forms += f"""
<form method="post" action="/items/{item['id']}/correct">
  <p>Correct the {escape(field.replace('_', ' '))} and send it through the verifier.</p>
  <label>{escape(field)} {value_input}</label>
  <button type="submit">Correct</button>
</form>
"""
    return forms


def _refuse_data(status, message):
    return JSONResponse(status_code=status, content={"message": message})


@app.get("/login")
def login_form():
    return _login_page()


@app.post("/login")
def login(username: str = Form(""), password: str = Form("")):
    conn = _connect()
    try:
        try:
            account = authenticate(conn, username.strip(), password)
        except StaffAuthError as error:
            return HTMLResponse(_login_page(str(error)).body, status_code=401)
        token = start_session(conn, account["id"])
    finally:
        conn.close()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", path="/")
    return response


@app.post("/logout")
def logout(request: Request):
    conn = _connect()
    try:
        end_session(conn, _token(request))
    finally:
        conn.close()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


@app.get("/")
def queue_page(request: Request):
    conn = _connect()
    try:
        account = _account(conn, request)
        if account is None:
            return RedirectResponse("/login", status_code=303)
        items = open_queue(conn, account["scopes"])
    finally:
        conn.close()
    return _queue_page(account, items)


@app.get("/items/{item_id}")
def detail_page(item_id: int, request: Request):
    conn = _connect()
    try:
        account = _account(conn, request)
        if account is None:
            return RedirectResponse("/login", status_code=303)
        try:
            item = _item_view(conn, account, item_id)
        except StaffQueueError as error:
            return HTMLResponse(
                _page("Not available", f"<h1>Not available</h1><p>{escape(str(error))}</p>"),
                status_code=403,
            )
    finally:
        conn.close()
    return _detail_page(account, item)


def _item_view(conn, account, item_id, banner=""):
    item = item_detail(conn, item_id, account["scopes"])
    raw = conn.execute("SELECT * FROM parse_review_items WHERE id = ?", (item_id,)).fetchone()
    names = [column[0] for column in conn.execute(
        "SELECT * FROM parse_review_items WHERE id = ?", (item_id,)).description]
    stored = dict(zip(names, raw))
    item["correct_field"] = None
    item["correct_options"] = []
    if stored.get("status") == "open":
        from src.staff_actions import carried_proposal
        from src import ai_extract

        proposal = carried_proposal(conn, stored)
        if proposal:
            item["correct_field"] = proposal["field"]
            if proposal["field"] == "unit":
                item["correct_options"] = sorted(ai_extract.vocabulary(conn)["unit"])
                if not item["correct_options"]:
                    item["correct_field"] = None
            elif proposal["field"] == "rate_year":
                item["correct_options"] = sorted({
                    str(year.get("label")).strip()
                    for year in proposal["payload"].get("rate_years") or []
                    if year.get("label")
                })
                if not item["correct_options"]:
                    item["correct_field"] = None
    item["decisions"] = decisions_for(conn, account["scopes"], item_id)
    if banner:
        item["_banner"] = f'<p class="warn" id="action-result">{escape(banner)}</p>'
    return item


@app.post("/items/{item_id}/dismiss")
def post_dismiss(item_id: int, request: Request, note: str = Form("")):
    conn = _connect()
    try:
        account = _account(conn, request)
        if account is None:
            return RedirectResponse("/login", status_code=303)
        try:
            dismiss_item(conn, account, item_id, note)
            banner = "Dismissed."
        except StaffActionError as error:
            banner = str(error)
            if str(error) == "This item is outside your scope.":
                return HTMLResponse(
                    _page("Not available", f"<h1>Not available</h1><p>{escape(str(error))}</p>"),
                    status_code=403,
                )
        item = _item_view(conn, account, item_id, banner)
    finally:
        conn.close()
    return _detail_page(account, item)


@app.post("/items/{item_id}/approve")
def post_approve(item_id: int, request: Request, note: str = Form("")):
    conn = _connect()
    try:
        account = _account(conn, request)
        if account is None:
            return RedirectResponse("/login", status_code=303)
        try:
            approve_item(conn, account, item_id, note)
            banner = "Approved. No tariff data was changed."
        except StaffActionError as error:
            banner = str(error)
            if str(error) == "This item is outside your scope.":
                return HTMLResponse(
                    _page("Not available", f"<h1>Not available</h1><p>{escape(str(error))}</p>"),
                    status_code=403,
                )
        item = _item_view(conn, account, item_id, banner)
    finally:
        conn.close()
    return _detail_page(account, item)


@app.post("/items/{item_id}/correct")
def post_correct(item_id: int, request: Request, value: str = Form("")):
    conn = _connect()
    try:
        account = _account(conn, request)
        if account is None:
            return RedirectResponse("/login", status_code=303)
        try:
            result = correct_item(conn, account, item_id, value)
            if result.get("signal"):
                banner = f"The verifier rejected this correction: {result['signal']}. {result.get('detail') or ''}"
            else:
                banner = f"Corrected. Stored {result.get('stored')} component on version {result.get('version_id')}."
        except StaffActionError as error:
            banner = str(error)
            if str(error) == "This item is outside your scope.":
                return HTMLResponse(
                    _page("Not available", f"<h1>Not available</h1><p>{escape(str(error))}</p>"),
                    status_code=403,
                )
        item = _item_view(conn, account, item_id, banner)
    finally:
        conn.close()
    return _detail_page(account, item)


@app.get("/decisions")
def decisions_page(request: Request):
    conn = _connect()
    try:
        account = _account(conn, request)
        if account is None:
            return RedirectResponse("/login", status_code=303)
        records = decisions_for(conn, account["scopes"])
    finally:
        conn.close()
    rows = []
    for record in records:
        rows.append(
            "<tr>"
            f"<td>{record['id']}</td>"
            f"<td><a href=\"/items/{record['item_id']}\">{record['item_id']}</a></td>"
            f"<td>{escape(record['username'])}</td>"
            f"<td>{escape(record['action'])}</td>"
            f"<td>{escape(record['outcome'])}</td>"
            f"<td>{escape(record['created_at'])}</td>"
            f"<td>{escape(record.get('note') or '')}</td>"
            "</tr>"
        )
    body = "\n".join(rows) or "<tr><td colspan=\"7\">No decisions.</td></tr>"
    return HTMLResponse(_page("Decisions", f"""
<div class="bar">
  <p class="who">{escape(account['username'])}</p>
  <form method="post" action="/logout"><button type="submit">Log out</button></form>
</div>
<p><a href="/">Back to the queue</a></p>
<h1>Decisions</h1>
<table>
  <thead><tr><th>#</th><th>Item</th><th>Reviewer</th><th>Action</th><th>Outcome</th><th>When</th><th>Note</th></tr></thead>
  <tbody>{body}</tbody>
</table>
"""))


@app.post("/api/items/{item_id}/dismiss")
def api_dismiss(item_id: int, request: Request, note: str = Form("")):
    conn = _connect()
    try:
        account = _account(conn, request)
        if account is None:
            return _refuse_data(401, SIGN_IN_REQUIRED)
        try:
            item = dismiss_item(conn, account, item_id, note)
        except StaffActionError as error:
            return _action_error(error)
    finally:
        conn.close()
    return {"item": item}


@app.post("/api/items/{item_id}/approve")
def api_approve(item_id: int, request: Request, note: str = Form("")):
    conn = _connect()
    try:
        account = _account(conn, request)
        if account is None:
            return _refuse_data(401, SIGN_IN_REQUIRED)
        try:
            item = approve_item(conn, account, item_id, note)
        except StaffActionError as error:
            return _action_error(error)
    finally:
        conn.close()
    return {"item": item}


@app.post("/api/items/{item_id}/correct")
def api_correct(item_id: int, request: Request, value: str = Form("")):
    conn = _connect()
    try:
        account = _account(conn, request)
        if account is None:
            return _refuse_data(401, SIGN_IN_REQUIRED)
        try:
            result = correct_item(conn, account, item_id, value)
        except StaffActionError as error:
            return _action_error(error)
    finally:
        conn.close()
    return result


def _action_error(error):
    text = str(error)
    if text == SIGN_IN_REQUIRED:
        status = 401
    elif text.startswith("No queue item"):
        status = 404
    elif text == "This item is outside your scope.":
        status = 403
    else:
        status = 422
    return _refuse_data(status, text)


@app.get("/api/queue")
def queue_data(request: Request):
    conn = _connect()
    try:
        account = _account(conn, request)
        if account is None:
            return _refuse_data(401, SIGN_IN_REQUIRED)
        items = open_queue(conn, account["scopes"])
    finally:
        conn.close()
    return {"open_count": len(items), "items": items}


@app.get("/api/items/{item_id}")
def item_data(item_id: int, request: Request):
    conn = _connect()
    try:
        account = _account(conn, request)
        if account is None:
            return _refuse_data(401, SIGN_IN_REQUIRED)
        try:
            item = item_detail(conn, item_id, account["scopes"])
        except StaffQueueError as error:
            status = 404 if str(error).startswith("No queue item") else 403
            return _refuse_data(status, str(error))
    finally:
        conn.close()
    return item


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STAFF_PORT") or "8877"))


if __name__ == "__main__":
    main()
