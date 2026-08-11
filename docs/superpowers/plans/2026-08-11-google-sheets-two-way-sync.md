# Google Sheets Two-Way Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any client, opt-in, be linked to an external Google Sheet so edits in Lumina's Sheets tab push to the Sheet, and edits made directly in the Sheet pull back into Lumina, in near-real-time, in both directions.

**Architecture:** A new `google_sheet_links` table maps `client_id` → a linked Google Sheet. Lumina → Sheet writes hook into the existing `applySheetFields()` save path in `projects.html` and call a new push endpoint. Sheet → Lumina writes come from an Apps Script `onChange` trigger (installed once per Sheet during setup) POSTing the sheet's full contents to a token-authenticated webhook, which reconciles by diffing against the client's current tasks (create/update/delete/skip).

**Tech Stack:** Flask blueprint + SQLite (existing patterns), `google-auth` for service-account JWT signing, raw Sheets API v4 REST calls via `google.auth.transport.requests.AuthorizedSession` (kept thin/raw rather than the heavy `google-api-python-client`, matching this codebase's existing raw-`requests` convention for Notion — see `backend/notion_store.py`).

## Global Constraints

- This codebase has no automated test suite (no `pytest`, no `tests/` directory anywhere) — every existing gotcha in `CLAUDE.md` verifies changes via `pyflakes` (Python), `node --check` on extracted `<script>` blocks (JS), and manual `curl`/live-endpoint checks. This plan follows that same established verification convention instead of writing pytest test files — each task's "test" steps are pyflakes/node-check/curl commands, not a new test framework.
- Never commit the Google service-account JSON key. It is read from env var `GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON` only.
- `assigned_to` in the Google Sheet is plain-text comma-separated employee **display names** (e.g. `Abhinav Gupta, Palak`) — matching exactly what's already stored in Notion's `Assigned To` property and shown everywhere else in this app. There is no id-mapping layer for the Sheet column (unlike the checkbox-based Lumina UI, which maps ids → names before writing to Notion).
- `Creation Date` and `Post Day` columns in the Google Sheet template must be formatted as **Plain Text**, not Date, so a typed `YYYY-MM-DD` round-trips verbatim instead of becoming a locale-dependent date serial/string. This is a setup instruction shown to the user, not something enforceable in code.
- Follow existing patterns: new DB table via `CREATE TABLE IF NOT EXISTS` in `db.py`; new blueprint registered the same way as `ops_bp`/`auth_bp`; frontend save/log-version fire-and-forget pattern already used by `applySheetFields()`.

---

### Task 1: `google_sheet_links` table

**Files:**
- Modify: `backend/db.py` (insert right after the `sheet_hidden_rows` block, ~line 343)

**Interfaces:**
- Produces: table `google_sheet_links(client_id TEXT PRIMARY KEY, spreadsheet_id TEXT, link_token TEXT UNIQUE, is_notion INTEGER, client_name TEXT, linked_at TEXT, linked_by TEXT)` — consumed by Task 6/7's routes and Task 4/5's store functions.

- [ ] **Step 1: Add the table**

Insert after the `idx_sheet_hidden_rows_client` index line (~343):

```python
        # Google Sheets two-way sync -- one linked spreadsheet per client.
        # link_token authenticates the inbound pull webhook (see routes/sheets_sync.py);
        # is_notion records whether this client's tasks live in Notion or the local
        # SQLite `clients`/`tasks` tables, decided once at link time from the same
        # `notionMode && !!client.notion_id` check the frontend already uses.
        conn.execute("""CREATE TABLE IF NOT EXISTS google_sheet_links (
            client_id      TEXT PRIMARY KEY,
            spreadsheet_id TEXT NOT NULL,
            link_token     TEXT NOT NULL UNIQUE,
            is_notion      INTEGER NOT NULL DEFAULT 0,
            client_name    TEXT,
            linked_at      TEXT,
            linked_by      TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_google_sheet_links_token ON google_sheet_links (link_token)")
```

- [ ] **Step 2: Verify**

Run: `.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'backend'); import db; db.init_db(); print('ok')"` from the repo root.
Expected: prints `ok` with no exception (confirms the new `CREATE TABLE`/`CREATE INDEX` statements are syntactically valid and idempotent against the real `logs/app.db`).

- [ ] **Step 3: Commit**

```bash
git add backend/db.py
git commit -m "Add google_sheet_links table for two-way Sheets sync"
```

---

### Task 2: `google_sheets_store.py` — auth + raw Sheets API access

**Files:**
- Create: `backend/google_sheets_store.py`
- Modify: `backend/requirements.txt`, `requirements.txt` (add `google-auth`)

**Interfaces:**
- Produces: `is_configured() -> bool`, `service_account_email() -> str`, `read_all_rows(spreadsheet_id) -> list[list]`, `write_row(spreadsheet_id, row_number, values)`, `append_row(spreadsheet_id, values) -> int`, `write_cell(spreadsheet_id, a1_cell, value)`.

- [ ] **Step 1: Add the dependency**

Append `google-auth>=2.29.0` to both `backend/requirements.txt` and `requirements.txt` (root), each on its own line at the end of the file.

- [ ] **Step 2: Create the module**

```python
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

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_session = None
_service_account_email_cache = None


def is_configured() -> bool:
    return bool(os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON"))


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
```

- [ ] **Step 3: Verify**

Run: `pip install google-auth>=2.29.0` (or `.venv/Scripts/pip.exe install google-auth>=2.29.0`), then `.venv/Scripts/python.exe -m pyflakes backend/google_sheets_store.py`.
Expected: pip install succeeds; pyflakes prints nothing (clean).

- [ ] **Step 4: Commit**

```bash
git add backend/google_sheets_store.py backend/requirements.txt requirements.txt
git commit -m "Add raw Google Sheets API v4 client via service-account auth"
```

---

### Task 3: Field mapping helpers (parse/build, shared row shape)

**Files:**
- Modify: `backend/google_sheets_store.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `SHEET_FIELDS` (list of 12 field-key strings, in column order), `_parse_sheet_notes(desc, existing_creation_date) -> dict`, `_build_sheet_notes(fields) -> str`, `_task_to_fields(task) -> dict`, `_fields_to_row(task_id, fields) -> list`, `_row_to_fields(row) -> dict`. These are consumed by Task 5's push/reconcile functions.

- [ ] **Step 1: Append the helpers**

```python
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
    SQLite-mode equivalent built in _current_tasks_by_id, Task 4) -- must have
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
```

- [ ] **Step 2: Verify**

Run: `.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'backend')
import google_sheets_store as gs
notes = gs._build_sheet_notes({'content':'hello','myNotes':'a note'})
print(notes)
print(gs._parse_sheet_notes(notes))
fields = gs._task_to_fields({'title':'[Story] My Post','description':notes,'due_date':'2026-08-20','status':'In Progress','assigned_to':'Abhinav Gupta'})
print(fields)
row = gs._fields_to_row('abc123', fields)
print(row)
print(gs._row_to_fields(row))
"`
Expected: no exceptions; the final `_row_to_fields(row)` dict matches `fields` exactly (round-trip check) except `status` is already normalized in both.

- [ ] **Step 3: Commit**

```bash
git add backend/google_sheets_store.py
git commit -m "Add Sheets<->task field mapping helpers, ported from projects.html"
```

---

### Task 4: Task create/update/delete adapters (Notion + SQLite)

**Files:**
- Modify: `backend/google_sheets_store.py` (append)

**Interfaces:**
- Consumes: `notion_store.create_task/update_task/archive_notion_page/list_tasks` (existing), `db.get_connection` (existing), `_build_sheet_notes` (Task 3).
- Produces: `_current_tasks_by_id(client_id, is_notion) -> dict[str, dict]`, `_create_task(...) -> Optional[str]`, `_update_task(...) -> bool`, `_delete_task(...) -> bool`. Consumed by Task 5.

- [ ] **Step 1: Append the adapters**

Also add these two imports to the top of `backend/google_sheets_store.py` (next to the existing `import re`, from Task 2):

```python
import notion_store
from utils import today_ist, now_ist
```

```python
def _current_tasks_by_id(client_id: str, is_notion: bool) -> dict:
    """Snapshot of every task Lumina currently has for this client, keyed by
    id, fetched once per reconciliation pass rather than once per row -- see
    reconcile_sheet_rows in Task 5 for why (avoids an N+1 Notion API call)."""
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


def _create_task(client_id: str, client_name: str, is_notion: bool, fields: dict) -> "str | None":
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
```

Note: `sqlite3` `Row` objects aren't used elsewhere in this module — plain tuple indexing matches `sqlite_patch_task`'s own style in `routes/ops.py`.

- [ ] **Step 2: Verify**

Run: `.venv/Scripts/python.exe -m pyflakes backend/google_sheets_store.py`
Expected: clean (no undefined names, no unused imports — `notion_store`/`today_ist`/`now_ist` are now used).

- [ ] **Step 3: Commit**

```bash
git add backend/google_sheets_store.py
git commit -m "Add Notion/SQLite task create-update-delete adapters for Sheets sync"
```

---

### Task 5: Push and reconcile

**Files:**
- Modify: `backend/google_sheets_store.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 2-4.
- Produces: `push_task_to_sheet(link, task_id, fields)`, `reconcile_sheet_rows(link, rows) -> dict`. Consumed by Task 7's routes.
- `link` is a dict with keys `client_id, spreadsheet_id, is_notion, client_name, linked_by`.

- [ ] **Step 1: Append**

```python
def push_task_to_sheet(link: dict, task_id: str, fields: dict):
    """Lumina -> Sheet. Fire-and-forget from the caller's perspective: a push
    failure here must never fail the save that already succeeded in
    Notion/SQLite (same contract as log_sheet_version in routes/ops.py)."""
    if not is_configured():
        return
    spreadsheet_id = link["spreadsheet_id"]
    try:
        rows = read_all_rows(spreadsheet_id)
    except Exception:
        logger.exception(f"Sheets push: failed to read sheet {spreadsheet_id}")
        return

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
    except Exception:
        logger.exception(f"Sheets push: failed to write row for task {task_id}")


def reconcile_sheet_rows(link: dict, rows: list) -> dict:
    """Sheet -> Lumina. `rows` is data rows only (the Apps Script snippet
    strips the header before posting, see Task 6). Full-snapshot diff against
    the client's current tasks, not incremental per-cell events -- chosen to
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

    for existing_id in current:
        if existing_id not in seen_ids:
            _delete_task(existing_id, is_notion)
            deleted += 1

    return {"created": created, "updated": updated, "deleted": deleted, "skipped": skipped}
```

- [ ] **Step 2: Verify**

Run: `.venv/Scripts/python.exe -m pyflakes backend/google_sheets_store.py`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add backend/google_sheets_store.py
git commit -m "Add push/reconcile: Lumina<->Google Sheet two-way sync core"
```

---

### Task 6: `routes/sheets_sync.py` — link management routes

**Files:**
- Create: `backend/routes/sheets_sync.py`
- Modify: `backend/app.py` (register the blueprint, ~line 58/86)

**Interfaces:**
- Consumes: `google_sheets_store` (Tasks 2-5), `utils._is_admin/today_ist/now_ist`, `db.get_connection`.
- Produces: `sheets_sync_bp` with `POST/GET/DELETE /api/clients/<client_id>/google-sheet-link`. Consumed by Task 9's frontend UI.

- [ ] **Step 1: Create the blueprint file**

```python
"""
Google Sheets two-way sync -- link management (connect/status/unlink a
client's Google Sheet). Push and pull-webhook routes are in Task 7.
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
    """Internal helper for Task 7's push route -- returns the plain dict
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
```

- [ ] **Step 2: Register the blueprint**

In `backend/app.py`, add near the other blueprint imports (~line 58):

```python
from routes.sheets_sync import sheets_sync_bp
```

And register it next to the others (~line 86):

```python
app.register_blueprint(sheets_sync_bp)
```

- [ ] **Step 3: Verify**

Run: `.venv/Scripts/python.exe -m pyflakes backend/routes/sheets_sync.py backend/app.py`
Expected: clean (no new warnings vs. baseline).

Run: `.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'backend'); import app; print('boots')"`
Expected: prints `boots` with no exception (confirms the blueprint import/registration doesn't break Flask startup).

- [ ] **Step 4: Commit**

```bash
git add backend/routes/sheets_sync.py backend/app.py
git commit -m "Add Google Sheets link management routes (connect/status/unlink)"
```

---

### Task 7: Push endpoint + pull webhook

**Files:**
- Modify: `backend/routes/sheets_sync.py` (append)

**Interfaces:**
- Consumes: `get_link_for_client`, `get_link_by_token` (Task 6), `gs.push_task_to_sheet`, `gs.reconcile_sheet_rows` (Task 5).
- Produces: `POST /api/sheets/push/<task_id>`, `POST /api/sheets/webhook/<link_token>`. Consumed by Task 8 (frontend push call) and the Apps Script snippet (Task 6) respectively.

- [ ] **Step 1: Append the two routes**

```python
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
```

- [ ] **Step 2: Verify**

Run: `.venv/Scripts/python.exe -m pyflakes backend/routes/sheets_sync.py`
Expected: clean.

Start the dev server (`.venv/Scripts/python.exe -m backend.app` in one shell) and, in another shell, run:
```bash
curl -s -X POST http://localhost:5000/api/sheets/push/nonexistent-task -H "Content-Type: application/json" -d "{\"client_id\":\"nonexistent-client\",\"fields\":{}}"
curl -s -X POST http://localhost:5000/api/sheets/webhook/bad-token -H "Content-Type: application/json" -d "{\"rows\":[]}"
```
Expected: first returns `{"success": true, "linked": false}` (no crash for an unlinked client); second returns `{"error": "Unknown link"}` with a 404 (no crash for a bad token).

- [ ] **Step 3: Commit**

```bash
git add backend/routes/sheets_sync.py
git commit -m "Add Sheets push endpoint and pull webhook"
```

---

### Task 8: Wire push into `applySheetFields()`

**Files:**
- Modify: `frontend/projects.html` (~line 2534, right after the existing `log-version` fire-and-forget call)

**Interfaces:**
- Consumes: `POST /api/sheets/push/<task_id>` (Task 7). Fires unconditionally; the backend itself decides whether the client is linked (no-ops otherwise) -- the frontend does not need to know the link state to call this safely.

- [ ] **Step 1: Add the push call**

In `applySheetFields()`, right after the existing `log-version` fetch block (~line 2534-2543, `fetch(`${API}/api/sheets/tasks/${taskId}/log-version`, ...)`), add:

```javascript
    // Push this save out to the client's linked Google Sheet, if any. Same
    // fire-and-forget contract as the log-version call above -- the backend
    // no-ops for unlinked clients, so this is safe to call unconditionally.
    fetch(`${API}/api/sheets/push/${taskId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_id: clientId,
        fields: { creation_date, due_date, title, type, content, idea, scripts, caption, link, myNotes, assigned_to, status },
      }),
    }).catch(() => {});
```

- [ ] **Step 2: Verify**

Extract inline `<script>` blocks and run `node --check`:
```bash
node -e "const fs=require('fs');const html=fs.readFileSync('frontend/projects.html','utf8');const m=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];fs.writeFileSync('D:/temp/claude/c--Users-abhin-OneDrive-Desktop-claude-office-assistant/c324aa17-b561-40bb-81e4-c13fc6de453f/scratchpad/pjs-check.js', m.map(x=>x[1]).join('\n;\n'))"
node --check "D:/temp/claude/c--Users-abhin-OneDrive-Desktop-claude-office-assistant/c324aa17-b561-40bb-81e4-c13fc6de453f/scratchpad/pjs-check.js"
```
Expected: no syntax error.

- [ ] **Step 3: Commit**

```bash
git add frontend/projects.html
git commit -m "Push Sheets edits to a client's linked Google Sheet on every save"
```

---

### Task 9: "Connect Google Sheet" UI

**Files:**
- Modify: `frontend/projects.html` (Sheets toolbar area, near the existing "Edit History"/"Hidden (N)" buttons, and the client-level Sheets tab render function)

**Interfaces:**
- Consumes: `GET/POST/DELETE /api/clients/<client_id>/google-sheet-link` (Task 6).

- [ ] **Step 1: Add a toolbar button + modal**

Add a button next to the existing "🕓 Edit History" button in the Sheets toolbar (same toolbar row referenced in gotcha #76):

```html
<button class="btn-sm" onclick="openGoogleSheetLinkModal('${clientId}')" id="gsheet-link-btn-${clientId}">🔗 Google Sheet</button>
```

Add the modal markup once, near the other overlay markup (`#sheet-history-overlay`, etc.):

```html
<div id="gsheet-link-overlay" style="display:none;position:fixed;inset:0;z-index:10000;background:var(--s1);overflow:auto;">
  <div style="max-width:640px;margin:40px auto;padding:24px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
      <h2 style="margin:0;">Connect Google Sheet</h2>
      <button class="btn-sm" onclick="closeGoogleSheetLinkModal()">Close</button>
    </div>
    <div id="gsheet-link-body"></div>
  </div>
</div>
```

- [ ] **Step 2: Add the JS**

```javascript
let gsheetLinkClientId = null;

function closeGoogleSheetLinkModal() {
  document.getElementById('gsheet-link-overlay').style.display = 'none';
}

async function openGoogleSheetLinkModal(clientId) {
  gsheetLinkClientId = clientId;
  const overlay = document.getElementById('gsheet-link-overlay');
  const body = document.getElementById('gsheet-link-body');
  overlay.style.display = 'block';
  body.innerHTML = 'Loading…';
  try {
    const res = await fetch(`${API}/api/clients/${clientId}/google-sheet-link`);
    const d = await res.json();
    if (d.linked) {
      body.innerHTML = `
        <p><strong>Linked</strong> to spreadsheet <code>${d.spreadsheet_id}</code></p>
        <p>Linked by ${d.linked_by || 'someone'} on ${d.linked_at || ''}</p>
        <p>Share the Sheet with: <code>${d.service_account_email}</code></p>
        <p>Apps Script (paste into the Sheet's Apps Script editor, then run <code>installTrigger</code> once):</p>
        <textarea readonly style="width:100%;height:180px;font-family:monospace;font-size:0.8rem;">${d.apps_script}</textarea>
        <button class="btn-sm" onclick="unlinkGoogleSheet('${clientId}')">Unlink</button>
      `;
    } else {
      body.innerHTML = `
        <p>Paste the URL of an existing Google Sheet to link it to this client. The Sheet needs a hidden column A (lumina_task_id) and 12 columns after it matching the Sheets tab's fields (Creation Date, Post Day, Post Title, Type, Content, Idea, Scripts/Copy, Caption, File (Drive Link), Notes, Assigned To, Status).</p>
        <input type="text" id="gsheet-url-input" placeholder="https://docs.google.com/spreadsheets/d/..." style="width:100%;padding:8px;margin-bottom:8px;">
        <button class="btn-sm" onclick="submitGoogleSheetLink('${clientId}')">Connect</button>
      `;
    }
  } catch (e) {
    body.innerHTML = 'Failed to load link status.';
  }
}

async function submitGoogleSheetLink(clientId) {
  const url = document.getElementById('gsheet-url-input').value.trim();
  if (!url) { toast('Paste a Sheet URL first', 'err'); return; }
  const client = allData.clients.find(c => String(c.id) === String(clientId));
  const isNotionClient = notionMode && !!(client && client.notion_id);
  try {
    const res = await fetch(`${API}/api/clients/${clientId}/google-sheet-link`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: UID, spreadsheet_url: url, is_notion: isNotionClient,
        client_name: client ? client.name : '',
      }),
    });
    const d = await res.json();
    if (!d.success) { toast(d.error || 'Failed to connect', 'err'); return; }
    toast('Sheet connected');
    openGoogleSheetLinkModal(clientId);
  } catch (e) {
    toast('Network error connecting sheet', 'err');
  }
}

async function unlinkGoogleSheet(clientId) {
  if (!confirm('Unlink this Google Sheet? Existing tasks in Lumina are unaffected.')) return;
  try {
    await fetch(`${API}/api/clients/${clientId}/google-sheet-link`, { method: 'DELETE' });
    toast('Sheet unlinked');
    openGoogleSheetLinkModal(clientId);
  } catch (e) {
    toast('Failed to unlink', 'err');
  }
}
```

- [ ] **Step 3: Verify**

Same `node --check` procedure as Task 8, re-run against the updated file.
Expected: no syntax error.

- [ ] **Step 4: Commit**

```bash
git add frontend/projects.html
git commit -m "Add Connect Google Sheet UI (link status, setup instructions, unlink)"
```

---

### Task 10: End-to-end verification + CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (append a new numbered gotcha documenting the feature, its setup requirements, and any live-test findings)

**Interfaces:** none (verification-only task).

- [ ] **Step 1: Static verification pass**

```bash
.venv/Scripts/python.exe -m pyflakes backend/*.py backend/routes/*.py
.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'backend'); import app; print('boots')"
```
Expected: no new pyflakes warnings vs. the pre-existing baseline; `boots` printed.

- [ ] **Step 2: Live setup, if a `GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON` credential is available**

1. Set the env var locally, restart the dev server.
2. Create a real test Google Sheet with the 13-column header row (`lumina_task_id, Creation Date, Post Day, Post Title, Type, Content, Idea, Scripts/Copy, Caption, File (Drive Link), Notes, Assigned To, Status`), columns B/C formatted as Plain Text.
3. Share it with the service account email shown by `GET /api/clients/<test-client-id>/google-sheet-link` after a successful connect.
4. In Lumina, edit a Sheets row for that client and confirm the row appears/updates in the Google Sheet within a few seconds.
5. Paste the Apps Script snippet into the Sheet, run `installTrigger` once, then type a new row directly into the Sheet and confirm a new task appears in Lumina; edit an existing linked row's Status cell and confirm it updates in Lumina; delete a row and confirm the task is removed from Lumina.

If no credential is available this session, skip this step and note in CLAUDE.md that live verification is still pending.

- [ ] **Step 3: Document in CLAUDE.md**

Append a new numbered gotcha under "Gotchas & Known Issues" summarizing: the feature, the `GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON` env var requirement (add it to the Environment Variables table too), the Plain-Text-column setup requirement, the loop-safety reasoning, and whether live end-to-end verification was actually performed this session or is still outstanding.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "Document Google Sheets two-way sync in CLAUDE.md"
```
