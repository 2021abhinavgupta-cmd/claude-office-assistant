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


def _apps_script_snippet(webhook_url: str, multi_tab: bool = False) -> str:
    if not multi_tab:
        return (
            "function onSheetChange(e) {\n"
            "  // Always the first tab, not getActiveSheet() -- Lumina only ever\n"
            "  // reads/writes this spreadsheet's first tab, so if a second tab is\n"
            "  // ever added and edited, this still targets the one that actually\n"
            "  // syncs instead of silently sending the wrong tab's data.\n"
            "  var sheet = SpreadsheetApp.getActive().getSheets()[0];\n"
            "  // getDisplayValues (not getValues): a Date-formatted cell would\n"
            "  // otherwise serialize with a UTC offset baked in, which can shift\n"
            "  // the visible date by a day. Reading exactly what's shown avoids\n"
            "  // that regardless of how the column happens to be formatted.\n"
            "  var data = sheet.getDataRange().getDisplayValues();\n"
            "  var rows = data.slice(1); // drop header row\n"
            "  var resp = UrlFetchApp.fetch(\"" + webhook_url + "\", {\n"
            "    method: \"post\",\n"
            "    contentType: \"application/json\",\n"
            "    payload: JSON.stringify({ rows: rows }),\n"
            "    muteHttpExceptions: true\n"
            "  });\n"
            "  // Throwing on a non-2xx response (instead of silently ignoring it,\n"
            "  // which muteHttpExceptions alone would do) restores Apps Script's own\n"
            "  // automatic trigger-failure email -- without this, a sync error had\n"
            "  // zero visibility anywhere: the trigger 'succeeds' from Apps Script's\n"
            "  // point of view even though nothing actually synced.\n"
            "  if (resp.getResponseCode() >= 300) {\n"
            "    throw new Error(\"Lumina sync failed: HTTP \" + resp.getResponseCode() + \" - \" + resp.getContentText());\n"
            "  }\n"
            "}\n\n"
            "function installTrigger() {\n"
            "  ScriptApp.newTrigger(\"onSheetChange\")\n"
            "    .forSpreadsheet(SpreadsheetApp.getActive())\n"
            "    .onChange()\n"
            "    .create();\n"
            "}\n"
            "// Run installTrigger() once manually (Run > installTrigger) to activate sync."
        )
    return (
        "function onSheetChange(e) {\n"
        "  // Multi-tab mode: sync every tab named like a month (\"August 2026\")\n"
        "  // plus a tab literally named \"Unscheduled\". Any other tab is ignored.\n"
        "  var ss = SpreadsheetApp.getActive();\n"
        "  var monthRe = /^(January|February|March|April|May|June|July|August|September|October|November|December) \\d{4}$/;\n"
        "  var tabs = {};\n"
        "  ss.getSheets().forEach(function(sheet) {\n"
        "    var name = sheet.getName();\n"
        "    if (name === \"Unscheduled\" || monthRe.test(name)) {\n"
        "      // getDisplayValues (not getValues): a Date-formatted cell would\n"
        "      // otherwise serialize with a UTC offset baked in, which can shift\n"
        "      // the visible date by a day -- worse here than in single-tab mode,\n"
        "      // since a shifted date can misroute a task to the wrong month tab.\n"
        "      var data = sheet.getDataRange().getDisplayValues();\n"
        "      tabs[name] = data.slice(1); // drop header row\n"
        "    }\n"
        "  });\n"
        "  var resp = UrlFetchApp.fetch(\"" + webhook_url + "\", {\n"
        "    method: \"post\",\n"
        "    contentType: \"application/json\",\n"
        "    payload: JSON.stringify({ tabs: tabs }),\n"
        "    muteHttpExceptions: true\n"
        "  });\n"
        "  // See the single-tab snippet's comment -- throwing on non-2xx restores\n"
        "  // Apps Script's own automatic trigger-failure email instead of the sync\n"
        "  // error going completely unnoticed.\n"
        "  if (resp.getResponseCode() >= 300) {\n"
        "    throw new Error(\"Lumina sync failed: HTTP \" + resp.getResponseCode() + \" - \" + resp.getContentText());\n"
        "  }\n"
        "}\n\n"
        "function installTrigger() {\n"
        "  ScriptApp.newTrigger(\"onSheetChange\")\n"
        "    .forSpreadsheet(SpreadsheetApp.getActive())\n"
        "    .onChange()\n"
        "    .create();\n"
        "}\n"
        "// Run installTrigger() once manually (Run > installTrigger) to activate sync.\n"
        "// Name each month tab exactly like \"August 2026\". Tasks with no due date\n"
        "// go in a tab named exactly \"Unscheduled\". Any other tab name is ignored."
    )


def _link_row_to_dict(row) -> dict:
    (client_id, spreadsheet_id, link_token, is_notion, client_name, linked_at, linked_by,
     last_push_at, last_push_ok, last_pull_at, last_pull_summary, multi_tab) = row
    webhook_url = f"{_base_url()}/api/sheets/webhook/{link_token}"
    return {
        "linked": True, "client_id": client_id, "spreadsheet_id": spreadsheet_id,
        "is_notion": bool(is_notion), "client_name": client_name,
        "linked_at": linked_at, "linked_by": linked_by,
        "multi_tab": bool(multi_tab),
        "service_account_email": gs.service_account_email(),
        "apps_script": _apps_script_snippet(webhook_url, bool(multi_tab)),
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
        gs.list_tabs(spreadsheet_id)
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
    # multi_tab: existing clients keep whatever mode they already had on
    # any relink (including to a different spreadsheet -- it's a per-client
    # sync-mode choice, not tied to one physical sheet). A brand-new client
    # connect always starts multi_tab=1, since monthly-tab sync is now the
    # primary supported mode -- see CLAUDE.md gotcha #87 and the design spec
    # at docs/superpowers/specs/2026-08-15-google-sheets-multi-tab-sync-design.md.
    cur.execute("SELECT link_token, spreadsheet_id, multi_tab FROM google_sheet_links WHERE client_id=?", (client_id,))
    existing = cur.fetchone()
    same_spreadsheet = existing and str(existing[1]) == str(spreadsheet_id)
    link_token = existing[0] if same_spreadsheet else secrets.token_urlsafe(24)
    multi_tab = existing[2] if existing is not None else 1
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO google_sheet_links "
            "(client_id, spreadsheet_id, link_token, is_notion, client_name, linked_at, linked_by, multi_tab) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (client_id, spreadsheet_id, link_token, 1 if is_notion else 0, client_name,
             f"{today_ist()} {now_ist()}", user_id, multi_tab),
        )
    conn.close()

    # Auto-build a brand-new, completely empty spreadsheet into an
    # immediately usable one instead of leaving it inert -- renames the
    # default tab to the current month (multi-tab clients) or just formats
    # the existing tab (single-tab clients), writes the header row, and
    # applies the Type/Assigned To/Status dropdowns. No-ops harmlessly if
    # the sheet already has any tabs/data (never touches a sheet someone
    # already started setting up by hand). Must not fail the connect itself.
    init_result = {"initialized": False}
    try:
        init_result = gs.initialize_blank_spreadsheet(spreadsheet_id, bool(multi_tab))
    except Exception:
        logger.exception(f"Sheets connect: blank-spreadsheet init failed for client {client_id}")

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
        push_fn = gs.push_task_to_sheet_multi_tab if multi_tab else gs.push_task_to_sheet
        for tid, task in existing_tasks.items():
            fields = gs._task_to_fields(task)
            if push_fn(link_for_push, tid, fields):
                backfilled += 1
    except Exception:
        logger.exception(f"Sheets connect: backfill failed for client {client_id}")

    # Also format Type/Assigned To/Status dropdowns on whatever tab(s) exist
    # at connect time (the sheet's default first tab, or any month tabs the
    # employee pre-created) -- so a newly-connected client's Sheet is never
    # left free-text and vulnerable to the typo class of bug _normalize_type
    # exists to paper over. Best-effort: must not fail the connect itself.
    try:
        gs.apply_dropdown_validation_to_all_tabs(spreadsheet_id)
    except Exception:
        logger.exception(f"Sheets connect: dropdown formatting failed for client {client_id}")

    webhook_url = f"{_base_url()}/api/sheets/webhook/{link_token}"
    return jsonify({
        "success": True, "spreadsheet_id": spreadsheet_id,
        "service_account_email": gs.service_account_email(),
        "apps_script": _apps_script_snippet(webhook_url, bool(multi_tab)),
        "multi_tab": bool(multi_tab),
        "backfilled": backfilled,
        "initialized_blank": init_result.get("initialized", False),
        "initialized_tabs": init_result.get("tabs", []),
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
        "last_push_at, last_push_ok, last_pull_at, last_pull_summary, multi_tab "
        "FROM google_sheet_links WHERE client_id=?", (client_id,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        # Surfaces the service account email even before a Sheet is
        # connected -- the "how to connect" guide in the modal needs it up
        # front (share the Sheet with this address BEFORE pasting the URL),
        # not only after a connect attempt fails with "make sure it's shared".
        return jsonify({"linked": False, "service_account_email": gs.service_account_email()})
    return jsonify(_link_row_to_dict(row))


@sheets_sync_bp.route("/api/clients/<string:client_id>/google-sheet-link/format", methods=["POST"])
def format_google_sheet_dropdowns(client_id: str):
    """Retroactively applies the Type/Assigned To/Status dropdowns to every
    tab of an already-linked client's Sheet -- for clients linked before
    this feature existed, or a tab created by hand rather than through
    ensure_tab_exists()/the connect flow (both of which already apply this
    automatically)."""
    if not _is_admin(_verified_user_id()):
        return jsonify({"error": "Unauthorized"}), 403
    link = get_link_for_client(client_id)
    if not link:
        return jsonify({"error": "Client has no linked Google Sheet"}), 404
    try:
        formatted = gs.apply_dropdown_validation_to_all_tabs(link["spreadsheet_id"])
    except Exception:
        logger.exception(f"Manual dropdown formatting failed for client {client_id}")
        return jsonify({"error": "Failed to format sheet, see server logs"}), 500
    return jsonify({"success": True, "tabs_formatted": formatted})


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
    shape google_sheets_store.push_task_to_sheet(_multi_tab)/
    reconcile_sheet_rows(_tabs) expect, or None if this client has no linked
    Sheet."""
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT client_id, spreadsheet_id, is_notion, client_name, linked_by, multi_tab "
        "FROM google_sheet_links WHERE client_id=?", (client_id,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"client_id": row[0], "spreadsheet_id": row[1], "is_notion": bool(row[2]),
            "client_name": row[3], "linked_by": row[4], "multi_tab": bool(row[5])}


def get_link_by_token(link_token: str):
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT client_id, spreadsheet_id, is_notion, client_name, linked_by, multi_tab "
        "FROM google_sheet_links WHERE link_token=?", (link_token,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"client_id": row[0], "spreadsheet_id": row[1], "is_notion": bool(row[2]),
            "client_name": row[3], "linked_by": row[4], "multi_tab": bool(row[5])}


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
    push_fn = gs.push_task_to_sheet_multi_tab if link.get("multi_tab") else gs.push_task_to_sheet
    ok = push_fn(link, task_id, fields)
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
    delete_fn = gs.delete_task_from_sheet_multi_tab if link.get("multi_tab") else gs.delete_task_from_sheet
    deleted = delete_fn(link, task_id)
    return jsonify({"success": True, "linked": True, "deleted": deleted})


@sheets_sync_bp.route("/api/sheets/webhook/<string:link_token>", methods=["POST"])
@limiter.limit("60 per minute")
def sheets_pull_webhook(link_token: str):
    """Called by the Apps Script onChange trigger installed in a linked
    Sheet (see _apps_script_snippet). link_token is the sole authenticator --
    unguessable, scoped to exactly one client, never shown outside the
    one-time setup dialog. Accepts either payload shape ({"tabs": {...}} for
    a multi_tab=1 link, {"rows": [...]} for a legacy single-tab link) so an
    already-installed script keeps working right up until it's reconnected."""
    link = get_link_by_token(link_token)
    if not link:
        return jsonify({"error": "Unknown link"}), 404
    body = request.get_json(silent=True) or {}

    if link.get("multi_tab") and isinstance(body.get("tabs"), dict):
        # Drop any tab whose value isn't itself a list before doing anything
        # else with it -- reconcile_sheet_tabs iterates each tab's rows, and
        # a non-list value (garbled payload) would otherwise reach the same
        # "silently fakes rows" hazard the isinstance(row, list) guard in
        # google_sheets_store.py closes for individual rows, just one level
        # up.
        tabs = {k: v for k, v in body["tabs"].items() if isinstance(v, list)}
        total_rows = sum(len(v) for v in tabs.values())
        if total_rows > MAX_WEBHOOK_ROWS:
            return jsonify({
                "error": f"Sheet has more than {MAX_WEBHOOK_ROWS} total rows across its tabs -- "
                         f"contact the developer to raise this limit"
            }), 400
        try:
            summary = gs.reconcile_sheet_tabs(link, tabs)
        except Exception:
            logger.exception(f"Sheets webhook (multi-tab): reconcile failed for link_token={link_token}")
            return jsonify({"error": "Reconcile failed, see server logs"}), 500
        _record_pull_result(link_token, summary)
        return jsonify({"success": True, **summary})

    rows = body.get("rows")
    if not isinstance(rows, list):
        return jsonify({"error": "rows (or tabs, for a multi-tab-linked client) required"}), 400
    if len(rows) > MAX_WEBHOOK_ROWS:
        return jsonify({"error": f"Sheet has more than {MAX_WEBHOOK_ROWS} rows -- contact the developer to raise this limit"}), 400
    try:
        summary = gs.reconcile_sheet_rows(link, rows)
    except Exception:
        logger.exception(f"Sheets webhook: reconcile failed for link_token={link_token}")
        return jsonify({"error": "Reconcile failed, see server logs"}), 500
    _record_pull_result(link_token, summary)
    return jsonify({"success": True, **summary})
