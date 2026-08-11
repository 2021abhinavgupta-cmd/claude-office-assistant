"""
Google Sheets two-way sync -- link management (connect/status/unlink a
client's Google Sheet), push endpoint, and pull webhook.
Routes: /api/clients/<id>/google-sheet-link, /api/sheets/push/<task_id>, /api/sheets/webhook/<token>
"""
import logging
import re
import secrets

import google_sheets_store as gs
from flask import Blueprint, jsonify, request
from utils import today_ist, now_ist, _is_admin

logger = logging.getLogger(__name__)
sheets_sync_bp = Blueprint("sheets_sync", __name__)


def _su_conn():
    from db import get_connection
    return get_connection()


def _extract_spreadsheet_id(url_or_id: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url_or_id)
    return m.group(1) if m else url_or_id.strip()


def _apps_script_snippet(webhook_url: str) -> str:
    return (
        "function onSheetChange(e) {\n"
        "  var sheet = SpreadsheetApp.getActiveSheet();\n"
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
    client_id, spreadsheet_id, link_token, is_notion, client_name, linked_at, linked_by = row
    webhook_url = f"{request.host_url.rstrip('/')}/api/sheets/webhook/{link_token}"
    return {
        "linked": True, "client_id": client_id, "spreadsheet_id": spreadsheet_id,
        "is_notion": bool(is_notion), "client_name": client_name,
        "linked_at": linked_at, "linked_by": linked_by,
        "service_account_email": gs.service_account_email(),
        "apps_script": _apps_script_snippet(webhook_url),
    }


@sheets_sync_bp.route("/api/clients/<string:client_id>/google-sheet-link", methods=["POST"])
def create_google_sheet_link(client_id: str):
    if not gs.is_configured():
        return jsonify({"error": "Google Sheets sync is not configured on this server"}), 400
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    raw_url = str(body.get("spreadsheet_url", "")).strip()
    is_notion = bool(body.get("is_notion"))
    client_name = str(body.get("client_name", "")).strip()
    if not raw_url:
        return jsonify({"error": "spreadsheet_url required"}), 400
    spreadsheet_id = _extract_spreadsheet_id(raw_url)

    try:
        gs.read_all_rows(spreadsheet_id)
    except Exception:
        return jsonify({
            "error": f"Could not read that sheet -- make sure it's shared (Editor access) with {gs.service_account_email()}"
        }), 400

    link_token = secrets.token_urlsafe(24)
    conn = _su_conn()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO google_sheet_links "
            "(client_id, spreadsheet_id, link_token, is_notion, client_name, linked_at, linked_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (client_id, spreadsheet_id, link_token, 1 if is_notion else 0, client_name,
             f"{today_ist()} {now_ist()}", user_id),
        )
    conn.close()

    webhook_url = f"{request.host_url.rstrip('/')}/api/sheets/webhook/{link_token}"
    return jsonify({
        "success": True, "spreadsheet_id": spreadsheet_id,
        "service_account_email": gs.service_account_email(),
        "apps_script": _apps_script_snippet(webhook_url),
    })


@sheets_sync_bp.route("/api/clients/<string:client_id>/google-sheet-link", methods=["GET"])
def get_google_sheet_link(client_id: str):
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT client_id, spreadsheet_id, link_token, is_notion, client_name, linked_at, linked_by "
        "FROM google_sheet_links WHERE client_id=?", (client_id,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"linked": False})
    return jsonify(_link_row_to_dict(row))


@sheets_sync_bp.route("/api/clients/<string:client_id>/google-sheet-link", methods=["DELETE"])
def delete_google_sheet_link(client_id: str):
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


@sheets_sync_bp.route("/api/sheets/push/<string:task_id>", methods=["POST"])
def push_sheet_task(task_id: str):
    """Called fire-and-forget from applySheetFields() in projects.html after
    every normal Sheets save. No-op (200) for clients with no linked Sheet --
    this must never surface as an error to a user who never opted into sync."""
    body = request.get_json(silent=True) or {}
    client_id = str(body.get("client_id", "")).strip()
    fields = body.get("fields") or {}
    if not client_id or not isinstance(fields, dict):
        return jsonify({"error": "client_id and fields required"}), 400
    link = get_link_for_client(client_id)
    if not link:
        return jsonify({"success": True, "linked": False})
    gs.push_task_to_sheet(link, task_id, fields)
    return jsonify({"success": True, "linked": True})


@sheets_sync_bp.route("/api/sheets/webhook/<string:link_token>", methods=["POST"])
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
    summary = gs.reconcile_sheet_rows(link, rows)
    return jsonify({"success": True, **summary})
