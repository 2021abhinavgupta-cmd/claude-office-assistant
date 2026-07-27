"""
Google Sheets two-way sync -- raw Sheets API v4 access via a service-account
session. Uses google-auth only (not the heavier google-api-python-client) so
this stays a thin REST wrapper, consistent with notion_store.py's raw-requests
convention for external APIs in this codebase.
"""
import json
import logging
import os
import re

import notion_store
from utils import today_ist, now_ist

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_session = None
_service_account_email_cache = None


def is_configured() -> bool:
    return bool(os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON"))


def config_error() -> str:
    """Empty string if the credential looks usable, otherwise a specific
    diagnostic message. Distinguishes "not configured at all" from
    "configured but broken" (invalid JSON, or valid JSON missing the fields
    a service-account key must have) -- without this, a paste mistake (a
    stray newline, missing outer quotes) surfaces downstream as "make sure
    the sheet is shared with ...", which sends whoever's debugging it
    checking sharing settings that were never the problem."""
    raw = os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        return "Google Sheets sync is not configured on this server"
    try:
        info = json.loads(raw)
    except Exception:
        return ("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON is not valid JSON -- check for a missing "
                "outer quote or an unescaped newline in the pasted private key")
    missing = [k for k in ("client_email", "private_key") if not info.get(k)]
    if missing:
        return f"GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON is missing required field(s): {', '.join(missing)}"
    return ""


def _get_session():
    """Lazily builds and caches an AuthorizedSession from the service-account
    JSON in GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON. google-auth refreshes the
    underlying token internally, so the cached session is safe to reuse for
    the life of the process."""
    global _session
    if _session is not None:
        return _session
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession

    info = json.loads(os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON", "{}"))
    creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
    _session = AuthorizedSession(creds)
    return _session


def service_account_email() -> str:
    """Shown to the employee during setup so they know which address to
    share the client's Google Sheet with."""
    global _service_account_email_cache
    if _service_account_email_cache is not None:
        return _service_account_email_cache
    raw = os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        return ""
    try:
        _service_account_email_cache = json.loads(raw).get("client_email", "")
    except Exception:
        _service_account_email_cache = ""
    return _service_account_email_cache


def read_all_rows(spreadsheet_id: str) -> list:
    """Every row (including the header) as a list of lists, using
    FORMATTED_VALUE (the API default) so plain-text-formatted date cells come
    back as the exact typed string rather than a date serial number. Short
    rows are NOT padded by the Sheets API -- callers must not assume every
    row has all 13 columns."""
    session = _get_session()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/A:M"
    r = session.get(url)
    r.raise_for_status()
    return r.json().get("values", [])


def write_row(spreadsheet_id: str, row_number: int, values: list):
    """Overwrites one full row, 1-indexed to match the Sheet's own row numbers."""
    session = _get_session()
    rng = f"A{row_number}:M{row_number}"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}"
    r = session.put(url, params={"valueInputOption": "RAW"}, json={"values": [values]})
    r.raise_for_status()


def append_row(spreadsheet_id: str, values: list) -> int:
    """Appends a new row after the sheet's last row with data. Returns the
    new row's 1-indexed row number, parsed out of the API's updatedRange."""
    session = _get_session()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/A:M:append"
    r = session.post(
        url,
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": [values]},
    )
    r.raise_for_status()
    updated_range = r.json().get("updates", {}).get("updatedRange", "")
    m = re.search(r"!A(\d+)", updated_range)
    return int(m.group(1)) if m else -1


def write_cell(spreadsheet_id: str, a1_cell: str, value):
    session = _get_session()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{a1_cell}"
    r = session.put(url, params={"valueInputOption": "RAW"}, json={"values": [[value]]})
    r.raise_for_status()


# ── Field mapping ────────────────────────────────────────────────────────
# Column order matches RH_FIELD_LABELS in projects.html (12 Sheets fields),
# with lumina_task_id prepended as column A -- A:M is 13 columns total.
SHEET_FIELDS = ["creation_date", "due_date", "title", "type", "content", "idea",
                "scripts", "caption", "link", "myNotes", "assigned_to", "status"]

SHEET_HEADER_ROW = ["lumina_task_id", "Creation Date", "Post Day", "Post Title", "Type",
                     "Content", "Idea", "Scripts/Copy", "Caption", "File (Drive Link)",
                     "Notes", "Assigned To", "Status"]


def _parse_sheet_notes(desc: str, existing_creation_date: str = "") -> dict:
    """Python port of parseSheetNotes() in projects.html. Must stay in sync
    with that function's regex boundaries -- this is the exact composite-Notes
    format applySheetFields() writes, so a value round-tripped through here
    has to parse identically on both sides."""
    desc = desc or ""
    creation_date = existing_creation_date or ""
    if not creation_date:
        m = re.search(r"Creation Date\s*:\s*(.*?)(?=\s*\|\s*(?:Content|Idea|Scripts|Caption|Link)\s*:|$)", desc, re.S | re.I)
        if m:
            creation_date = m.group(1).strip()

    def _field(label, stop_labels):
        stop = "|".join(stop_labels)
        m = re.search(rf"{label}:\s*(.*?)(?=\s*\|\s*(?:{stop})\s*:|$)", desc, re.S)
        return m.group(1).strip() if m else ""

    return {
        "creation_date": creation_date,
        "content": _field("Content", ["Creation Date", "Idea", "Scripts", "Caption", "Link"]),
        "idea": _field("Idea", ["Creation Date", "Content", "Scripts", "Caption", "Link"]),
        "scripts": _field("Scripts", ["Creation Date", "Content", "Idea", "Caption", "Link"]),
        "caption": _field("Caption", ["Creation Date", "Content", "Idea", "Scripts", "Link"]),
        "link": _field("Link", ["Creation Date", "Content", "Idea", "Scripts", "Caption", "Notes"]),
        "myNotes": _field("Notes", ["Creation Date", "Content", "Idea", "Scripts", "Caption", "Link"]),
    }


def _build_sheet_notes(fields: dict) -> str:
    """Python port of the detailParts join inside applySheetFields()."""
    parts = []
    if fields.get("content"): parts.append(f"Content: {fields['content']}")
    if fields.get("idea"): parts.append(f"Idea: {fields['idea']}")
    if fields.get("scripts"): parts.append(f"Scripts: {fields['scripts']}")
    if fields.get("caption"): parts.append(f"Caption: {fields['caption']}")
    if fields.get("link"): parts.append(f"Link: {fields['link']}")
    if fields.get("myNotes"): parts.append(f"Notes: {fields['myNotes']}")
    return " | ".join(parts)


def _task_to_fields(task: dict) -> dict:
    """task is shaped like one entry from notion_store.list_tasks() (or the
    SQLite-mode equivalent built in _current_tasks_by_id below) -- must have
    title/description/due_date/creation_date/status/assigned_to keys."""
    title_raw = task.get("title") or ""
    m = re.match(r"^\[(.*?)\]\s*(.*)$", title_raw)
    ttype, title = (m.group(1), m.group(2)) if m else ("Post", title_raw)
    parsed = _parse_sheet_notes(task.get("description", ""), task.get("creation_date", ""))
    return {
        "creation_date": parsed["creation_date"] or task.get("creation_date", "") or "",
        "due_date": task.get("due_date", "") or "",
        "title": title, "type": ttype,
        "content": parsed["content"], "idea": parsed["idea"], "scripts": parsed["scripts"],
        "caption": parsed["caption"], "link": parsed["link"], "myNotes": parsed["myNotes"],
        "assigned_to": task.get("assigned_to", "") or "",
        "status": (task.get("status") or "").lower().replace(" ", "_"),
    }


def _fields_to_row(task_id: str, fields: dict) -> list:
    return [task_id] + [fields.get(f, "") for f in SHEET_FIELDS]


def _row_to_fields(row: list) -> dict:
    padded = list(row) + [""] * (13 - len(row))
    fields = dict(zip(SHEET_FIELDS, [str(v).strip() for v in padded[1:13]]))
    fields["status"] = fields["status"].lower().replace(" ", "_")
    return fields


# ── Task adapters (Notion + SQLite) ─────────────────────────────────────

def _current_tasks_by_id(client_id: str, is_notion: bool) -> dict:
    """Snapshot of every task Lumina currently has for this client, keyed by
    id, fetched once per reconciliation pass rather than once per row -- see
    reconcile_sheet_rows below for why (avoids an N+1 Notion API call)."""
    if is_notion:
        tasks = notion_store.list_tasks(client_notion_id=client_id)
        return {t["notion_id"]: t for t in tasks}
    from db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id,title,description,assigned_to,due_date,status FROM tasks WHERE client_id=?", (client_id,))
    rows = cur.fetchall()
    conn.close()
    return {
        str(r[0]): {"notion_id": str(r[0]), "title": r[1], "description": r[2],
                     "assigned_to": r[3], "due_date": r[4], "status": r[5], "creation_date": ""}
        for r in rows
    }


def _create_task(client_id: str, client_name: str, is_notion: bool, fields: dict):
    new_title = f"[{fields.get('type') or 'Post'}] {fields.get('title') or 'New Idea'}"
    notes = _build_sheet_notes(fields)
    if is_notion:
        result = notion_store.create_task(
            title=new_title, client_name=client_name, client_notion_id=client_id,
            assigned_to=fields.get("assigned_to", ""), due_date=fields.get("due_date", ""),
            status=fields.get("status") or "not_started", service="Social Media",
            notes=notes, creation_date=fields.get("creation_date", ""),
        )
        return result["notion_id"] if result else None
    from db import get_connection
    conn = get_connection()
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks (client_id,title,description,assigned_to,due_date,status,progress) VALUES (?,?,?,?,?,?,0)",
            (client_id, new_title, notes, fields.get("assigned_to", ""), fields.get("due_date", ""),
             fields.get("status") or "not_started"),
        )
        task_id = cur.lastrowid
    conn.close()
    return str(task_id)


def _update_task(task_id: str, is_notion: bool, fields: dict, editor_name: str) -> bool:
    new_title = f"[{fields.get('type') or 'Post'}] {fields.get('title') or ''}"
    notes = _build_sheet_notes(fields)
    if is_notion:
        last_edited = f"{today_ist()} {now_ist()}|{editor_name}|Google Sheets sync"
        return notion_store.update_task(
            notion_id=task_id, status=fields.get("status"), submission_note=notes,
            assigned_to=fields.get("assigned_to", ""), new_title=new_title,
            due_date=fields.get("due_date"), creation_date=fields.get("creation_date"),
            last_edited=last_edited,
        )
    from db import get_connection
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE tasks SET title=?, description=?, assigned_to=?, due_date=?, status=?, "
            "last_edited_by=?, last_edited_at=?, last_edited_summary=? WHERE id=?",
            (new_title, notes, fields.get("assigned_to", ""), fields.get("due_date", ""),
             fields.get("status") or "not_started", editor_name, f"{today_ist()} {now_ist()}",
             "Google Sheets sync", task_id),
        )
    conn.close()
    return True


def _delete_task(task_id: str, is_notion: bool) -> bool:
    if is_notion:
        return notion_store.archive_notion_page(task_id)
    from db import get_connection
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.close()
    return True


# ── Push / reconcile ────────────────────────────────────────────────────

def push_task_to_sheet(link: dict, task_id: str, fields: dict) -> bool:
    """Lumina -> Sheet. Fire-and-forget from the frontend's perspective: a
    push failure here must never fail the save that already succeeded in
    Notion/SQLite (same contract as log_sheet_version in routes/ops.py).
    Returns whether the push itself succeeded, so the caller can record
    sync-health state (see routes/sheets_sync.py) without changing that
    fire-and-forget contract from the frontend's point of view."""
    if not is_configured():
        return False
    spreadsheet_id = link["spreadsheet_id"]
    try:
        rows = read_all_rows(spreadsheet_id)
    except Exception:
        logger.exception(f"Sheets push: failed to read sheet {spreadsheet_id}")
        return False

    row_number = None
    for i, row in enumerate(rows):
        if i == 0:
            continue  # header
        if row and str(row[0]).strip() == str(task_id):
            row_number = i + 1
            break

    values = _fields_to_row(task_id, fields)
    try:
        if row_number:
            write_row(spreadsheet_id, row_number, values)
        else:
            append_row(spreadsheet_id, values)
        return True
    except Exception:
        logger.exception(f"Sheets push: failed to write row for task {task_id}")
        return False


def reconcile_sheet_rows(link: dict, rows: list) -> dict:
    """Sheet -> Lumina. `rows` is data rows only (the Apps Script snippet
    strips the header before posting). Full-snapshot diff against the
    client's current tasks, not incremental per-cell events -- chosen to
    avoid this codebase's recurring diff-only-payload bug class (CLAUDE.md
    gotchas #45/#51/#69).

    Loop safety: this function only ever writes back to the Sheet for the
    "brand new row" case (write_cell with the newly created task_id) -- every
    other branch writes to Notion/SQLite only. A push-triggered Sheet write
    does fire the Sheet's own onChange and lands back here, but by then the
    Sheet and Lumina already agree, so that pass's own diff is empty for that
    row and terminates without writing anywhere -- no unbounded ping-pong."""
    client_id = link["client_id"]
    is_notion = link["is_notion"]
    client_name = link.get("client_name") or ""
    spreadsheet_id = link["spreadsheet_id"]
    editor_name = link.get("linked_by") or "Google Sheets"

    current = _current_tasks_by_id(client_id, is_notion)
    seen_ids = set()
    created, updated, deleted, skipped = 0, 0, 0, 0

    for idx, row in enumerate(rows):
        row_number = idx + 2  # +1 for 0-index, +1 for the header row Apps Script stripped
        task_id = str(row[0]).strip() if row else ""
        row_fields = _row_to_fields(row)
        if not task_id and not any(row_fields.values()):
            continue  # fully blank row

        if not task_id:
            new_id = _create_task(client_id, client_name, is_notion, row_fields)
            if new_id:
                try:
                    write_cell(spreadsheet_id, f"A{row_number}", new_id)
                except Exception:
                    logger.exception(f"Sheets reconcile: failed to write back task id for row {row_number}")
                created += 1
            continue

        seen_ids.add(task_id)
        existing = current.get(task_id)
        if not existing:
            # References a task id Lumina no longer has -- nothing to reconcile against.
            continue
        if _task_to_fields(existing) == row_fields:
            skipped += 1
            continue
        _update_task(task_id, is_notion, row_fields, editor_name)
        updated += 1

    # Safety guard: if this snapshot recognized zero existing tasks (empty
    # `rows`, or every row was a blank/unrecognized/newly-created one) while
    # Lumina has tasks on file for this client, treat it as a suspicious
    # payload rather than "the user deleted everything" -- a flaky onChange
    # firing mid-paste, a momentarily cleared sheet, or a script bug could
    # otherwise wipe every task for a client in one request. Deleting
    # everything on purpose should go through an explicit action, not a
    # webhook payload that happens to recognize nothing.
    if not seen_ids and current:
        logger.warning(
            f"Sheets reconcile: snapshot for client {client_id} recognized 0 of "
            f"{len(current)} known tasks -- skipping delete pass as a safety measure."
        )
        return {"created": created, "updated": updated, "deleted": 0, "skipped": skipped,
                "deletes_skipped_safety": len(current)}

    for existing_id in current:
        if existing_id not in seen_ids:
            _delete_task(existing_id, is_notion)
            deleted += 1

    return {"created": created, "updated": updated, "deleted": deleted, "skipped": skipped}
