"""
Google Sheets two-way sync -- link management (connect/status/unlink a
client's Google Sheet), push endpoint, and pull webhook.
Routes: /api/clients/<id>/google-sheet-link, /api/sheets/push/<task_id>, /api/sheets/webhook/<token>
"""
import logging
import os
import re
import secrets

import google_sheets_store as gs
import notion_store
from extensions import limiter
from flask import Blueprint, jsonify, request
from utils import today_ist, now_ist, _is_admin

# Real Google Sheets spreadsheet ids are alphanumeric plus -/_, and always
# well over 20 characters. Rejecting anything else up front means a bad
# paste can't reach the Sheets API as a malformed/injected path segment.
_SPREADSHEET_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{20,100}$")

logger = logging.getLogger(__name__)
sheets_sync_bp = Blueprint("sheets_sync", __name__)


def _su_conn():
    from db import get_connection
    return get_connection()


def _verified_user_id() -> str:
    """Real session-verified user_id, via the HttpOnly session_token cookie
    set at login -- NOT a client-supplied user_id field/param. This app's
    `_is_admin()` is `bool(user_id)` everywhere (a deliberate product
    decision, see CLAUDE.md gotcha #60), but most other routes in this
    codebase still take that user_id straight from an unauthenticated
    request body/query string, meaning literally any non-empty string
    passes -- combined with this app's CORS(origins="*"), that's forgeable
    by any external site. Since this feature writes to a client's live
    Google Sheet and exposes a webhook secret, it uses the real
    cookie+session-table verification this codebase already has (see
    routes/auth.py::_verify_session, the pattern app.py::create_client
    itself uses) instead of repeating that weaker pattern."""
    from routes.auth import _verify_session
    token = request.cookies.get("session_token", "")
    return _verify_session(token) or ""


def _extract_spreadsheet_id(url_or_id: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url_or_id)
    return m.group(1) if m else url_or_id.strip()


def _base_url() -> str:
    """Prefer an explicitly-configured PUBLIC_BASE_URL over request.host_url.
    Flask reflects the incoming Host header into request.host_url; on most
    reverse-proxy setups that's the real edge-supplied host, but trusting it
    for a value embedded in generated setup code (the Apps Script snippet's
    webhook URL) is one weaker link than necessary -- a forged Host header
    that reached this app would make the UI hand a user a snippet that POSTs
    their client's Sheet data to an attacker's domain instead of Lumina's.
    PUBLIC_BASE_URL is optional; without it this just falls back to the
    previous behavior, so local dev needs no extra config."""
    configured = os.getenv("PUBLIC_BASE_URL", "").strip()
    return configured.rstrip("/") if configured else request.host_url.rstrip("/")


def _apps_script_snippet(webhook_url: str) -> str:
    return (
        "function onSheetChange(e) {\n"
        "  // Always the first tab, not getActiveSheet() -- Lumina only ever\n"
        "  // reads/writes this spreadsheet's first tab, so if a second tab is\n"
        "  // ever added and edited, this still targets the one that actually\n"
        "  // syncs instead of silently sending the wrong tab's data.\n"
        "  var sheet = SpreadsheetApp.getActive().getSheets()[0];\n"
        "  var data = sheet.getDataRange().getValues();\n"
        "  var rows = data.slice(1); // drop header row\n"
        "  UrlFetchApp.fetch(\"" + webhook_url + "\", {\n"
        "    method: \"post\",\n"
        "    contentType: \"application/json\",\n"
        "    payload: JSON.stringify({ rows: rows }),\n"
        "    muteHttpExceptions: true\n"
        "  });\n"
        "}\n\n"
        "function installTrigger() {\n"
        "  ScriptApp.newTrigger(\"onSheetChange\")\n"
        "    .forSpreadsheet(SpreadsheetApp.getActive())\n"
        "    .onChange()\n"
        "    .create();\n"
        "}\n"
        "// Run installTrigger() once manually (Run > installTrigger) to activate sync."
    )


def _link_row_to_dict(row) -> dict:
    (client_id, spreadsheet_id, link_token, is_notion, client_name, linked_at, linked_by,
     last_push_at, last_push_ok, last_pull_at, last_pull_summary) = row
    webhook_url = f"{_base_url()}/api/sheets/webhook/{link_token}"
    return {
        "linked": True, "client_id": client_id, "spreadsheet_id": spreadsheet_id,
        "is_notion": bool(is_notion), "client_name": client_name,
        "linked_at": linked_at, "linked_by": linked_by,
        "service_account_email": gs.service_account_email(),
        "apps_script": _apps_script_snippet(webhook_url),
        "last_push_at": last_push_at, "last_push_ok": bool(last_push_ok) if last_push_ok is not None else None,
        "last_pull_at": last_pull_at, "last_pull_summary": last_pull_summary,
    }


@sheets_sync_bp.route("/api/sheets/links", methods=["GET"])
def list_linked_client_ids():
    """Just the set of linked client_ids (no tokens) -- powers a lightweight
    'is this client linked' indicator on the connect button without needing
    to open every client's modal to check."""
    if not _is_admin(_verified_user_id()):
        return jsonify({"error": "Unauthorized"}), 403
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("SELECT client_id FROM google_sheet_links")
    ids = [r[0] for r in cur.fetchall()]
    conn.close()
    return jsonify({"linked_client_ids": ids})


@sheets_sync_bp.route("/api/clients/<string:client_id>/google-sheet-link", methods=["POST"])
@limiter.limit("10 per minute")
def create_google_sheet_link(client_id: str):
    cfg_error = gs.config_error()
    if cfg_error:
        return jsonify({"error": cfg_error}), 400
    user_id = _verified_user_id()
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    body = request.get_json(silent=True) or {}
    raw_url = str(body.get("spreadsheet_url", "")).strip()
    is_notion = bool(body.get("is_notion"))
    client_name = str(body.get("client_name", "")).strip()
    if not raw_url:
        return jsonify({"error": "spreadsheet_url required"}), 400
    spreadsheet_id = _extract_spreadsheet_id(raw_url)
    if not _SPREADSHEET_ID_RE.match(spreadsheet_id):
        return jsonify({"error": "That doesn't look like a valid Google Sheets URL/ID"}), 400
    # Sanity check, not a trust decision: is_notion is client-supplied (the
    # frontend derives it from notionMode && !!client.notion_id) but this
    # endpoint is already admin-gated, so a mismatch here is a bug/misclick
    # to catch early, not an attack to defend against.
    if is_notion and not notion_store.is_configured():
        return jsonify({"error": "is_notion=true but Notion isn't configured on this server"}), 400

    try:
        gs.read_all_rows(spreadsheet_id)
    except Exception:
        return jsonify({
            "error": f"Could not read that sheet -- make sure it's shared (Editor access) with {gs.service_account_email()}"
        }), 400

    conn = _su_conn()
    cur = conn.cursor()

    # Guard against the same physical spreadsheet being linked to two
    # DIFFERENT clients. Nothing about read_all_rows/reconcile is scoped
    # per-client beyond "whatever client_id this spreadsheet_id's link row
    # says" -- if a second client claimed the same spreadsheet, both
    # clients' reconciles would read the exact same rows and each treat the
    # other's tasks as unrecognized ids to create/delete, a mutual data-
    # corruption loop rather than a one-off mistake. This can only happen
    # via a copy-pasted URL mistake (each client's own Connect flow always
    # points at a sheet the employee just created for that client), so
    # rejecting it outright is safe -- there's no legitimate reason for two
    # clients to intentionally share one spreadsheet_id.
    cur.execute("SELECT client_id, client_name FROM google_sheet_links WHERE spreadsheet_id=?", (spreadsheet_id,))
    other = cur.fetchone()
    if other and str(other[0]) != str(client_id):
        return jsonify({
            "error": f"That Google Sheet is already linked to a different client"
                     f"{' (' + other[1] + ')' if other[1] else ''}. "
                     f"Unlink it there first, or use a different Sheet for this client."
        }), 409

    # Reuse the existing link_token only when relinking the SAME spreadsheet
    # (e.g. reconnecting after a credential fix) -- that's the one case
    # where whatever trigger is already installed in that physical sheet is
    # still the right one, and reusing the token avoids invalidating it.
    # Relinking to a genuinely DIFFERENT spreadsheet must mint a fresh token:
    # Apps Script triggers are bound to the spreadsheet they were installed
    # in, so the user has to open the NEW sheet's script editor and paste
    # the snippet again regardless -- there's no reinstall being saved by
    # keeping the old token. Reusing it here would instead leave the OLD
    # (still-installed, now orphaned) sheet's trigger POSTing to a webhook
    # URL that this client's link row now resolves to the NEW spreadsheet_id
    # -- every edit in the old sheet would still update Lumina, and any
    # write-back (e.g. a new task's id) would land in the NEW sheet at
    # whatever row number the OLD sheet's snapshot happened to report,
    # silently corrupting an unrelated row there. Minting a fresh token
    # instead makes the old trigger's requests 404 harmlessly.
    cur.execute("SELECT link_token, spreadsheet_id FROM google_sheet_links WHERE client_id=?", (client_id,))
    existing = cur.fetchone()
    same_spreadsheet = existing and str(existing[1]) == str(spreadsheet_id)
    link_token = existing[0] if same_spreadsheet else secrets.token_urlsafe(24)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO google_sheet_links "
            "(client_id, spreadsheet_id, link_token, is_notion, client_name, linked_at, linked_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (client_id, spreadsheet_id, link_token, 1 if is_notion else 0, client_name,
             f"{today_ist()} {now_ist()}", user_id),
        )
    conn.close()

    # One-time backfill: push every task Lumina already has for this client
    # into the newly-linked Sheet. Without this, connecting a client with
    # pre-existing tasks leaves the Sheet empty until each row happens to be
    # individually edited in Lumina (which is what triggers a push) -- not
    # discoverable, and impractical for a client with a dozen+ existing
    # tasks. push_task_to_sheet is safe to call repeatedly (it looks up the
    # row by task_id and overwrites in place, or appends if missing), so
    # this is also safe to re-run on a re-link.
    backfilled = 0
    try:
        existing_tasks = gs._current_tasks_by_id(client_id, is_notion)
        link_for_push = {"spreadsheet_id": spreadsheet_id, "client_id": client_id}
        for tid, task in existing_tasks.items():
            fields = gs._task_to_fields(task)
            if gs.push_task_to_sheet(link_for_push, tid, fields):
                backfilled += 1
    except Exception:
        logger.exception(f"Sheets connect: backfill failed for client {client_id}")

    webhook_url = f"{_base_url()}/api/sheets/webhook/{link_token}"
    return jsonify({
        "success": True, "spreadsheet_id": spreadsheet_id,
        "service_account_email": gs.service_account_email(),
        "apps_script": _apps_script_snippet(webhook_url),
        "backfilled": backfilled,
    })


@sheets_sync_bp.route("/api/clients/<string:client_id>/google-sheet-link", methods=["GET"])
def get_google_sheet_link(client_id: str):
    # Auth required: the response includes the live link_token embedded in
    # the Apps Script snippet -- that token is the pull webhook's sole
    # authenticator (see sheets_pull_webhook below), so leaving this
    # endpoint open would let anyone who knows a client_id (not treated as
    # secret elsewhere in this app) read it back out and forge webhook calls.
    if not _is_admin(_verified_user_id()):
        return jsonify({"error": "Unauthorized"}), 403
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT client_id, spreadsheet_id, link_token, is_notion, client_name, linked_at, linked_by, "
        "last_push_at, last_push_ok, last_pull_at, last_pull_summary "
        "FROM google_sheet_links WHERE client_id=?", (client_id,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"linked": False})
    return jsonify(_link_row_to_dict(row))


@sheets_sync_bp.route("/api/clients/<string:client_id>/google-sheet-link", methods=["DELETE"])
def delete_google_sheet_link(client_id: str):
    if not _is_admin(_verified_user_id()):
        return jsonify({"error": "Unauthorized"}), 403
    conn = _su_conn()
    with conn:
        conn.execute("DELETE FROM google_sheet_links WHERE client_id=?", (client_id,))
    conn.close()
    return jsonify({"success": True})


def get_link_for_client(client_id: str):
    """Internal helper for the push route below -- returns the plain dict
    shape google_sheets_store.push_task_to_sheet/reconcile_sheet_rows expect,
    or None if this client has no linked Sheet."""
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT client_id, spreadsheet_id, is_notion, client_name, linked_by "
        "FROM google_sheet_links WHERE client_id=?", (client_id,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"client_id": row[0], "spreadsheet_id": row[1], "is_notion": bool(row[2]),
            "client_name": row[3], "linked_by": row[4]}


def get_link_by_token(link_token: str):
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT client_id, spreadsheet_id, is_notion, client_name, linked_by "
        "FROM google_sheet_links WHERE link_token=?", (link_token,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"client_id": row[0], "spreadsheet_id": row[1], "is_notion": bool(row[2]),
            "client_name": row[3], "linked_by": row[4]}


# Hard cap on rows accepted per webhook call -- generous for this app's real
# scale (dozens to low hundreds of rows per client), but stops a garbled or
# runaway Apps Script payload from hammering the Notion API with hundreds of
# create/update calls in one request.
MAX_WEBHOOK_ROWS = 1000


def _record_push_result(client_id: str, ok: bool):
    conn = _su_conn()
    with conn:
        conn.execute(
            "UPDATE google_sheet_links SET last_push_at=?, last_push_ok=? WHERE client_id=?",
            (f"{today_ist()} {now_ist()}", 1 if ok else 0, client_id),
        )
    conn.close()


def _record_pull_result(link_token: str, summary: dict):
    parts = [f"{k}={v}" for k, v in summary.items()]
    conn = _su_conn()
    with conn:
        conn.execute(
            "UPDATE google_sheet_links SET last_pull_at=?, last_pull_summary=? WHERE link_token=?",
            (f"{today_ist()} {now_ist()}", ", ".join(parts), link_token),
        )
    conn.close()


@sheets_sync_bp.route("/api/sheets/push/<string:task_id>", methods=["POST"])
@limiter.limit("120 per minute")
def push_sheet_task(task_id: str):
    """Called fire-and-forget from applySheetFields() in projects.html after
    every normal Sheets save. No-op (200) for clients with no linked Sheet --
    this must never surface as an error to a user who never opted into sync."""
    if not _is_admin(_verified_user_id()):
        return jsonify({"error": "Unauthorized"}), 403
    body = request.get_json(silent=True) or {}
    client_id = str(body.get("client_id", "")).strip()
    fields = body.get("fields") or {}
    if not client_id or not isinstance(fields, dict):
        return jsonify({"error": "client_id and fields required"}), 400
    link = get_link_for_client(client_id)
    if not link:
        return jsonify({"success": True, "linked": False})
    ok = gs.push_task_to_sheet(link, task_id, fields)
    _record_push_result(client_id, ok)
    return jsonify({"success": True, "linked": True, "pushed": ok})


@sheets_sync_bp.route("/api/sheets/delete/<string:task_id>", methods=["POST"])
@limiter.limit("120 per minute")
def delete_sheet_task(task_id: str):
    """Called fire-and-forget from projects.html after a Lumina-side row
    delete actually commits (past the undo window). No-op (200) for clients
    with no linked Sheet. Only the Sheet-side "row deleted -> Lumina task
    deleted" direction existed before this -- a Lumina-side delete never
    touched the linked Sheet at all, leaving a dead row referencing a task
    that no longer existed (see CLAUDE.md gotcha #87, 2026-08-14)."""
    if not _is_admin(_verified_user_id()):
        return jsonify({"error": "Unauthorized"}), 403
    body = request.get_json(silent=True) or {}
    client_id = str(body.get("client_id", "")).strip()
    if not client_id:
        return jsonify({"error": "client_id required"}), 400
    link = get_link_for_client(client_id)
    if not link:
        return jsonify({"success": True, "linked": False})
    deleted = gs.delete_task_from_sheet(link, task_id)
    return jsonify({"success": True, "linked": True, "deleted": deleted})


@sheets_sync_bp.route("/api/sheets/webhook/<string:link_token>", methods=["POST"])
@limiter.limit("60 per minute")
def sheets_pull_webhook(link_token: str):
    """Called by the Apps Script onChange trigger installed in a linked
    Sheet (see _apps_script_snippet). link_token is the sole authenticator --
    unguessable, scoped to exactly one client, never shown outside the
    one-time setup dialog."""
    link = get_link_by_token(link_token)
    if not link:
        return jsonify({"error": "Unknown link"}), 404
    body = request.get_json(silent=True) or {}
    rows = body.get("rows")
    if not isinstance(rows, list):
        return jsonify({"error": "rows required"}), 400
    if len(rows) > MAX_WEBHOOK_ROWS:
        return jsonify({"error": f"Sheet has more than {MAX_WEBHOOK_ROWS} rows -- contact the developer to raise this limit"}), 400
    try:
        summary = gs.reconcile_sheet_rows(link, rows)
    except Exception:
        logger.exception(f"Sheets webhook: reconcile failed for link_token={link_token}")
        return jsonify({"error": "Reconcile failed, see server logs"}), 500
    _record_pull_result(link_token, summary)
    return jsonify({"success": True, **summary})
