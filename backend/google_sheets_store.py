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
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import notion_store
from utils import today_ist, now_ist, IST

logger = logging.getLogger(__name__)

# Per-client lock guarding every Sheet-touching operation -- push, delete,
# and reconcile alike. Deployment is a single gunicorn worker (gevent, see
# Procfile) -- gunicorn's gevent worker monkey-patches stdlib threading on
# startup, so a plain threading.Lock here is genuinely gevent-cooperative.
# Started out scoped to reconcile only (two webhook requests for the same
# client landing close together, e.g. Apps Script firing onChange more than
# once for one paste, could both read the same stale snapshot and both
# create a task for the same blank row) but a Lumina-side save/delete for
# that same client can just as easily land mid-reconcile: push_task_to_sheet
# might read/write a row the same instant reconcile is diffing the sheet
# against Notion/SQLite, or delete_task_from_sheet's tombstone write could
# race a reconcile pass that read the sheet just before it. Widening this
# to cover all three closes that whole class at once instead of only the
# one instance already observed. Scoped per-client (not global) since
# different clients' sync operations touch disjoint data and shouldn't
# block each other.
_client_sheet_locks: dict = {}
_client_sheet_locks_guard = threading.Lock()


def _get_client_sheet_lock(client_id: str) -> threading.Lock:
    with _client_sheet_locks_guard:
        lock = _client_sheet_locks.get(client_id)
        if lock is None:
            lock = threading.Lock()
            _client_sheet_locks[client_id] = lock
        return lock


# The per-client lock above makes push/delete/reconcile take turns instead
# of literally overlapping, but that alone doesn't stop a genuine field-level
# conflict: Apps Script reads the live sheet at trigger time, OUTSIDE any
# lock we control, then POSTs it. If a push writes a field's new value into
# the Sheet in the gap between that read and this webhook's payload actually
# arriving and acquiring the lock, the payload reconcile ends up processing
# still holds the OLD value for that field -- and since updates are applied
# as a whole-row overwrite (deliberately, see reconcile's own docstring on
# why this codebase avoids diff-only payloads), the stale payload would
# silently stomp the value push just wrote, with no error and no signal that
# anything was lost.
#
# PUSH_FRESHNESS_WINDOW_SECONDS below closes the common case: if this
# task_id was pushed within the last few seconds, reconcile treats an
# incoming update for it as possibly-stale and skips applying it THIS pass,
# rather than blindly overwriting. Nothing is lost by skipping -- the Sheet
# row itself is untouched (we didn't write anything), so the very next edit
# anywhere in the sheet re-triggers onChange and reconcile re-evaluates this
# row with a fresh read that, by then, reflects the push. Worst case a
# same-instant Sheet edit applies on the next trigger instead of instantly;
# best case (the much more common one) it correctly defers to whichever
# side's write actually happened last. In-memory only (matches the lock
# dicts above) -- a restart just resets to the pre-fix behavior for the
# handful of tasks mid-edit at that exact moment, never worse than before
# this existed.
_recent_pushes: dict = {}
_recent_pushes_guard = threading.Lock()
PUSH_FRESHNESS_WINDOW_SECONDS = 8


def _mark_pushed(task_id: str):
    with _recent_pushes_guard:
        _recent_pushes[task_id] = time.monotonic()
        # Opportunistic prune, piggybacked on the same write -- keeps this
        # dict from growing unbounded over the life of the process without
        # needing a scheduled job (same reasoning as the tombstone table's
        # prune-on-insert).
        cutoff = time.monotonic() - (PUSH_FRESHNESS_WINDOW_SECONDS * 10)
        for tid in [t for t, ts in _recent_pushes.items() if ts < cutoff]:
            del _recent_pushes[tid]


def _recently_pushed(task_id: str) -> bool:
    with _recent_pushes_guard:
        ts = _recent_pushes.get(task_id)
    return ts is not None and (time.monotonic() - ts) < PUSH_FRESHNESS_WINDOW_SECONDS


def _retry(fn, attempts: int = 3, base_delay: float = 0.4) -> bool:
    """Runs fn() up to `attempts` times with short backoff between tries.
    Returns True once fn() completes without raising, False if every
    attempt failed. Used for the new-task-id write-back in
    reconcile_sheet_rows -- a transient network/quota blip on that one call
    otherwise leaves a row permanently id-less, which then gets treated as
    "new" again (and duplicate-created) on the next unrelated edit anywhere
    in the sheet. Not used for push_task_to_sheet, which already has its own
    fire-and-forget contract with the caller."""
    for i in range(attempts):
        try:
            fn()
            return True
        except Exception:
            if i == attempts - 1:
                return False
            time.sleep(base_delay * (i + 1))
    return False

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


def _first_sheet_id(spreadsheet_id: str) -> int:
    """The spreadsheet-internal numeric sheetId (gid) of the first tab --
    needed for a physical row delete via batchUpdate, which addresses rows
    by sheetId, not by the A1 range names read_all_rows/write_row use."""
    session = _get_session()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    r = session.get(url, params={"fields": "sheets.properties"})
    r.raise_for_status()
    sheets = r.json().get("sheets", [])
    return sheets[0]["properties"]["sheetId"] if sheets else 0


def delete_row(spreadsheet_id: str, row_number: int):
    """Physically removes one row (1-indexed, matching the Sheet's own row
    numbers), shifting everything below it up -- not a blank-out. Mirrors
    what a Sheet-side row delete already does to a Lumina task, so a
    Lumina-side delete now does the equivalent to the Sheet."""
    session = _get_session()
    sheet_id = _first_sheet_id(spreadsheet_id)
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
    new_sheet_id = r.json()["replies"][0]["addSheet"]["properties"]["sheetId"]
    try:
        apply_dropdown_validation(spreadsheet_id, new_sheet_id)
    except Exception:
        logger.exception(f"Failed to apply dropdown validation to newly-created tab '{sheet_name}' ({spreadsheet_id})")


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


def delete_task_from_sheet(link: dict, task_id: str) -> bool:
    """Public entry point -- serializes against any other Sheet-touching
    operation for the same client (see _get_client_sheet_lock). Note the
    tombstone write below happens INSIDE this lock, same as the physical
    delete -- so a reconcile pass that's waiting on this same client's lock
    will always see the tombstone already recorded once it gets to run,
    never a half-done delete."""
    with _get_client_sheet_lock(link.get("client_id", "")):
        return _delete_task_from_sheet_locked(link, task_id)


def _delete_task_from_sheet_locked(link: dict, task_id: str) -> bool:
    """Lumina -> Sheet, delete direction. Fire-and-forget from the frontend's
    perspective, same contract as push_task_to_sheet -- a failure here must
    never fail the Lumina-side delete that already succeeded. Returns
    whether a matching row was found and removed."""
    # Tombstone unconditionally, before anything below can fail -- this is
    # exactly the record reconcile_sheet_rows() needs to avoid resurrecting
    # this task if the physical Sheet delete below fails, races, or the
    # client turns out not to be configured at all.
    _tombstone_task(task_id, link.get("client_id", ""))
    if not is_configured():
        return False
    spreadsheet_id = link["spreadsheet_id"]
    try:
        rows = read_all_rows(spreadsheet_id)
    except Exception:
        logger.exception(f"Sheets delete: failed to read sheet {spreadsheet_id}")
        return False

    row_number = None
    for i, row in enumerate(rows):
        if i == 0:
            continue  # header
        if row and str(row[0]).strip() == str(task_id):
            row_number = i + 1
            break
    if not row_number:
        return False  # already gone from the Sheet, or never made it there

    # Retry like write_cell's id-write-back (see _retry) -- a transient
    # failure here leaves a row in the Sheet whose task_id no longer exists
    # in Lumina. That's the exact shape reconcile_sheet_rows() treats as
    # "undo after a Sheet-side delete" (see its `existing = current.get(...)`
    # branch below) and will RECREATE the task on the next unrelated Sheet
    # edit -- i.e. a plain network blip here can resurrect a task the user
    # deliberately deleted in Lumina. Retrying shrinks that window a lot;
    # it does not close it (a sustained outage still leaves the row orphaned
    # and eventually recreated) -- there's no tombstone table to suppress
    # recreation for a specific task_id, by design, not yet built.
    ok = _retry(lambda: delete_row(spreadsheet_id, row_number))
    if not ok:
        logger.error(
            f"Sheets delete: giving up removing row for task {task_id} after retries -- "
            f"row will linger in the Sheet and may get recreated as a new task on the next "
            f"unrelated edit."
        )
    return ok


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


# ── Field mapping ────────────────────────────────────────────────────────
# Column order matches RH_FIELD_LABELS in projects.html (12 Sheets fields),
# with lumina_task_id prepended as column A -- A:M is 13 columns total.
SHEET_FIELDS = ["creation_date", "due_date", "title", "type", "content", "idea",
                "scripts", "caption", "link", "myNotes", "assigned_to", "status"]

SHEET_HEADER_ROW = ["lumina_task_id", "Creation Date", "Post Day", "Post Title", "Type",
                     "Content", "Idea", "Scripts/Copy", "Caption", "File (Drive Link)",
                     "Notes", "Assigned To", "Status"]

# Column indices (0-based) of SHEET_HEADER_ROW's Type/Assigned To/Status
# columns -- used below to build data validation ranges. Kept as named
# constants rather than inline magic numbers since a future header reorder
# would otherwise silently misplace the dropdowns onto the wrong column.
_COL_TYPE = 4
_COL_ASSIGNED_TO = 11
_COL_STATUS = 12

# Exactly the same option sets as Lumina's own Sheets UI dropdowns
# (projects.html's Type <select>/pill-picker and Status <select>/pill-picker)
# -- see gotcha #87. Type intentionally excludes "Post"/"Video": those are
# accepted by _normalize_type() as legacy/detection fallbacks but were never
# real options in Lumina's own dropdown, so they're not offered here either.
TYPE_DROPDOWN_OPTIONS = ["Story", "Reel", "Static", "Carousel"]
STATUS_DROPDOWN_OPTIONS = ["Need to Start", "Scheduled", "In Progress", "Paused", "Posted", "Final"]


def _employee_names() -> list:
    from utils import _load_employees
    return [e["name"] for e in _load_employees().get("employees", []) if e.get("status") == "active"]


def _column_validation_requests(sheet_id: int) -> list:
    """setDataValidation requests for one tab's Type/Assigned To/Status
    columns. Type and Status are strict ONE_OF_LIST -- they're genuinely
    single-value fields in Lumina, so rejecting anything outside the exact
    option set at the Sheets level prevents the free-text-typo class of bug
    _normalize_type() had to be built to paper over (a stray "IG Reel" or
    "TikTok" in the Type column used to reach the backend at all -- see
    gotcha #87's fourth/sixth audit rounds). Assigned To is NOT strict:
    Lumina stores it as a comma-separated list of employee names (multiple
    assignees in one cell, see the checkbox picker in projects.html), which
    a single-value strict dropdown would reject outright -- so this is a
    non-blocking suggestion list instead, giving autocomplete without
    breaking multi-assignee entries."""
    def _rule(col_index, options, strict):
        return {
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,  # skip header row
                    "startColumnIndex": col_index,
                    "endColumnIndex": col_index + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": v} for v in options],
                    },
                    "showCustomUi": True,
                    "strict": strict,
                },
            }
        }
    requests = [
        _rule(_COL_TYPE, TYPE_DROPDOWN_OPTIONS, True),
        _rule(_COL_STATUS, STATUS_DROPDOWN_OPTIONS, True),
    ]
    names = _employee_names()
    if names:
        requests.append(_rule(_COL_ASSIGNED_TO, names, False))
    return requests


def apply_dropdown_validation(spreadsheet_id: str, sheet_id: int):
    """Applies the Type/Assigned To/Status dropdowns to one tab. Safe to
    call repeatedly -- setDataValidation replaces whatever rule (if any)
    was already on that range."""
    session = _get_session()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate"
    body = {"requests": _column_validation_requests(sheet_id)}
    r = session.post(url, json=body)
    r.raise_for_status()


def _is_effectively_blank(spreadsheet_id: str) -> bool:
    """True only for a genuinely fresh spreadsheet: exactly one tab (the
    default "Sheet1" every new Google Sheet starts with) and zero non-empty
    cells anywhere in it. False the moment there's more than one tab or any
    data at all -- this must never fire on a sheet someone has already
    started setting up by hand, even partially."""
    tabs = list_tabs(spreadsheet_id)
    if len(tabs) != 1:
        return False
    rows = read_tab_rows(spreadsheet_id, tabs[0]["name"])
    return not any(any(str(c).strip() for c in row) for row in rows)


def initialize_blank_spreadsheet(spreadsheet_id: str, multi_tab: bool) -> dict:
    """For a brand-new, completely empty spreadsheet handed to Connect: sets
    it up the same way a user would by hand, so it's immediately usable
    instead of silently doing nothing until the first task happens to sync.
    multi_tab=True: renames the default tab to the current month (the
    synced-tab-name convention -- see _month_tab_name_for) and creates a
    sibling "Unscheduled" tab, both with the standard header row and
    Type/Assigned To/Status dropdown validation. multi_tab=False: just adds
    the header row and dropdowns to the existing single tab, no renaming
    (single-tab mode reads whatever the first tab is called, regardless of
    name). No-op ({"initialized": False}) if the spreadsheet isn't blank by
    _is_effectively_blank()'s definition -- never touches an already-started
    sheet."""
    if not _is_effectively_blank(spreadsheet_id):
        return {"initialized": False}
    default_tab = list_tabs(spreadsheet_id)[0]
    if multi_tab:
        target_name = _month_tab_name_for(today_ist())
        session = _get_session()
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate"
        body = {"requests": [{
            "updateSheetProperties": {
                "properties": {"sheetId": default_tab["sheet_id"], "title": target_name},
                "fields": "title",
            }
        }]}
        r = session.post(url, json=body)
        r.raise_for_status()
    else:
        target_name = default_tab["name"]
    write_tab_row(spreadsheet_id, target_name, 1, SHEET_HEADER_ROW)
    apply_dropdown_validation(spreadsheet_id, default_tab["sheet_id"])
    created = [target_name]
    if multi_tab:
        ensure_tab_exists(spreadsheet_id, UNSCHEDULED_TAB_NAME)
        created.append(UNSCHEDULED_TAB_NAME)
    return {"initialized": True, "tabs": created}


def apply_dropdown_validation_to_all_tabs(spreadsheet_id: str) -> int:
    """Applies dropdowns to every tab in the spreadsheet (not just synced
    month tabs -- a stray tab getting dropdowns too is harmless, and this
    keeps the retroactive/connect-time call simple). Returns how many tabs
    were formatted. Best-effort per tab: one tab's failure (e.g. a weird
    sheetId edge case) shouldn't abort the rest."""
    formatted = 0
    for t in list_tabs(spreadsheet_id):
        try:
            apply_dropdown_validation(spreadsheet_id, t["sheet_id"])
            formatted += 1
        except Exception:
            logger.exception(
                f"Failed to apply dropdown validation to tab '{t['name']}' "
                f"(spreadsheet {spreadsheet_id})"
            )
    return formatted


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
    # Normalize the same way _create_task/_update_task always normalize
    # whatever ends up in the stored title -- without this, a Sheet's raw
    # Type cell (free text, e.g. "TikTok") never equals the normalized
    # value _task_to_fields() re-derives from an already-created task's
    # title ("Post"), so old_fields == row_fields would never be true for
    # that row and every reconcile pass would re-issue a spurious "changed"
    # update for it forever, even with nothing meaningfully different.
    fields["type"] = _normalize_type(fields.get("type"))
    return fields


# ── Task adapters (Notion + SQLite) ─────────────────────────────────────

# A client's linked Google Sheet is a social-media content calendar, not a
# general task board -- but a client can (and often does) also have
# branding/website/shoot/misc workflow tasks (see CLAUDE.md gotcha #10) that
# were never meant to appear there at all. Mirrors the exact same detection
# projects.html already uses for "is this a Sheets row" (its `socialTasks`
# filter in renderClientSheets(), and the two `isSocial` checks for Kanban
# column sets) -- must stay in sync with that regex/service check. Note:
# the frontend's regex (/\[(Story|...)\]/i.test(title)) has NO anchor --
# it matches the bracket anywhere in the title, not just at the start.
# Anchoring this one to the start (an earlier version of this regex used
# ^\[...\]) would make the backend STRICTER than the frontend -- under-
# inclusive relative to what Lumina's own UI already treats as a Sheets
# row, reopening the exact deletion-risk class of bug this filter exists
# to close in the first place (see the "mixed workflow" fix elsewhere in
# this gotcha), just for the narrower case of a title where the bracket
# isn't at position 0 (e.g. a task manually renamed to "Draft [Story]
# idea"). Using search() with no ^ makes this at least as inclusive as
# the frontend, which is the safe direction to err in -- over-inclusion
# here just means a task with a coincidental bracket substring gets
# treated as Sheets-eligible (cosmetic at worst), where under-inclusion
# means real data loss.
_SOCIAL_TITLE_RE = re.compile(r"\[(Story|Static|Reel|Carousel|Post|Video)\]", re.I)


def _is_social_task(task: dict) -> bool:
    if (task.get("service") or "") == "Social Media":
        return True
    return bool(_SOCIAL_TITLE_RE.search(task.get("title") or ""))


def _current_tasks_by_id(client_id: str, is_notion: bool) -> dict:
    """Snapshot of every SOCIAL-MEDIA task Lumina currently has for this
    client, keyed by id, fetched once per reconciliation pass rather than
    once per row -- see reconcile_sheet_rows below for why (avoids an N+1
    Notion API call). Filtered to _is_social_task() on purpose: without
    this, a client with any non-social workflow tasks would have ALL of
    them swept into this snapshot too -- backfilling them into the Sheet
    as garbage rows on connect, and (far worse) making reconcile's delete
    pass treat every one of them as "not recognized in this Sheet payload"
    and delete it on the very next unrelated Sheet edit, since they were
    never meant to be Sheets rows in the first place."""
    if is_notion:
        tasks = notion_store.list_tasks(client_notion_id=client_id)
        return {t["notion_id"]: t for t in tasks if _is_social_task(t)}
    from db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id,title,description,assigned_to,due_date,status,creation_date FROM tasks WHERE client_id=?", (client_id,))
    rows = cur.fetchall()
    conn.close()
    result = {}
    for r in rows:
        rec = {"notion_id": str(r[0]), "title": r[1], "description": r[2],
               "assigned_to": r[3], "due_date": r[4], "status": r[5], "creation_date": r[6] or ""}
        if _is_social_task(rec):
            result[str(r[0])] = rec
    return result


def _normalize_type(raw_type: str) -> str:
    """Collapses any Type value to one _SOCIAL_TITLE_RE actually recognizes.
    The Sheet's Type cell is free text -- Lumina's own Sheets UI only offers
    Story/Reel/Static/Carousel/Post via a dropdown, but a real Google Sheet
    has no such constraint, so a user can type anything (or a stray value
    from a bad paste) directly into that column.
    This matters beyond cosmetics: _create_task/_update_task always build
    the task's title as "[Type] Title", and _is_social_task() (used by
    _current_tasks_by_id, which both the connect-time backfill and every
    reconcile pass depend on) recognizes a task via EITHER a real "Social
    Media" service (Notion mode always sets this at creation, immune) OR
    that exact bracket prefix (the ONLY signal SQLite mode has, since its
    `tasks` table has no service column at all). Without normalizing here,
    a SQLite-mode task created from a row with an unrecognized Type (e.g.
    "TikTok") would build a title like "[TikTok] ..." that _is_social_task
    can't recognize -- invisible to _current_tasks_by_id on the very next
    reconcile pass, which (not finding its task_id in `current`, and it's
    not tombstoned) recreates it again from the same row, write-backs a
    new id, which itself fires another onChange... an unbounded create
    loop, one duplicate orphaned task per pass, for as long as that row's
    Type stays unrecognized. Normalizing at creation/update time instead
    guarantees the title _is_social_task expects is always produced."""
    cleaned = (raw_type or "").strip()
    return cleaned if cleaned.lower() in ("story", "static", "reel", "carousel", "post", "video") else "Post"


def _create_task(client_id: str, client_name: str, is_notion: bool, fields: dict):
    new_title = f"[{_normalize_type(fields.get('type'))}] {fields.get('title') or 'New Idea'}"
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
            "INSERT INTO tasks (client_id,title,description,assigned_to,due_date,status,progress,creation_date) VALUES (?,?,?,?,?,?,0,?)",
            (client_id, new_title, notes, fields.get("assigned_to", ""), fields.get("due_date", ""),
             fields.get("status") or "not_started", fields.get("creation_date", "")),
        )
        task_id = cur.lastrowid
    conn.close()
    return str(task_id)


def _update_task(task_id: str, is_notion: bool, fields: dict, editor_name: str) -> bool:
    new_title = f"[{_normalize_type(fields.get('type'))}] {fields.get('title') or ''}"
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
            "UPDATE tasks SET title=?, description=?, assigned_to=?, due_date=?, status=?, creation_date=?, "
            "last_edited_by=?, last_edited_at=?, last_edited_summary=? WHERE id=?",
            (new_title, notes, fields.get("assigned_to", ""), fields.get("due_date", ""),
             fields.get("status") or "not_started", fields.get("creation_date", ""),
             editor_name, f"{today_ist()} {now_ist()}", "Google Sheets sync", task_id),
        )
    conn.close()
    return True


def _log_version(task_id: str, client_id: str, editor_name: str, fields: dict, changed_fields=None):
    """Records one sheet_edit_log entry for a Sheet-originated create/update,
    same table/shape routes/ops.py's log_sheet_version writes for Lumina-
    originated edits (see gotcha #74). Without this, a task's Version
    History only ever showed edits made through Lumina's own UI -- any edit
    made directly in the linked Google Sheet was invisible there, even
    though it changed the task. Best-effort: a logging failure must never
    break the actual sync it's recording."""
    try:
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO sheet_edit_log (task_id, client_id, editor_name, edited_at, snapshot, changed_fields) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, client_id, editor_name, f"{today_ist()} {now_ist()}", json.dumps(fields),
                 json.dumps(changed_fields) if changed_fields is not None else None),
            )
        conn.close()
    except Exception:
        logger.exception(f"Sheets reconcile: failed to log version for task {task_id}")


# How long a deliberate Lumina-side delete blocks reconcile_sheet_rows() from
# recreating that task_id. Long enough to cover a delete_row() outage (well
# past its 3-attempt retry) and a stale/racing webhook payload settling; short
# enough that a genuine Ctrl+Z in Sheets to undo a *Sheet-initiated* delete
# (the original, still-desired use of that recreate path -- see its own
# comment below) keeps working almost always, since that's a different
# task_id history with no tombstone at all. A deliberate "undo my Lumina
# delete via Sheets" within this window is the one case this intentionally
# blocks -- Lumina's own 8s undo bar (commitPendingDelete) is the sanctioned
# way to do that, and has already closed by the time a Sheet delete can even
# fire.
TOMBSTONE_TTL_MINUTES = 15


def _tombstone_task(task_id: str, client_id: str):
    """Marks task_id as deliberately deleted from Lumina. Best-effort, same
    contract as _log_version -- a logging failure here must never break the
    actual delete it's recording."""
    try:
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO sheet_task_tombstones (task_id, client_id, deleted_at) VALUES (?,?,?)",
                (task_id, client_id, datetime.now(IST).isoformat()),
            )
            # Opportunistic prune, piggybacked on the same write -- keeps this
            # table from growing unbounded without needing a scheduled job.
            cutoff = (datetime.now(IST) - timedelta(minutes=TOMBSTONE_TTL_MINUTES)).isoformat()
            conn.execute("DELETE FROM sheet_task_tombstones WHERE deleted_at < ?", (cutoff,))
        conn.close()
    except Exception:
        logger.exception(f"Sheets delete: failed to tombstone task {task_id}")


def _is_tombstoned(task_id: str) -> bool:
    """True if task_id was deliberately deleted from Lumina within the TTL."""
    try:
        from db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT deleted_at FROM sheet_task_tombstones WHERE task_id=?", (task_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return False
        deleted_at = datetime.fromisoformat(row[0])
        return datetime.now(IST) - deleted_at < timedelta(minutes=TOMBSTONE_TTL_MINUTES)
    except Exception:
        logger.exception(f"Sheets reconcile: failed to check tombstone for task {task_id}")
        return False


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
    """Public entry point -- serializes against any other Sheet-touching
    operation for the same client (see _get_client_sheet_lock)."""
    with _get_client_sheet_lock(link.get("client_id", "")):
        return _push_task_to_sheet_locked(link, task_id, fields)


def _push_task_to_sheet_locked(link: dict, task_id: str, fields: dict) -> bool:
    """Lumina -> Sheet. Fire-and-forget from the frontend's perspective: a
    push failure here must never fail the save that already succeeded in
    Notion/SQLite (same contract as log_sheet_version in routes/ops.py).
    Returns whether the push itself succeeded, so the caller can record
    sync-health state (see routes/sheets_sync.py) without changing that
    fire-and-forget contract from the frontend's point of view."""
    if _is_tombstoned(task_id):
        # A push that lands after this task was already deleted from Lumina
        # (slow request, retry, or just unlucky ordering with the delete)
        # would otherwise write the row straight back into the Sheet --
        # undoing the delete from the OPPOSITE direction the tombstone in
        # reconcile_sheet_rows() guards against. And it's worse than a
        # simple resurrection: once TOMBSTONE_TTL_MINUTES elapses, that
        # phantom row stops being tombstoned too, so the next unrelated
        # Sheet edit's reconcile would treat it as a normal "Sheets undo"
        # and recreate the task in Lumina as well -- a delayed push alone
        # would eventually resurrect the task on both sides.
        logger.info(f"Sheets push: skipping push for tombstoned task {task_id} (deleted from Lumina).")
        return False
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
        _mark_pushed(task_id)
        return True
    except Exception:
        logger.exception(f"Sheets push: failed to write row for task {task_id}")
        return False


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


def reconcile_sheet_rows(link: dict, rows: list) -> dict:
    """Public entry point -- serializes against any other Sheet-touching
    operation for the same client (see _get_client_sheet_lock) before
    handing off to the real implementation below. Different clients run
    fully in parallel; same client runs one operation at a time."""
    with _get_client_sheet_lock(link["client_id"]):
        return _reconcile_sheet_rows_locked(link, rows)


def _reconcile_sheet_rows_locked(link: dict, rows: list) -> dict:
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
    # Ids that matched a REAL current task, as opposed to seen_ids (every
    # non-blank id encountered, including ones that don't match anything in
    # `current` and get recreated as garbage). The safety-guard check and
    # the delete pass below both need "did this payload actually recognize
    # any of our real tasks", not "did it contain any id at all" -- a single
    # unrecognized/garbage row (e.g. a stray non-Sheets tab getting synced
    # by mistake) used to make seen_ids non-empty and silently bypass the
    # safety guard, letting every real task for the client get deleted. See
    # CLAUDE.md gotcha #87's flagged-not-fixed entry, fixed here.
    recognized_ids = set()
    created, updated, deleted, skipped, errored, duplicates, recreated, tombstoned, skipped_recent_push, update_failed, delete_failed = \
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0

    for idx, row in enumerate(rows):
        row_number = idx + 2  # +1 for 0-index, +1 for the header row Apps Script stripped
        try:
            # `rows` is caller-supplied JSON (from Apps Script normally, but
            # the webhook's only real authenticator is the link_token in the
            # URL -- anyone holding it can POST anything). A single
            # malformed entry (not a list, or one that doesn't behave like
            # one) must not abort every other row's sync, same reasoning as
            # notion_store.list_tasks()'s per-page isolation (gotcha #52).
            # Explicit isinstance check, not just `if row:` -- a JSON string
            # element (e.g. a garbled payload's row entry being "garbage"
            # instead of a list) is truthy and `row[0]` on it doesn't throw
            # (string indexing), so without this it silently falls through
            # as one fake near-blank row per character instead of erroring.
            if not isinstance(row, list):
                errored += 1
                continue
            task_id = str(row[0]).strip() if row else ""
            row_fields = _row_to_fields(row)
            if not task_id and not any(row_fields.values()):
                continue  # fully blank row

            if not task_id:
                new_id = _create_task(client_id, client_name, is_notion, row_fields)
                if new_id:
                    ok = _retry(lambda: write_cell(spreadsheet_id, f"A{row_number}", new_id))
                    if not ok:
                        logger.error(
                            f"Sheets reconcile: giving up writing back task id {new_id} for row "
                            f"{row_number} (client {client_id}) after retries -- row stays id-less "
                            f"and may get duplicate-created on the next unrelated edit."
                        )
                    _log_version(new_id, client_id, f"{editor_name} (via Google Sheets)", row_fields,
                                 changed_fields=list(row_fields.keys()))
                    created += 1
                continue

            if task_id in seen_ids:
                # Two rows in the same snapshot share a task_id -- almost
                # always a copy-pasted row where column A wasn't cleared.
                # Keep whichever occurrence was processed first (the earlier
                # row, deterministic) and flag it instead of silently
                # letting the later row's fields overwrite it with no trace.
                logger.warning(
                    f"Sheets reconcile: duplicate task_id {task_id} at row {row_number} for client "
                    f"{client_id} -- ignoring, an earlier row already claimed this id this pass."
                )
                duplicates += 1
                continue
            seen_ids.add(task_id)

            existing = current.get(task_id)
            if not existing:
                # References a task id Lumina no longer has. Two different
                # things produce this exact shape, and they need opposite
                # handling:
                #  (a) the row was deleted in Sheets (correctly deleting the
                #      Lumina task), then the delete was undone with Ctrl+Z in
                #      Sheets -- recreate it, see below.
                #  (b) the task was deliberately deleted from LUMINA, whose
                #      delete_task_from_sheet() failed/raced to remove the
                #      Sheet row in time -- recreating here would silently
                #      resurrect a task the user just deleted on purpose.
                # _is_tombstoned() is how (b) is told apart from (a): only a
                # Lumina-initiated delete writes a tombstone (see
                # _tombstone_task, called from delete_task_from_sheet).
                if _is_tombstoned(task_id):
                    # Deliberately not physically deleting this row here: row
                    # numbers in this loop are positions in the static `rows`
                    # snapshot, and every other branch's row_number-addressed
                    # write (write_cell for a create/recreate elsewhere in
                    # this same pass) assumes the Sheet hasn't shifted under
                    # it mid-loop. A row-removing batchUpdate here would
                    # invalidate every row_number computed after it. Simplest
                    # safe fix: leave the stale row in place and just skip it
                    # -- it'll either get physically cleaned up by a later
                    # delete_task_from_sheet retry, or naturally fall through
                    # to the ordinary recreate path once TOMBSTONE_TTL_MINUTES
                    # passes, which is the existing (accepted) behavior for a
                    # reference this old.
                    logger.info(
                        f"Sheets reconcile: row {row_number} (client {client_id}) references tombstoned "
                        f"task_id {task_id} -- not recreating (deliberately deleted from Lumina)."
                    )
                    tombstoned += 1
                    continue
                # (a): recreate as a new task from the row's current data and
                # overwrite column A with the new id, so undo-after-delete
                # recovers a working (if new-id) task instead of a silently
                # dead row that every future edit also hits this branch for.
                new_id = _create_task(client_id, client_name, is_notion, row_fields)
                if new_id:
                    logger.warning(
                        f"Sheets reconcile: row {row_number} (client {client_id}) referenced task_id "
                        f"{task_id}, which no longer exists in Lumina -- recreated as {new_id} "
                        f"(likely a Sheets undo after a row delete)."
                    )
                    ok = _retry(lambda: write_cell(spreadsheet_id, f"A{row_number}", new_id))
                    if not ok:
                        logger.error(
                            f"Sheets reconcile: giving up writing back recreated task id {new_id} for "
                            f"row {row_number} (client {client_id}) after retries."
                        )
                    _log_version(new_id, client_id, f"{editor_name} (via Google Sheets)", row_fields,
                                 changed_fields=list(row_fields.keys()))
                    recreated += 1
                continue
            # This row's task_id matched a real current task -- the only
            # point in this function where that's true. See the comment on
            # recognized_ids' declaration above for why this must be tracked
            # separately from seen_ids.
            recognized_ids.add(task_id)
            old_fields = _task_to_fields(existing)
            if old_fields == row_fields:
                skipped += 1
                continue
            if _recently_pushed(task_id):
                # `existing` (fetched fresh, after this pass acquired the
                # per-client lock) already reflects that recent push. Since
                # it still differs from row_fields, this payload's view of
                # this row predates the push -- applying it would silently
                # overwrite the field(s) that push just wrote. Skip this
                # task for this pass only; see PUSH_FRESHNESS_WINDOW_SECONDS
                # above for why nothing is lost by doing so.
                logger.info(
                    f"Sheets reconcile: row {row_number} (client {client_id}, task_id {task_id}) was "
                    f"pushed to within the last {PUSH_FRESHNESS_WINDOW_SECONDS}s -- deferring this "
                    f"update to the next pass instead of risking a stale overwrite."
                )
                skipped_recent_push += 1
                continue
            # _update_task()'s bool return was previously discarded here --
            # a failed Notion PATCH (e.g. an unrecognized Status value from
            # the Sheet getting rejected outright if the workspace's Status
            # property is Notion's newer `status` type rather than `select`,
            # which auto-creates new options -- see CLAUDE.md gotcha #43 for
            # the established "one bad property fails the WHOLE page PATCH"
            # behavior) still got reported as a success: updated += 1, and a
            # version-history entry claiming the change happened, even
            # though Notion still held the old data. Same silent-failure
            # class already fixed elsewhere in this app for other Notion-
            # writing flows (gotchas #53/#86) -- check the return value
            # instead of assuming success.
            ok = _update_task(task_id, is_notion, row_fields, editor_name)
            if not ok:
                logger.warning(
                    f"Sheets reconcile: update failed for task_id {task_id} (row {row_number}, "
                    f"client {client_id}) -- not logging a version or counting it as updated, "
                    f"since Notion/SQLite may not actually hold the new values."
                )
                update_failed += 1
                continue
            changed = [k for k in row_fields if row_fields.get(k) != old_fields.get(k)]
            _log_version(task_id, client_id, f"{editor_name} (via Google Sheets)", row_fields,
                         changed_fields=changed)
            updated += 1
        except Exception:
            logger.exception(f"Sheets reconcile: skipping malformed row {row_number} for client {client_id}")
            errored += 1

    # Safety guard: if this snapshot recognized zero existing tasks (empty
    # `rows`, or every row was a blank/unrecognized/newly-created one) while
    # Lumina has tasks on file for this client, treat it as a suspicious
    # payload rather than "the user deleted everything" -- a flaky onChange
    # firing mid-paste, a momentarily cleared sheet, or a script bug could
    # otherwise wipe every task for a client in one request. Deleting
    # everything on purpose should go through an explicit action, not a
    # webhook payload that happens to recognize nothing.
    if not recognized_ids and current:
        logger.warning(
            f"Sheets reconcile: snapshot for client {client_id} recognized 0 of "
            f"{len(current)} known tasks -- skipping delete pass as a safety measure."
        )
        return {"created": created, "updated": updated, "deleted": 0, "skipped": skipped,
                "errored": errored, "duplicates": duplicates, "recreated": recreated,
                "tombstoned": tombstoned, "skipped_recent_push": skipped_recent_push,
                "update_failed": update_failed, "delete_failed": 0, "deletes_skipped_safety": len(current)}

    for existing_id in current:
        if existing_id not in recognized_ids:
            # Same discarded-return-value bug as _update_task above: a
            # failed archive_notion_page() (Notion mode) still got counted
            # as a successful delete. The task then stays active in Notion
            # forever while Lumina believes it's gone, and the next
            # reconcile pass re-fetches it into `current` and "deletes" it
            # again -- same false-positive-success shape, just retried
            # instead of stuck.
            if _delete_task(existing_id, is_notion):
                deleted += 1
            else:
                logger.warning(
                    f"Sheets reconcile: delete failed for task_id {existing_id} (client {client_id}) "
                    f"-- not counting it as deleted, it may still be active in Notion/SQLite."
                )
                delete_failed += 1

    return {"created": created, "updated": updated, "deleted": deleted, "skipped": skipped,
            "errored": errored, "duplicates": duplicates, "recreated": recreated,
            "tombstoned": tombstoned, "skipped_recent_push": skipped_recent_push,
            "update_failed": update_failed, "delete_failed": delete_failed}


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
    # See the matching declaration + comment in _reconcile_sheet_rows_locked
    # -- same fix, same reasoning, applied here too.
    recognized_ids = set()
    created, updated, deleted, skipped, errored, duplicates, recreated, tombstoned, skipped_recent_push, update_failed, delete_failed = \
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0

    for tab_name, rows in tabs.items():
        if not _is_synced_tab_name(tab_name):
            continue  # defensive -- Apps Script already filters, a hand-edited payload might not
        for idx, row in enumerate(rows):
            row_number = idx + 2  # +1 for 0-index, +1 for the header row Apps Script stripped
            row_label = f"{tab_name} row {row_number}"
            try:
                # See the matching guard in _reconcile_sheet_rows_locked --
                # a non-list row (e.g. a bare string) is truthy and doesn't
                # throw on row[0], so without this it silently fakes rows
                # instead of erroring.
                if not isinstance(row, list):
                    errored += 1
                    continue
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

                # This row's task_id matched a real current task -- see the
                # comment on recognized_ids' declaration above.
                recognized_ids.add(task_id)
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
                # See the matching comment in _reconcile_sheet_rows_locked --
                # _update_task()'s return was previously discarded, silently
                # reporting a failed Notion/SQLite write as a success.
                ok = _update_task(task_id, is_notion, row_fields, editor_name)
                if not ok:
                    logger.warning(
                        f"Sheets reconcile (multi-tab): update failed for task_id {task_id} "
                        f"({row_label}, client {client_id}) -- not logging a version or counting "
                        f"it as updated."
                    )
                    update_failed += 1
                    continue
                changed = [k for k in row_fields if row_fields.get(k) != old_fields.get(k)]
                _log_version(task_id, client_id, f"{editor_name} (via Google Sheets)", row_fields,
                             changed_fields=changed)
                updated += 1
            except Exception:
                logger.exception(f"Sheets reconcile (multi-tab): skipping malformed row at {row_label} for client {client_id}")
                errored += 1

    if not recognized_ids and current:
        logger.warning(
            f"Sheets reconcile (multi-tab): snapshot for client {client_id} recognized 0 of "
            f"{len(current)} known tasks -- skipping delete pass as a safety measure."
        )
        return {"created": created, "updated": updated, "deleted": 0, "skipped": skipped,
                "errored": errored, "duplicates": duplicates, "recreated": recreated,
                "tombstoned": tombstoned, "skipped_recent_push": skipped_recent_push,
                "update_failed": update_failed, "delete_failed": 0,
                "deletes_skipped_safety": len(current), "deletes_skipped_missing_tab": 0}

    # Per-task guard on top of the all-tabs-empty safety check above: a task
    # missing from `seen_ids` means SOME tab didn't contain it, but that's
    # ambiguous between two very different causes --
    #  (a) the row was genuinely deleted from within its tab (that tab's own
    #      name IS a key in `tabs`, just with fewer rows) -- a real delete
    #      signal, safe to mirror.
    #  (b) the tab this task's own due_date says it belongs in was renamed
    #      (typo, reorganizing) or deleted outright, so it never appears as
    #      a key in `tabs` at all -- the Apps Script snippet only sends tabs
    #      whose NAME still matches the convention, unconditionally
    #      including an entry (even an empty list) for every one that does.
    # Treating (b) the same as (a) would mass-delete every task that ever
    # lived in that tab on the very next unrelated edit anywhere else in the
    # spreadsheet, off nothing more than a rename typo. Skip (b) instead --
    # same "defer, don't destroy" reasoning as the tombstone and recent-push
    # guards elsewhere in this file.
    payload_tab_names = set(tabs.keys())
    deletes_skipped_missing_tab = 0
    for existing_id in current:
        if existing_id in recognized_ids:
            continue
        target_tab = _month_tab_name_for(current[existing_id].get("due_date", ""))
        if target_tab not in payload_tab_names:
            logger.warning(
                f"Sheets reconcile (multi-tab): task {existing_id} (client {client_id}) belongs in tab "
                f"'{target_tab}', which wasn't present in this sync -- skipping delete instead of "
                f"assuming it was intentionally removed (renamed or deleted tab?)."
            )
            deletes_skipped_missing_tab += 1
            continue
        if _delete_task(existing_id, is_notion):
            deleted += 1
        else:
            logger.warning(
                f"Sheets reconcile (multi-tab): delete failed for task_id {existing_id} "
                f"(client {client_id}) -- not counting it as deleted."
            )
            delete_failed += 1

    return {"created": created, "updated": updated, "deleted": deleted, "skipped": skipped,
            "errored": errored, "duplicates": duplicates, "recreated": recreated,
            "tombstoned": tombstoned, "skipped_recent_push": skipped_recent_push,
            "update_failed": update_failed, "delete_failed": delete_failed,
            "deletes_skipped_missing_tab": deletes_skipped_missing_tab}
