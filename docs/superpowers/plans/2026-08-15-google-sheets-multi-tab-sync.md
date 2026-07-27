# Google Sheets Multi-Tab Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing Google Sheets two-way sync so a client's linked spreadsheet can have one tab per due-date month (plus an "Unscheduled" tab), synced in both directions, while every already-linked client keeps its current single-tab behavior untouched until manually reconnected.

**Architecture:** A new `multi_tab` flag on `google_sheet_links` gates a parallel set of tab-aware functions in `google_sheets_store.py` (`*_multi_tab` / `reconcile_sheet_tabs`) that sit alongside the existing single-tab functions, which are left byte-for-byte unmodified. `routes/sheets_sync.py` dispatches to whichever set matches a given link's `multi_tab` value. The Apps Script snippet gains a multi-tab variant that enumerates matching tabs and POSTs `{tabs: {...}}` instead of a flat `{rows: [...]}`. Lumina's own in-app Sheets view gets a month-tab strip (replacing the existing dropdown) purely as a display filter over the same task list — no new storage.

**Tech Stack:** Python 3.11 / Flask (`backend/google_sheets_store.py`, `backend/routes/sheets_sync.py`, `backend/db.py`), vanilla JS (`frontend/projects.html`), Google Sheets API v4 via `google-auth`'s `AuthorizedSession`, Google Apps Script (generated snippet, user-installed).

**Spec:** `docs/superpowers/specs/2026-08-15-google-sheets-multi-tab-sync-design.md`

## Global Constraints

- MMGA (the one already-linked client) must see **zero** behavior change from this work until someone explicitly reconnects it — enforced by leaving every existing function signature and body in `google_sheets_store.py` untouched, and adding new `*_multi_tab` functions instead of branching inside the old ones.
- Recognized tab names: `"<Month> <Year>"` (e.g. `"August 2026"`, real month names only) or the literal `"Unscheduled"`. Any other tab name is never read or written.
- A task's target tab is derived from `due_date` (Post Day); blank `due_date` → `"Unscheduled"`.
- Missing target tabs are auto-created (with the standard header row) on push, never on reconcile.
- No pytest suite exists in this codebase — verification is `pyflakes`, `node --check`, an `import app` boot check, and hand-written scratch scripts run via `.venv/Scripts/python.exe` against a temp SQLite DB with mocked Sheets/Notion calls (never production data), matching how every prior round of this feature was verified (see CLAUDE.md gotcha #87).
- Every scratch test file goes in the session scratchpad directory and is deleted after use, never committed.

---

### Task 1: `multi_tab` column migration

**Files:**
- Modify: `backend/db.py:359-366`

**Interfaces:**
- Produces: `google_sheet_links.multi_tab` column (`INTEGER`, default `0` for pre-existing rows), read/written by every later task in this plan.

- [ ] **Step 1: Add the column migration**

In `backend/db.py`, immediately after the existing `last_push_at`/`last_push_ok`/`last_pull_at`/`last_pull_summary` migration loop (ends at line 365 with `pass  # Column already exists`), add:

```python
        # multi_tab -- whether this client's sync targets one tab per
        # due-date month (see google_sheets_store.py's *_multi_tab
        # functions / reconcile_sheet_tabs) or the original single-first-tab
        # sync. Defaults 0 so every already-linked client (existing rows
        # predate this column and get SQLite's column default on ALTER
        # TABLE) keeps its current single-tab behavior untouched; new
        # connects explicitly insert 1 (see
        # routes/sheets_sync.py::create_google_sheet_link).
        try:
            conn.execute("ALTER TABLE google_sheet_links ADD COLUMN multi_tab INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists
```

- [ ] **Step 2: Verify the migration runs clean**

Run: `.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'backend'); import db; db.init_db(); print('ok')"`
Expected: prints `ok` with no exception, confirming the `ALTER TABLE` applies cleanly against the real `logs/app.db` (idempotent — safe to run again).

- [ ] **Step 3: Commit**

```bash
git add backend/db.py
git commit -m "Add multi_tab column for per-client month-tab Sheets sync"
```

---

### Task 2: Tab-name and date-routing helpers

**Files:**
- Modify: `backend/google_sheets_store.py:97-116` (insert new section between `_retry` and `_SCOPES`)

**Interfaces:**
- Produces: `UNSCHEDULED_TAB_NAME: str`, `_is_synced_tab_name(name: str) -> bool`, `_month_tab_name_for(due_date: str) -> str`, `_a1_quote(sheet_name: str) -> str` — all consumed by Tasks 3-6.

- [ ] **Step 1: Insert the helpers**

In `backend/google_sheets_store.py`, right after the `_retry` function (which ends at line 114 with `return False`) and before the `_SCOPES = [...]` line, insert:

```python
# ── Multi-tab (monthly calendar) support ────────────────────────────────
# A linked spreadsheet can have one tab per due-date month (e.g. "August
# 2026") plus a fixed "Unscheduled" tab for tasks with no due date. Only
# tabs matching this exact naming convention are ever read or written --
# any other tab (personal notes, scratch work, etc.) is invisible to sync.
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]
_MONTH_TAB_RE = re.compile(r"^(" + "|".join(_MONTH_NAMES) + r") \d{4}$")
UNSCHEDULED_TAB_NAME = "Unscheduled"


def _is_synced_tab_name(name: str) -> bool:
    return name == UNSCHEDULED_TAB_NAME or bool(_MONTH_TAB_RE.match(name or ""))


def _month_tab_name_for(due_date: str) -> str:
    """"August 2026"-style tab name for a task's due_date (Post Day), or
    UNSCHEDULED_TAB_NAME if due_date is blank or unparseable."""
    if not due_date:
        return UNSCHEDULED_TAB_NAME
    try:
        dt = datetime.strptime(str(due_date)[:10], "%Y-%m-%d")
    except Exception:
        return UNSCHEDULED_TAB_NAME
    return f"{_MONTH_NAMES[dt.month - 1]} {dt.year}"


def _a1_quote(sheet_name: str) -> str:
    """Quotes a tab name for use in A1 range notation, e.g. 'August 2026'.
    Doubles any literal single quote per A1 notation's own escaping rule --
    cheap defense-in-depth, the enforced naming convention never actually
    produces one."""
    return "'" + sheet_name.replace("'", "''") + "'"
```

- [ ] **Step 2: Verify with a scratch script**

Write `D:\temp\claude\c--Users-abhin-OneDrive-Desktop-claude-office-assistant\c324aa17-b561-40bb-81e4-c13fc6de453f\scratchpad\test_tab_helpers.py`:

```python
import sys
sys.path.insert(0, "backend")
import google_sheets_store as gs

assert gs._month_tab_name_for("2026-08-15") == "August 2026", gs._month_tab_name_for("2026-08-15")
assert gs._month_tab_name_for("") == "Unscheduled"
assert gs._month_tab_name_for(None) == "Unscheduled"
assert gs._month_tab_name_for("not-a-date") == "Unscheduled"
assert gs._is_synced_tab_name("August 2026") is True
assert gs._is_synced_tab_name("Unscheduled") is True
assert gs._is_synced_tab_name("Notes") is False
assert gs._is_synced_tab_name("august 2026") is False  # case-sensitive, matches real month names only
assert gs._a1_quote("August 2026") == "'August 2026'"
assert gs._a1_quote("O'Brien 2026") == "'O''Brien 2026'"
print("ok")
```

Run: `.venv/Scripts/python.exe D:\temp\claude\c--Users-abhin-OneDrive-Desktop-claude-office-assistant\c324aa17-b561-40bb-81e4-c13fc6de453f\scratchpad\test_tab_helpers.py`
Expected: prints `ok`. Delete the scratch file after.

- [ ] **Step 3: pyflakes check**

Run: `.venv/Scripts/python.exe -m pyflakes backend/google_sheets_store.py`
Expected: no new warnings vs. baseline.

- [ ] **Step 4: Commit**

```bash
git add backend/google_sheets_store.py
git commit -m "Add tab-name and due-date-to-tab routing helpers for multi-tab sync"
```

---

### Task 3: Tab-scoped Sheets I/O helpers

**Files:**
- Modify: `backend/google_sheets_store.py:1-16` (add import)
- Modify: `backend/google_sheets_store.py:237-259` (insert new section after `delete_row`)

**Interfaces:**
- Consumes: `_a1_quote` (Task 2)
- Produces: `list_tabs(spreadsheet_id) -> list[{"name": str, "sheet_id": int}]`, `_sheet_id_for_tab(spreadsheet_id, sheet_name) -> int`, `ensure_tab_exists(spreadsheet_id, sheet_name)`, `read_tab_rows(spreadsheet_id, sheet_name) -> list`, `write_tab_row(spreadsheet_id, sheet_name, row_number, values)`, `append_tab_row(spreadsheet_id, sheet_name, values) -> int`, `write_tab_cell(spreadsheet_id, sheet_name, a1_cell, value)`, `delete_tab_row(spreadsheet_id, sheet_name, row_number)`, `read_all_synced_tabs(spreadsheet_id) -> dict[str, list]` — all consumed by Tasks 4-6.

- [ ] **Step 1: Add the `quote` import**

In `backend/google_sheets_store.py`, the import block currently reads:

```python
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
```

Change to:

```python
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import quote
```

- [ ] **Step 2: Insert the tab-scoped I/O helpers**

Right after the existing `delete_row` function (ends at line 258 with `r.raise_for_status()`) and before `def delete_task_from_sheet(link: dict, task_id: str) -> bool:`, insert:

```python
# ── Multi-tab I/O ────────────────────────────────────────────────────────
# Parallel to read_all_rows/write_row/append_row/write_cell/delete_row/
# _first_sheet_id above, which stay untouched and keep addressing an
# unqualified (first-tab) range for every already-linked single-tab client.
# These take an explicit sheet_name and are only ever called for a
# multi_tab=1 link.

def list_tabs(spreadsheet_id: str) -> list:
    """Every tab in the spreadsheet as [{"name": str, "sheet_id": int}, ...],
    in the spreadsheet's own tab order."""
    session = _get_session()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    r = session.get(url, params={"fields": "sheets.properties"})
    r.raise_for_status()
    sheets = r.json().get("sheets", [])
    return [{"name": s["properties"]["title"], "sheet_id": s["properties"]["sheetId"]} for s in sheets]


def _sheet_id_for_tab(spreadsheet_id: str, sheet_name: str) -> int:
    """The spreadsheet-internal numeric sheetId (gid) for one named tab --
    needed for a physical row delete via batchUpdate, which addresses rows
    by sheetId, not by the A1 range names the read/write helpers use. Falls
    back to the first tab's sheetId if sheet_name isn't found."""
    tabs = list_tabs(spreadsheet_id)
    for t in tabs:
        if t["name"] == sheet_name:
            return t["sheet_id"]
    return tabs[0]["sheet_id"] if tabs else 0


def ensure_tab_exists(spreadsheet_id: str, sheet_name: str):
    """Creates sheet_name as a new tab (with the standard header row) if it
    doesn't already exist. Idempotent -- safe to call on every push."""
    tabs = list_tabs(spreadsheet_id)
    if any(t["name"] == sheet_name for t in tabs):
        return
    session = _get_session()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate"
    body = {"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]}
    r = session.post(url, json=body)
    r.raise_for_status()
    write_tab_row(spreadsheet_id, sheet_name, 1, SHEET_HEADER_ROW)


def read_tab_rows(spreadsheet_id: str, sheet_name: str) -> list:
    """Every row (including the header) in one tab, as a list of lists.
    Short rows are NOT padded -- callers must not assume every row has all
    13 columns."""
    session = _get_session()
    rng = quote(f"{_a1_quote(sheet_name)}!A:M", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}"
    r = session.get(url)
    r.raise_for_status()
    return r.json().get("values", [])


def write_tab_row(spreadsheet_id: str, sheet_name: str, row_number: int, values: list):
    """Overwrites one full row in one tab, 1-indexed to match the tab's own
    row numbers."""
    session = _get_session()
    rng = quote(f"{_a1_quote(sheet_name)}!A{row_number}:M{row_number}", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}"
    r = session.put(url, params={"valueInputOption": "RAW"}, json={"values": [values]})
    r.raise_for_status()


def append_tab_row(spreadsheet_id: str, sheet_name: str, values: list) -> int:
    """Appends a new row after one tab's last row with data. Returns the new
    row's 1-indexed row number, parsed out of the API's updatedRange."""
    session = _get_session()
    rng = quote(f"{_a1_quote(sheet_name)}!A:M", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}:append"
    r = session.post(
        url,
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": [values]},
    )
    r.raise_for_status()
    updated_range = r.json().get("updates", {}).get("updatedRange", "")
    m = re.search(r"!A(\d+)", updated_range)
    return int(m.group(1)) if m else -1


def write_tab_cell(spreadsheet_id: str, sheet_name: str, a1_cell: str, value):
    session = _get_session()
    rng = quote(f"{_a1_quote(sheet_name)}!{a1_cell}", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}"
    r = session.put(url, params={"valueInputOption": "RAW"}, json={"values": [[value]]})
    r.raise_for_status()


def delete_tab_row(spreadsheet_id: str, sheet_name: str, row_number: int):
    """Physically removes one row from one tab (1-indexed), shifting
    everything below it up -- not a blank-out."""
    session = _get_session()
    sheet_id = _sheet_id_for_tab(spreadsheet_id, sheet_name)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate"
    body = {
        "requests": [{
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": row_number - 1,
                    "endIndex": row_number,
                }
            }
        }]
    }
    r = session.post(url, json=body)
    r.raise_for_status()


def read_all_synced_tabs(spreadsheet_id: str) -> dict:
    """One batchGet across every currently-synced tab (month-named or
    "Unscheduled"), so push/delete can locate a task_id without an
    N-tabs-worth of separate round trips. Returns {tab_name: rows}, rows
    being data rows only (header stripped, matching read_tab_rows' raw
    per-row shape minus row 1)."""
    tabs = [t["name"] for t in list_tabs(spreadsheet_id) if _is_synced_tab_name(t["name"])]
    if not tabs:
        return {}
    session = _get_session()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchGet"
    ranges = [f"{_a1_quote(name)}!A:M" for name in tabs]
    r = session.get(url, params={"ranges": ranges})
    r.raise_for_status()
    value_ranges = r.json().get("valueRanges", [])
    result = {}
    for name, vr in zip(tabs, value_ranges):
        rows = vr.get("values", [])
        result[name] = rows[1:] if rows else []
    return result
```

**Note:** `write_tab_row`/`SHEET_HEADER_ROW` reference inside `ensure_tab_exists` — `SHEET_HEADER_ROW` is defined later in the file (line ~327). Python resolves this fine at call time (not at module-load time), matching how the rest of this file already forward-references module-level names across functions.

- [ ] **Step 3: Verify with a scratch script (mocked HTTP)**

Write `.../scratchpad/test_tab_io.py`:

```python
import sys
from unittest.mock import patch, MagicMock
sys.path.insert(0, "backend")
import google_sheets_store as gs

fake_session = MagicMock()

def fake_get(url, params=None, **kw):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    if "fields" in (params or {}):
        resp.json.return_value = {"sheets": [
            {"properties": {"title": "August 2026", "sheetId": 111}},
            {"properties": {"title": "Unscheduled", "sheetId": 222}},
            {"properties": {"title": "Notes", "sheetId": 333}},
        ]}
    elif "ranges" in (params or {}):
        resp.json.return_value = {"valueRanges": [
            {"values": [["lumina_task_id"], ["t1", "2026-08-01"]]},
            {"values": [["lumina_task_id"], ["t2"]]},
        ]}
    else:
        resp.json.return_value = {"values": [["lumina_task_id"], ["t1"]]}
    return resp

fake_session.get.side_effect = fake_get

with patch.object(gs, "_get_session", return_value=fake_session):
    tabs = gs.list_tabs("SSID")
    assert [t["name"] for t in tabs] == ["August 2026", "Unscheduled", "Notes"], tabs
    assert gs._sheet_id_for_tab("SSID", "Unscheduled") == 222
    assert gs._sheet_id_for_tab("SSID", "Missing Tab") == 111  # falls back to first tab

    synced = gs.read_all_synced_tabs("SSID")
    assert set(synced.keys()) == {"August 2026", "Unscheduled"}, synced  # "Notes" excluded
    assert synced["August 2026"] == [["t1", "2026-08-01"]]  # header stripped
    assert synced["Unscheduled"] == [["t2"]]

print("ok")
```

Run: `.venv/Scripts/python.exe .../scratchpad/test_tab_io.py`
Expected: prints `ok`. Delete the scratch file after.

- [ ] **Step 4: pyflakes check**

Run: `.venv/Scripts/python.exe -m pyflakes backend/google_sheets_store.py`
Expected: no new warnings vs. baseline.

- [ ] **Step 5: Commit**

```bash
git add backend/google_sheets_store.py
git commit -m "Add tab-scoped Sheets I/O helpers for multi-tab sync"
```

---

### Task 4: Multi-tab push

**Files:**
- Modify: `backend/google_sheets_store.py:616-618` (insert after `_push_task_to_sheet_locked`, before `reconcile_sheet_rows`)

**Interfaces:**
- Consumes: `_get_client_sheet_lock`, `_is_tombstoned`, `is_configured`, `_month_tab_name_for`, `read_all_synced_tabs`, `ensure_tab_exists`, `append_tab_row`, `write_tab_row`, `delete_tab_row`, `_fields_to_row`, `_mark_pushed` (all existing or from Tasks 2-3)
- Produces: `push_task_to_sheet_multi_tab(link: dict, task_id: str, fields: dict) -> bool`, consumed by Task 7.

- [ ] **Step 1: Insert the function**

Right after `_push_task_to_sheet_locked` (ends at line 616 with `return False`) and before `def reconcile_sheet_rows(link: dict, rows: list) -> dict:`, insert:

```python
def push_task_to_sheet_multi_tab(link: dict, task_id: str, fields: dict) -> bool:
    """Multi-tab (multi_tab=1) equivalent of push_task_to_sheet -- routes by
    the task's due_date into the matching month tab (or "Unscheduled"),
    moving the row between tabs if its due_date's month changed since the
    last push. Same fire-and-forget contract and locking as
    push_task_to_sheet."""
    with _get_client_sheet_lock(link.get("client_id", "")):
        return _push_task_to_sheet_multi_tab_locked(link, task_id, fields)


def _push_task_to_sheet_multi_tab_locked(link: dict, task_id: str, fields: dict) -> bool:
    if _is_tombstoned(task_id):
        logger.info(f"Sheets push (multi-tab): skipping push for tombstoned task {task_id}.")
        return False
    if not is_configured():
        return False
    spreadsheet_id = link["spreadsheet_id"]
    target_tab = _month_tab_name_for(fields.get("due_date"))
    try:
        all_tabs = read_all_synced_tabs(spreadsheet_id)
    except Exception:
        logger.exception(f"Sheets push (multi-tab): failed to read tabs for {spreadsheet_id}")
        return False

    found_tab, row_number = None, None
    for tab_name, rows in all_tabs.items():
        for i, row in enumerate(rows):
            if row and str(row[0]).strip() == str(task_id):
                found_tab, row_number = tab_name, i + 2  # +1 header, +1 to 1-index
                break
        if found_tab:
            break

    values = _fields_to_row(task_id, fields)
    try:
        if found_tab == target_tab:
            write_tab_row(spreadsheet_id, target_tab, row_number, values)
        elif found_tab:
            # due_date moved this task into a different month since the
            # last push -- append to the new tab BEFORE deleting the old
            # row, so a failure mid-move leaves a harmless duplicate (self-
            # heals via reconcile_sheet_tabs' existing duplicate-id dedup)
            # rather than losing the row outright.
            ensure_tab_exists(spreadsheet_id, target_tab)
            append_tab_row(spreadsheet_id, target_tab, values)
            delete_tab_row(spreadsheet_id, found_tab, row_number)
        else:
            ensure_tab_exists(spreadsheet_id, target_tab)
            append_tab_row(spreadsheet_id, target_tab, values)
        _mark_pushed(task_id)
        return True
    except Exception:
        logger.exception(f"Sheets push (multi-tab): failed to write row for task {task_id}")
        return False
```

- [ ] **Step 2: Verify with a scratch script (mocked HTTP)**

Write `.../scratchpad/test_push_multi_tab.py`:

```python
import sys
from unittest.mock import patch, MagicMock
sys.path.insert(0, "backend")
import google_sheets_store as gs

link = {"client_id": "c1", "spreadsheet_id": "SSID"}

# Case 1: brand-new task, no due_date -> lands in "Unscheduled", tab auto-created.
with patch.object(gs, "is_configured", return_value=True), \
     patch.object(gs, "_is_tombstoned", return_value=False), \
     patch.object(gs, "read_all_synced_tabs", return_value={}), \
     patch.object(gs, "ensure_tab_exists") as m_ensure, \
     patch.object(gs, "append_tab_row") as m_append, \
     patch.object(gs, "write_tab_row") as m_write, \
     patch.object(gs, "delete_tab_row") as m_delete:
    ok = gs.push_task_to_sheet_multi_tab(link, "t1", {"due_date": "", "title": "x"})
    assert ok is True
    m_ensure.assert_called_once_with("SSID", "Unscheduled")
    m_append.assert_called_once()
    m_write.assert_not_called()
    m_delete.assert_not_called()

# Case 2: task already in "July 2026", due_date now "2026-08-01" -> moves tab.
with patch.object(gs, "is_configured", return_value=True), \
     patch.object(gs, "_is_tombstoned", return_value=False), \
     patch.object(gs, "read_all_synced_tabs", return_value={"July 2026": [["t2", "2026-07-01"]]}), \
     patch.object(gs, "ensure_tab_exists") as m_ensure, \
     patch.object(gs, "append_tab_row") as m_append, \
     patch.object(gs, "write_tab_row") as m_write, \
     patch.object(gs, "delete_tab_row") as m_delete:
    ok = gs.push_task_to_sheet_multi_tab(link, "t2", {"due_date": "2026-08-01", "title": "x"})
    assert ok is True
    m_ensure.assert_called_once_with("SSID", "August 2026")
    m_append.assert_called_once()
    args, _ = m_append.call_args
    assert args[0] == "SSID" and args[1] == "August 2026"
    m_delete.assert_called_once_with("SSID", "July 2026", 2)
    m_write.assert_not_called()

# Case 3: tombstoned task -> push is a no-op.
with patch.object(gs, "_is_tombstoned", return_value=True):
    ok = gs.push_task_to_sheet_multi_tab(link, "t3", {"due_date": "2026-08-01"})
    assert ok is False

print("ok")
```

Run: `.venv/Scripts/python.exe .../scratchpad/test_push_multi_tab.py`
Expected: prints `ok`. Delete the scratch file after.

- [ ] **Step 3: pyflakes check**

Run: `.venv/Scripts/python.exe -m pyflakes backend/google_sheets_store.py`
Expected: no new warnings vs. baseline.

- [ ] **Step 4: Commit**

```bash
git add backend/google_sheets_store.py
git commit -m "Add multi-tab push (routes tasks into month tabs by due_date)"
```

---

### Task 5: Multi-tab delete

**Files:**
- Modify: `backend/google_sheets_store.py:318-320` (insert after `_delete_task_from_sheet_locked`, before the "Field mapping" section)

**Interfaces:**
- Consumes: `_get_client_sheet_lock`, `_tombstone_task`, `is_configured`, `read_all_synced_tabs`, `delete_tab_row`, `_retry` (existing or from Task 3)
- Produces: `delete_task_from_sheet_multi_tab(link: dict, task_id: str) -> bool`, consumed by Task 7.

- [ ] **Step 1: Insert the function**

Right after `_delete_task_from_sheet_locked` (ends at line 318 with `return ok`) and before the `# ── Field mapping ──` comment, insert:

```python
def delete_task_from_sheet_multi_tab(link: dict, task_id: str) -> bool:
    """Multi-tab (multi_tab=1) equivalent of delete_task_from_sheet -- the
    task's row could be in any month tab now, not just "the" tab, so this
    searches all synced tabs before removing it. Same tombstone/retry
    contract as delete_task_from_sheet."""
    with _get_client_sheet_lock(link.get("client_id", "")):
        return _delete_task_from_sheet_multi_tab_locked(link, task_id)


def _delete_task_from_sheet_multi_tab_locked(link: dict, task_id: str) -> bool:
    _tombstone_task(task_id, link.get("client_id", ""))
    if not is_configured():
        return False
    spreadsheet_id = link["spreadsheet_id"]
    try:
        all_tabs = read_all_synced_tabs(spreadsheet_id)
    except Exception:
        logger.exception(f"Sheets delete (multi-tab): failed to read tabs for {spreadsheet_id}")
        return False

    found_tab, row_number = None, None
    for tab_name, rows in all_tabs.items():
        for i, row in enumerate(rows):
            if row and str(row[0]).strip() == str(task_id):
                found_tab, row_number = tab_name, i + 2
                break
        if found_tab:
            break
    if not found_tab:
        return False  # already gone, or never made it there

    ok = _retry(lambda: delete_tab_row(spreadsheet_id, found_tab, row_number))
    if not ok:
        logger.error(f"Sheets delete (multi-tab): giving up removing row for task {task_id} after retries.")
    return ok
```

- [ ] **Step 2: Verify with a scratch script (mocked HTTP)**

Write `.../scratchpad/test_delete_multi_tab.py`:

```python
import sys
from unittest.mock import patch, MagicMock
sys.path.insert(0, "backend")
import google_sheets_store as gs

link = {"client_id": "c1", "spreadsheet_id": "SSID"}

with patch.object(gs, "_tombstone_task") as m_tomb, \
     patch.object(gs, "is_configured", return_value=True), \
     patch.object(gs, "read_all_synced_tabs", return_value={
         "August 2026": [["t1", "2026-08-01"]],
         "Unscheduled": [["t2"]],
     }), \
     patch.object(gs, "delete_tab_row") as m_delete:
    ok = gs.delete_task_from_sheet_multi_tab(link, "t2")
    assert ok is True
    m_tomb.assert_called_once_with("t2", "c1")
    m_delete.assert_called_once_with("SSID", "Unscheduled", 2)

with patch.object(gs, "_tombstone_task"), \
     patch.object(gs, "is_configured", return_value=True), \
     patch.object(gs, "read_all_synced_tabs", return_value={"August 2026": [["t1"]]}), \
     patch.object(gs, "delete_tab_row") as m_delete:
    ok = gs.delete_task_from_sheet_multi_tab(link, "not-there")
    assert ok is False
    m_delete.assert_not_called()

print("ok")
```

Run: `.venv/Scripts/python.exe .../scratchpad/test_delete_multi_tab.py`
Expected: prints `ok`. Delete the scratch file after.

- [ ] **Step 3: pyflakes check**

Run: `.venv/Scripts/python.exe -m pyflakes backend/google_sheets_store.py`
Expected: no new warnings vs. baseline.

- [ ] **Step 4: Commit**

```bash
git add backend/google_sheets_store.py
git commit -m "Add multi-tab delete (searches all month tabs for the task's row)"
```

---

### Task 6: Multi-tab reconcile

**Files:**
- Modify: `backend/google_sheets_store.py` (append after the existing `_reconcile_sheet_rows_locked`, end of file, currently ending at line 805)

**Interfaces:**
- Consumes: `_get_client_sheet_lock`, `_current_tasks_by_id`, `_row_to_fields`, `_create_task`, `_is_tombstoned`, `_task_to_fields`, `_recently_pushed`, `_update_task`, `_delete_task`, `_log_version`, `_retry`, `write_tab_cell`, `_is_synced_tab_name` (existing or Tasks 2-3)
- Produces: `reconcile_sheet_tabs(link: dict, tabs: dict) -> dict`, consumed by Task 7. Return shape matches `reconcile_sheet_rows`'s dict exactly: `{created, updated, deleted, skipped, errored, duplicates, recreated, tombstoned, skipped_recent_push}` (plus optional `deletes_skipped_safety`).

- [ ] **Step 1: Append the function**

At the end of `backend/google_sheets_store.py` (after the final `return {"created": created, ...}` of `_reconcile_sheet_rows_locked`), append:

```python


def reconcile_sheet_tabs(link: dict, tabs: dict) -> dict:
    """Multi-tab (multi_tab=1) equivalent of reconcile_sheet_rows -- tabs is
    {tab_name: rows}, one entry per synced tab in the incoming Apps Script
    payload. Same full-snapshot-diff contract, unioned across every tab
    instead of one flat list: a task's physical tab is just a container,
    the Post Day cell value in its row is still what sets the task's
    due_date (same as the single-tab behavior) -- this function does not
    enforce or correct tab/due_date consistency, only push does that."""
    with _get_client_sheet_lock(link["client_id"]):
        return _reconcile_sheet_tabs_locked(link, tabs)


def _reconcile_sheet_tabs_locked(link: dict, tabs: dict) -> dict:
    client_id = link["client_id"]
    is_notion = link["is_notion"]
    client_name = link.get("client_name") or ""
    spreadsheet_id = link["spreadsheet_id"]
    editor_name = link.get("linked_by") or "Google Sheets"

    current = _current_tasks_by_id(client_id, is_notion)
    seen_ids = set()
    created, updated, deleted, skipped, errored, duplicates, recreated, tombstoned, skipped_recent_push = \
        0, 0, 0, 0, 0, 0, 0, 0, 0

    for tab_name, rows in tabs.items():
        if not _is_synced_tab_name(tab_name):
            continue  # defensive -- Apps Script already filters, a hand-edited payload might not
        for idx, row in enumerate(rows):
            row_number = idx + 2  # +1 for 0-index, +1 for the header row Apps Script stripped
            row_label = f"{tab_name} row {row_number}"
            try:
                task_id = str(row[0]).strip() if row else ""
                row_fields = _row_to_fields(row)
                if not task_id and not any(row_fields.values()):
                    continue  # fully blank row

                if not task_id:
                    new_id = _create_task(client_id, client_name, is_notion, row_fields)
                    if new_id:
                        ok = _retry(lambda: write_tab_cell(spreadsheet_id, tab_name, f"A{row_number}", new_id))
                        if not ok:
                            logger.error(
                                f"Sheets reconcile (multi-tab): giving up writing back task id {new_id} "
                                f"for {row_label} (client {client_id}) after retries."
                            )
                        _log_version(new_id, client_id, f"{editor_name} (via Google Sheets)", row_fields,
                                     changed_fields=list(row_fields.keys()))
                        created += 1
                    continue

                if task_id in seen_ids:
                    logger.warning(
                        f"Sheets reconcile (multi-tab): duplicate task_id {task_id} at {row_label} for "
                        f"client {client_id} -- ignoring, an earlier row already claimed this id this pass."
                    )
                    duplicates += 1
                    continue
                seen_ids.add(task_id)

                existing = current.get(task_id)
                if not existing:
                    if _is_tombstoned(task_id):
                        logger.info(
                            f"Sheets reconcile (multi-tab): {row_label} (client {client_id}) references "
                            f"tombstoned task_id {task_id} -- not recreating."
                        )
                        tombstoned += 1
                        continue
                    new_id = _create_task(client_id, client_name, is_notion, row_fields)
                    if new_id:
                        logger.warning(
                            f"Sheets reconcile (multi-tab): {row_label} (client {client_id}) referenced "
                            f"task_id {task_id}, which no longer exists in Lumina -- recreated as {new_id}."
                        )
                        ok = _retry(lambda: write_tab_cell(spreadsheet_id, tab_name, f"A{row_number}", new_id))
                        if not ok:
                            logger.error(
                                f"Sheets reconcile (multi-tab): giving up writing back recreated task id "
                                f"{new_id} for {row_label} (client {client_id}) after retries."
                            )
                        _log_version(new_id, client_id, f"{editor_name} (via Google Sheets)", row_fields,
                                     changed_fields=list(row_fields.keys()))
                        recreated += 1
                    continue

                old_fields = _task_to_fields(existing)
                if old_fields == row_fields:
                    skipped += 1
                    continue
                if _recently_pushed(task_id):
                    logger.info(
                        f"Sheets reconcile (multi-tab): {row_label} (client {client_id}, task_id {task_id}) "
                        f"was pushed to recently -- deferring this update to the next pass."
                    )
                    skipped_recent_push += 1
                    continue
                _update_task(task_id, is_notion, row_fields, editor_name)
                changed = [k for k in row_fields if row_fields.get(k) != old_fields.get(k)]
                _log_version(task_id, client_id, f"{editor_name} (via Google Sheets)", row_fields,
                             changed_fields=changed)
                updated += 1
            except Exception:
                logger.exception(f"Sheets reconcile (multi-tab): skipping malformed row at {row_label} for client {client_id}")
                errored += 1

    if not seen_ids and current:
        logger.warning(
            f"Sheets reconcile (multi-tab): snapshot for client {client_id} recognized 0 of "
            f"{len(current)} known tasks -- skipping delete pass as a safety measure."
        )
        return {"created": created, "updated": updated, "deleted": 0, "skipped": skipped,
                "errored": errored, "duplicates": duplicates, "recreated": recreated,
                "tombstoned": tombstoned, "skipped_recent_push": skipped_recent_push,
                "deletes_skipped_safety": len(current)}

    for existing_id in current:
        if existing_id not in seen_ids:
            _delete_task(existing_id, is_notion)
            deleted += 1

    return {"created": created, "updated": updated, "deleted": deleted, "skipped": skipped,
            "errored": errored, "duplicates": duplicates, "recreated": recreated,
            "tombstoned": tombstoned, "skipped_recent_push": skipped_recent_push}
```

- [ ] **Step 2: Verify with a scratch script (mocked task adapters)**

Write `.../scratchpad/test_reconcile_multi_tab.py`:

```python
import sys
from unittest.mock import patch, MagicMock
sys.path.insert(0, "backend")
import google_sheets_store as gs

link = {"client_id": "c1", "is_notion": False, "client_name": "Acme", "spreadsheet_id": "SSID", "linked_by": "emp001"}

# Union-across-tabs: one update in "August 2026", one new row in "Unscheduled",
# a task present in neither tab gets deleted, a row in an unrecognized tab
# name is ignored entirely.
current = {
    "t1": {"notion_id": "t1", "title": "[Post] Old", "description": "", "assigned_to": "", "due_date": "2026-08-01", "status": "not_started", "creation_date": ""},
    "t2": {"notion_id": "t2", "title": "[Post] Gone", "description": "", "assigned_to": "", "due_date": "2026-07-01", "status": "not_started", "creation_date": ""},
}
tabs = {
    "August 2026": [["t1", "2026-08-01", "", "New Title", "Post", "", "", "", "", "", "", "", "not_started"]],
    "Unscheduled": [["", "", "", "Brand New", "Post", "", "", "", "", "", "", "", "not_started"]],
    "Random Notes": [["should", "never", "be", "read"]],
}

with patch.object(gs, "_current_tasks_by_id", return_value=current), \
     patch.object(gs, "_create_task", return_value="t3") as m_create, \
     patch.object(gs, "_update_task", return_value=True) as m_update, \
     patch.object(gs, "_delete_task", return_value=True) as m_delete, \
     patch.object(gs, "_log_version"), \
     patch.object(gs, "_is_tombstoned", return_value=False), \
     patch.object(gs, "_recently_pushed", return_value=False), \
     patch.object(gs, "write_tab_cell"):
    summary = gs.reconcile_sheet_tabs(link, tabs)

assert summary["updated"] == 1, summary
assert summary["created"] == 1, summary
assert summary["deleted"] == 1, summary
m_update.assert_called_once()
assert m_update.call_args[0][0] == "t1"
m_create.assert_called_once()
m_delete.assert_called_once_with("t2", False)

print("ok")
```

Run: `.venv/Scripts/python.exe .../scratchpad/test_reconcile_multi_tab.py`
Expected: prints `ok`. Delete the scratch file after.

- [ ] **Step 3: pyflakes check**

Run: `.venv/Scripts/python.exe -m pyflakes backend/google_sheets_store.py`
Expected: no new warnings vs. baseline.

- [ ] **Step 4: Commit**

```bash
git add backend/google_sheets_store.py
git commit -m "Add multi-tab reconcile (unions rows across every synced tab)"
```

---

### Task 7: Wire it up in `routes/sheets_sync.py`

**Files:**
- Modify: `backend/routes/sheets_sync.py` (multiple locations, listed per step)

**Interfaces:**
- Consumes: everything from Tasks 2-6 (`gs.push_task_to_sheet_multi_tab`, `gs.delete_task_from_sheet_multi_tab`, `gs.reconcile_sheet_tabs`, `gs.list_tabs`)
- Produces: `multi_tab` present in `get_link_for_client()`/`get_link_by_token()`'s returned dicts (consumed nowhere outside this file, but required for the dispatch logic below to work).

- [ ] **Step 1: Connectivity check no longer depends on tab structure**

Replace:

```python
    try:
        gs.read_all_rows(spreadsheet_id)
    except Exception:
        return jsonify({
            "error": f"Could not read that sheet -- make sure it's shared (Editor access) with {gs.service_account_email()}"
        }), 400
```

with:

```python
    try:
        gs.list_tabs(spreadsheet_id)
    except Exception:
        return jsonify({
            "error": f"Could not read that sheet -- make sure it's shared (Editor access) with {gs.service_account_email()}"
        }), 400
```

(`list_tabs` validates sharing/read access the same way `read_all_rows` did, without assuming anything about which tab exists or is named what -- works identically for both legacy and multi-tab clients.)

- [ ] **Step 2: `multi_tab` default and column read/write in `create_google_sheet_link`**

Replace:

```python
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
```

with:

```python
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
```

- [ ] **Step 3: Backfill dispatch + response's Apps Script snippet**

Replace:

```python
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
```

with:

```python
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

    webhook_url = f"{_base_url()}/api/sheets/webhook/{link_token}"
    return jsonify({
        "success": True, "spreadsheet_id": spreadsheet_id,
        "service_account_email": gs.service_account_email(),
        "apps_script": _apps_script_snippet(webhook_url, bool(multi_tab)),
        "multi_tab": bool(multi_tab),
        "backfilled": backfilled,
    })
```

- [ ] **Step 4: `_apps_script_snippet` gains a `multi_tab` param**

Replace the whole function:

```python
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
```

with:

```python
def _apps_script_snippet(webhook_url: str, multi_tab: bool = False) -> str:
    if not multi_tab:
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
        "      var data = sheet.getDataRange().getValues();\n"
        "      tabs[name] = data.slice(1); // drop header row\n"
        "    }\n"
        "  });\n"
        "  UrlFetchApp.fetch(\"" + webhook_url + "\", {\n"
        "    method: \"post\",\n"
        "    contentType: \"application/json\",\n"
        "    payload: JSON.stringify({ tabs: tabs }),\n"
        "    muteHttpExceptions: true\n"
        "  });\n"
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
```

- [ ] **Step 5: `_link_row_to_dict` and `get_google_sheet_link`'s SELECT gain `multi_tab`**

Replace:

```python
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
```

with:

```python
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
```

And replace the `get_google_sheet_link` SELECT:

```python
    cur.execute(
        "SELECT client_id, spreadsheet_id, link_token, is_notion, client_name, linked_at, linked_by, "
        "last_push_at, last_push_ok, last_pull_at, last_pull_summary "
        "FROM google_sheet_links WHERE client_id=?", (client_id,)
    )
```

with:

```python
    cur.execute(
        "SELECT client_id, spreadsheet_id, link_token, is_notion, client_name, linked_at, linked_by, "
        "last_push_at, last_push_ok, last_pull_at, last_pull_summary, multi_tab "
        "FROM google_sheet_links WHERE client_id=?", (client_id,)
    )
```

- [ ] **Step 6: `get_link_for_client`/`get_link_by_token` gain `multi_tab`**

Replace:

```python
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
```

with:

```python
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
```

- [ ] **Step 7: Push and delete routes dispatch by `link["multi_tab"]`**

Replace:

```python
    link = get_link_for_client(client_id)
    if not link:
        return jsonify({"success": True, "linked": False})
    ok = gs.push_task_to_sheet(link, task_id, fields)
    _record_push_result(client_id, ok)
    return jsonify({"success": True, "linked": True, "pushed": ok})
```

with:

```python
    link = get_link_for_client(client_id)
    if not link:
        return jsonify({"success": True, "linked": False})
    push_fn = gs.push_task_to_sheet_multi_tab if link.get("multi_tab") else gs.push_task_to_sheet
    ok = push_fn(link, task_id, fields)
    _record_push_result(client_id, ok)
    return jsonify({"success": True, "linked": True, "pushed": ok})
```

Replace:

```python
    link = get_link_for_client(client_id)
    if not link:
        return jsonify({"success": True, "linked": False})
    deleted = gs.delete_task_from_sheet(link, task_id)
    return jsonify({"success": True, "linked": True, "deleted": deleted})
```

with:

```python
    link = get_link_for_client(client_id)
    if not link:
        return jsonify({"success": True, "linked": False})
    delete_fn = gs.delete_task_from_sheet_multi_tab if link.get("multi_tab") else gs.delete_task_from_sheet
    deleted = delete_fn(link, task_id)
    return jsonify({"success": True, "linked": True, "deleted": deleted})
```

- [ ] **Step 8: Webhook route accepts both payload shapes**

Replace:

```python
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
```

with:

```python
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
        tabs = body["tabs"]
        total_rows = sum(len(v) for v in tabs.values() if isinstance(v, list))
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
```

- [ ] **Step 9: Verify**

Run: `.venv/Scripts/python.exe -m pyflakes backend/routes/sheets_sync.py backend/google_sheets_store.py`
Expected: no new warnings vs. baseline.

Run: `.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'backend'); import app; print('ok')"`
Expected: prints `ok` (Flask app boots clean with the new blueprint code, no import errors).

- [ ] **Step 10: Live curl smoke test against local dev server**

Start the dev server (`.venv/Scripts/python.exe -m backend.app` in the background), then:

```bash
curl -s -X POST http://localhost:5000/api/sheets/webhook/nonexistent-token -H "Content-Type: application/json" -d '{"tabs":{}}'
```

Expected: `{"error":"Unknown link"}` with a 404 -- confirms the route parses the new payload shape without raising before even reaching the "unknown token" check (i.e. the `isinstance(body.get("tabs"), dict)` branch doesn't blow up on a link-less request). Stop the dev server after.

- [ ] **Step 11: Commit**

```bash
git add backend/routes/sheets_sync.py
git commit -m "Wire multi-tab sync into routes: connect, push, delete, webhook"
```

---

### Task 8: Frontend — month tab strip replacing the dropdown

**Files:**
- Modify: `frontend/projects.html:203-207` (CSS)
- Modify: `frontend/projects.html:1602-1611` (filter logic)
- Modify: `frontend/projects.html:1661-1665` (toolbar markup)

**Interfaces:**
- Consumes: `loadMonthFilter`/`saveMonthFilter` (existing, unchanged), `monthLabel` (existing, unchanged), `records`/`allMonths` (existing, computed in `renderClientSheets`)
- Produces: no new exported functions -- purely a display/interaction change inside `renderClientSheets`.

- [ ] **Step 1: Add tab-strip CSS**

The existing `.ctab`/`.ctab.on` rules (used for the Task List/Kanban/Blockers/Calendar/Sheets top-level tabs) are the right visual language, but `switchClientTab()` toggles `.on` on **every** `.ctab` element inside a client card body -- reusing that exact class for month tabs would make switching the outer List/Kanban/Sheets tab incorrectly strip `.on` from whichever month tab was active. Use a distinct class instead.

Right after the existing block:

```css
    .ctab-content{min-height:60px}
```

add:

```css
    .smtab{padding:5px 15px;border-radius:100px;font-size:.77rem;font-weight:600;border:1px solid var(--bdr);background:transparent;color:var(--muted);cursor:pointer;transition:all .2s;white-space:nowrap}
    .smtab:hover{border-color:var(--acc);color:var(--acc);background:rgba(245,166,35,.07)}
    .smtab.on{background:rgba(245,166,35,.15);border-color:var(--acc);color:var(--acc)}
```

- [ ] **Step 2: Add the "Unscheduled" bucket to the filter logic**

Replace:

```javascript
  // Distinct Post-Day months present, computed before any filter so the
  // dropdown always lists every month that has data, not just visible rows.
  const allMonths = Array.from(new Set(records.map(r => (r.due_date || '').slice(0, 7)).filter(Boolean))).sort();

  const hiddenRowIds = loadHiddenRows(clientId);
  const hiddenRecords = records.filter(r => hiddenRowIds.has(String(r.t.id)));
  records = records.filter(r => !hiddenRowIds.has(String(r.t.id)));

  const monthFilter = loadMonthFilter(clientId);
  if (monthFilter) records = records.filter(r => (r.due_date || '').slice(0, 7) === monthFilter);
```

with:

```javascript
  // Distinct Post-Day months present, computed before any filter so the
  // tab strip always lists every month that has data, not just visible rows.
  const allMonths = Array.from(new Set(records.map(r => (r.due_date || '').slice(0, 7)).filter(Boolean))).sort();
  const hasUnscheduled = records.some(r => !r.due_date);

  const hiddenRowIds = loadHiddenRows(clientId);
  const hiddenRecords = records.filter(r => hiddenRowIds.has(String(r.t.id)));
  records = records.filter(r => !hiddenRowIds.has(String(r.t.id)));

  const monthFilter = loadMonthFilter(clientId);
  if (monthFilter === '__unscheduled__') {
    records = records.filter(r => !r.due_date);
  } else if (monthFilter) {
    records = records.filter(r => (r.due_date || '').slice(0, 7) === monthFilter);
  }
```

- [ ] **Step 3: Replace the `<select>` with the tab strip**

Replace:

```html
          <select id="sheet-month-${clientId}" onchange="saveMonthFilter('${clientId}', this.value)"
            style="background:var(--s2); border:1px solid var(--bdr); border-radius:6px; padding:8px 10px; color:var(--txt); font-size:.85rem; margin-left:8px;">
            <option value="">All Months</option>
            ${allMonths.map(ym => `<option value="${ym}" ${monthFilter===ym?'selected':''}>${monthLabel(ym)}</option>`).join('')}
          </select>
```

with:

```html
          <div id="sheet-month-tabs-${clientId}" style="display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin-left:8px;">
            <button class="smtab ${monthFilter===''?'on':''}" onclick="saveMonthFilter('${clientId}', ''); renderClientSheets('${clientId}')">All</button>
            ${allMonths.map(ym => `<button class="smtab ${monthFilter===ym?'on':''}" onclick="saveMonthFilter('${clientId}', '${ym}'); renderClientSheets('${clientId}')">${monthLabel(ym)}</button>`).join('')}
            ${hasUnscheduled ? `<button class="smtab ${monthFilter==='__unscheduled__'?'on':''}" onclick="saveMonthFilter('${clientId}', '__unscheduled__'); renderClientSheets('${clientId}')">Unscheduled</button>` : ''}
          </div>
```

(This also fixes a pre-existing bug in the same code being touched: the old `<select>`'s `onchange` called `saveMonthFilter` but never re-rendered, so picking a month silently did nothing until some unrelated action happened to re-render the table. The new buttons call `renderClientSheets` immediately after saving, same pattern every other Sheets toolbar control already uses.)

- [ ] **Step 4: `node --check` the edited script block**

Extract the `<script>` block containing `renderClientSheets` (or run `node --check` against the whole file if the project's existing verification already does that -- see CLAUDE.md's established pattern for this file) and confirm no syntax errors.

Run: `node --check frontend/projects.html` (if this fails because the file isn't pure JS -- it's HTML with embedded `<script>` -- extract just the script block(s) to a temp `.js` file first, matching how this was verified in every prior round documented in CLAUDE.md gotcha #87, then run `node --check` on that temp file. Delete the temp file after.)
Expected: no syntax errors.

- [ ] **Step 5: Manual verification in a browser**

Start the dev server, open `projects.html`, expand a social-media client's Sheets tab, confirm: the month tab strip renders instead of a dropdown, clicking "All" / a month / "Unscheduled" (if any unscheduled rows exist) actually filters the visible rows immediately (no extra click/refresh needed), and the active tab is visually highlighted. If chrome-devtools MCP is available in this session, use it for this check; otherwise do it manually and report what was and wasn't verified (per this codebase's established practice of being explicit about what live-browser testing did or didn't happen -- see CLAUDE.md gotcha #87's verification notes throughout).

- [ ] **Step 6: Commit**

```bash
git add frontend/projects.html
git commit -m "Replace Sheets month filter dropdown with a month tab strip"
```

---

### Task 9: Frontend — Connect modal copy

**Files:**
- Modify: `frontend/projects.html:3970-3986` (Connect Google Sheet modal body)

**Interfaces:**
- Consumes: `d.multi_tab` (new field returned by `GET /api/clients/<id>/google-sheet-link`, added in Task 7 Step 5)

- [ ] **Step 1: Show sync mode + naming convention in the "linked" state**

Replace:

```javascript
          body.innerHTML = `
            <p><strong>Linked</strong> to spreadsheet <code>${esc(d.spreadsheet_id)}</code></p>
            <p>Linked by ${esc(d.linked_by || 'someone')} on ${esc(d.linked_at || '')}</p>
            ${pushLine}
            ${pullLine}
            <p>Share the Sheet with: <code>${esc(d.service_account_email)}</code></p>
            <p>Apps Script (paste into the Sheet's Apps Script editor, then run <code>installTrigger</code> once):</p>
            <textarea readonly style="width:100%;height:180px;font-family:monospace;font-size:0.8rem;">${esc(d.apps_script)}</textarea>
            <div style="margin-top:12px;"><button class="btn btn-sm" onclick="unlinkGoogleSheet('${clientId}')" style="border: 1px solid var(--bdr); background: var(--s2); color: var(--txt); padding: 8px 12px;">Unlink</button></div>
          `;
```

with:

```javascript
          const modeLine = d.multi_tab
            ? `<p>Sync mode: <strong>one tab per month</strong>. Name each month's tab exactly like <code>"August 2026"</code>; tasks with no due date sync to a tab named exactly <code>"Unscheduled"</code>. Any other tab name is ignored.</p>`
            : `<p>Sync mode: <strong>single tab</strong> (this Sheet's first tab only).</p>`;
          body.innerHTML = `
            <p><strong>Linked</strong> to spreadsheet <code>${esc(d.spreadsheet_id)}</code></p>
            <p>Linked by ${esc(d.linked_by || 'someone')} on ${esc(d.linked_at || '')}</p>
            ${modeLine}
            ${pushLine}
            ${pullLine}
            <p>Share the Sheet with: <code>${esc(d.service_account_email)}</code></p>
            <p>Apps Script (paste into the Sheet's Apps Script editor, then run <code>installTrigger</code> once):</p>
            <textarea readonly style="width:100%;height:180px;font-family:monospace;font-size:0.8rem;">${esc(d.apps_script)}</textarea>
            <div style="margin-top:12px;"><button class="btn btn-sm" onclick="unlinkGoogleSheet('${clientId}')" style="border: 1px solid var(--bdr); background: var(--s2); color: var(--txt); padding: 8px 12px;">Unlink</button></div>
          `;
```

- [ ] **Step 2: Mention the convention before connecting**

Replace:

```javascript
          body.innerHTML = `
            <p>Paste the URL of an existing Google Sheet to link it to this client. The Sheet needs a hidden column A (lumina_task_id) and 12 columns after it matching the Sheets tab's fields (Creation Date, Post Day, Post Title, Type, Content, Idea, Scripts/Copy, Caption, File (Drive Link), Notes, Assigned To, Status). Format the Creation Date and Post Day columns as Plain Text so typed YYYY-MM-DD dates round-trip correctly.</p>
            <input type="text" id="gsheet-url-input" placeholder="https://docs.google.com/spreadsheets/d/..." style="width:100%;padding:8px;margin-bottom:8px;">
            <button class="btn btn-sm" onclick="submitGoogleSheetLink('${clientId}')" style="border: 1px solid var(--bdr); background: var(--acc); color: #000; padding: 8px 16px; font-weight:600;">Connect</button>
          `;
```

with:

```javascript
          body.innerHTML = `
            <p>Paste the URL of an existing Google Sheet to link it to this client. The Sheet needs a hidden column A (lumina_task_id) and 12 columns after it matching the Sheets tab's fields (Creation Date, Post Day, Post Title, Type, Content, Idea, Scripts/Copy, Caption, File (Drive Link), Notes, Assigned To, Status). Format the Creation Date and Post Day columns as Plain Text so typed YYYY-MM-DD dates round-trip correctly.</p>
            <p>New connections sync one tab per due-date month. Name each month's tab exactly like <code>"August 2026"</code>; tasks with no due date go in a tab named exactly <code>"Unscheduled"</code>. Any other tab name is ignored.</p>
            <input type="text" id="gsheet-url-input" placeholder="https://docs.google.com/spreadsheets/d/..." style="width:100%;padding:8px;margin-bottom:8px;">
            <button class="btn btn-sm" onclick="submitGoogleSheetLink('${clientId}')" style="border: 1px solid var(--bdr); background: var(--acc); color: #000; padding: 8px 16px; font-weight:600;">Connect</button>
          `;
```

- [ ] **Step 3: `node --check` and commit**

Run the same script-block syntax check as Task 8 Step 4.

```bash
git add frontend/projects.html
git commit -m "Explain monthly-tab naming convention in the Connect Google Sheet modal"
```

---

### Task 10: Full verification sweep, CLAUDE.md update, deploy

**Files:**
- Modify: `CLAUDE.md` (append to gotcha #87)

- [ ] **Step 1: Full pyflakes sweep**

Run: `.venv/Scripts/python.exe -m pyflakes backend/*.py backend/routes/*.py`
Expected: no new warnings vs. the pre-existing baseline noted throughout CLAUDE.md gotcha #87.

- [ ] **Step 2: App boot check**

Run: `.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'backend'); import app; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Re-run every scratch test from Tasks 2-6 together, once, as a final regression pass**

(They were already deleted individually after each task -- recreate and run them one more time in one batch before deploying, then delete again. This catches any interaction between later tasks' edits and earlier tasks' functions that isolated single-task testing wouldn't.)

- [ ] **Step 4: Append to CLAUDE.md gotcha #87**

Add a new sub-bullet after the existing "Fourth follow-up" bullet, documenting: the `multi_tab` flag and what it gates, the new `*_multi_tab`/`reconcile_sheet_tabs` functions and that they're fully parallel to (not branches inside) the existing single-tab functions, the tab-naming convention, the auto-create-on-push behavior, that MMGA stays on `multi_tab=0` until manually reconnected, the month-tab-strip frontend change (and the dropdown-never-re-rendered bug fixed as a side effect), and what has/hasn't been live-verified (same "credential/live-client blocked" caveat pattern used throughout the rest of this gotcha, unless Task 8 Step 5's live browser check actually happened, in which case record what was verified instead).

- [ ] **Step 5: Commit the CLAUDE.md update**

```bash
git add CLAUDE.md
git commit -m "Document multi-tab Google Sheets sync in gotcha #87"
```

- [ ] **Step 6: Push and deploy**

```bash
git push
```

Then wait for the Railway deploy and confirm health:

```bash
until curl -s https://lumina.mmga.agency/api/health | grep -q '"status":"ok"'; do sleep 5; done
sleep 15
curl -s https://lumina.mmga.agency/api/health
```

Expected: final curl shows `{"status":"ok",...}`.
