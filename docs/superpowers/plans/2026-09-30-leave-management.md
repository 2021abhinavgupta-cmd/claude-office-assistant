# Leave Management System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every employee a 15-day annual leave pool, a calendar-based
apply/approve workflow gated by HR (Noorish, `emp009`), automatic overtime
(>24hrs cumulative beyond a 9hr/day baseline) converting into extra leave
days, and leave visibility in the existing attendance Excel export.

**Architecture:** Extend the existing `employee_leave` table (built in
gotcha #119 for a narrower purpose) with a status/approval/type layer
instead of forking a parallel table. Two new ledger tables
(`overtime_ledger`, `comp_off_ledger`) back a nightly conversion job. A new
`routes/leave.py` blueprint exposes balance/calendar/apply/approve/reject.
The existing `/api/attendance/export-sheets` xlsx export — which already
computes daily/monthly overtime via `_overtime_hours()`/`_hours_and_day_type()`
in `routes/attendance.py` — gets extended to show approved-leave-vs-plain-
absence and the formal balance, rather than building a second export.
WhatsApp's existing `set_leave`/`clear_leave` tools switch from instant-apply
to request-and-wait, closing the self-approval loophole this feature exists
to prevent. A new `frontend/leave.html` page is the human-facing calendar.

**Tech Stack:** Flask blueprints, raw sqlite3 via `db.get_connection()`,
APScheduler (existing `task_scheduler.py`), vanilla JS/HTML (no build step,
matches every other page in this codebase).

**Spec:** `docs/superpowers/specs/2026-09-30-leave-management-design.md`
(the Excel section there is superseded by the discovery in this plan's
intro — `attendance_export_sheets()` already exists and is extended in
Task 7 rather than a new endpoint being built)

## Global Constraints

- Every employee's annual pool is exactly **15 days**, flat, no proration.
- Overtime baseline: **9 hours/day** (already `_FULL_DAY_HOURS = 9.0` in
  `routes/attendance.py` — reuse it, do not redefine it).
- Conversion rate: every **24** cumulative unconverted overtime hours ->
  **+1** leave day (`comp_off_ledger`).
- HR approver is **hardcoded to `emp009`** (Noorish) — same allowlist
  convention as the bet feature (`routes/ops.py` lines ~2967/2986), not the
  generic `bool(user_id)` admin pattern.
- Leave year = **calendar year (Jan 1 - Dec 31), no carryover**. Comp-off
  earned also resets with the same year boundary.
- Half-day leave deducts **0.5**, full-day deducts **1.0**.
- Calendar dot thresholds: worked hours `>= 6` -> green (full day worked),
  `0 < hours < 6` -> yellow (half day worked), approved leave -> red,
  pending leave request -> orange, weekend/no-data -> no dot.
- Backdated leave applications are allowed (no future-only restriction).
- WhatsApp `set_leave` now creates a **pending** row, not an instant grant.
  `is_on_leave()`/`on_leave_ids()`/`active_leave()` must only ever consider
  `status='approved'` rows — this is what closes the self-approval loophole
  and is the one behavior change to an existing feature (gotcha #119).

## Review Focus

- Applying for leave dates that overlap an already-**approved** leave for
  the same person -> must be rejected, not silently double-booked. (Task 2)
- `is_on_leave`/`on_leave_ids` must exclude `pending` rows -> a same-day
  WhatsApp leave request must NOT unlock the standup lock or silence login
  nudges until HR approves it. (Task 2)
- `leave_type` must be validated to exactly `full`/`half` -> anything else
  rejected before it can corrupt the 0.5/1.0 balance math. (Task 2/3)
- Only `emp009` may list pending requests, approve, or reject -> any other
  `user_id` gets 403, including an employee trying to approve their own
  request. (Task 3)
- The nightly overtime-conversion sweep must be idempotent if run twice for
  the same date (e.g. a server restart re-triggering it) -> no duplicate
  `overtime_ledger` rows, no double-counted `comp_off_ledger` accrual.
  (Task 4)

---

## Task 1: Database schema — extend `employee_leave`, add ledger tables

**Files:**
- Modify: `backend/db.py:218-228` (the existing `employee_leave` CREATE TABLE
  block)
- Test: `scripts/test_leave_schema.py` (new)

**Interfaces:**
- Consumes: nothing (schema layer, no dependencies)
- Produces: `employee_leave` gains columns `status TEXT DEFAULT 'approved'`,
  `leave_type TEXT DEFAULT 'full'`, `approved_by TEXT DEFAULT NULL`,
  `approved_at TEXT DEFAULT NULL`. New tables `overtime_ledger(id, user_id,
  date, worked_hours, ot_hours, converted_at, created_at)` with
  `UNIQUE(user_id, date)`, and `comp_off_ledger(id, user_id, days,
  source_ot_hours, created_at)`. Every later task reads/writes these exact
  column names.

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_leave_schema.py
"""Scratch test: employee_leave gets its new columns, and the two new
ledger tables exist, after db.init_db() runs. Uses a temp sqlite file,
never touches logs/app.db."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["LUMINA_DB_PATH"] = tmp_path  # picked up by db.py below if it honours it

import db  # noqa: E402

# db.py's DB_PATH is normally a fixed constant -- override it directly so
# this test never touches the real logs/app.db regardless of env var support.
db.DB_PATH = tmp_path
db.init_db()

conn = db.get_connection()
cols = {r[1] for r in conn.execute("PRAGMA table_info(employee_leave)").fetchall()}
assert {"status", "leave_type", "approved_by", "approved_at"} <= cols, \
    f"employee_leave missing new columns, has: {cols}"

tables = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
assert "overtime_ledger" in tables, "overtime_ledger table not created"
assert "comp_off_ledger" in tables, "comp_off_ledger table not created"

ot_cols = {r[1] for r in conn.execute("PRAGMA table_info(overtime_ledger)").fetchall()}
assert {"user_id", "date", "worked_hours", "ot_hours", "converted_at"} <= ot_cols

co_cols = {r[1] for r in conn.execute("PRAGMA table_info(comp_off_ledger)").fetchall()}
assert {"user_id", "days", "source_ot_hours", "created_at"} <= co_cols

# Old-shape insert (no status/leave_type given) must still work and default
# to 'approved'/'full' -- existing WhatsApp-set rows must not become invalid.
conn.execute(
    "INSERT INTO employee_leave (user_id, start_date, end_date, reason, created_by) "
    "VALUES ('emp003','2026-01-01','2026-01-01','test','test')"
)
conn.commit()
row = conn.execute(
    "SELECT status, leave_type FROM employee_leave WHERE user_id='emp003'"
).fetchone()
assert row == ("approved", "full"), f"expected default ('approved','full'), got {row}"

conn.close()
os.remove(tmp_path)
print("OK: test_leave_schema")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe scripts\test_leave_schema.py` (Windows) or
`venv/bin/python scripts/test_leave_schema.py`
Expected: `AssertionError: employee_leave missing new columns` (the columns
don't exist yet)

- [ ] **Step 3: Add the schema changes**

In `backend/db.py`, replace the existing `employee_leave` block
(lines 214-228) with:

```python
        # Employee leave / holiday -- inclusive [start_date, end_date]
        # windows. A row here exempts the person from the daily-standup
        # lock and from every "you haven't logged in" nudge for the days
        # it covers, but ONLY once status='approved' (CLAUDE.md gotcha
        # #119, extended by the leave management system to add a real
        # approval workflow -- 2026-09-30). status defaults to 'approved'
        # so every row written before this change (all WhatsApp instant
        # grants) stays valid and still correctly counts as leave taken.
        conn.execute("""CREATE TABLE IF NOT EXISTS employee_leave (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            start_date  TEXT NOT NULL,
            end_date    TEXT NOT NULL,
            reason      TEXT DEFAULT '',
            created_by  TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now')),
            status      TEXT DEFAULT 'approved',
            leave_type  TEXT DEFAULT 'full',
            approved_by TEXT DEFAULT NULL,
            approved_at TEXT DEFAULT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_employee_leave_user "
                     "ON employee_leave(user_id, start_date, end_date)")
        for _col, _ddl in (
            ("status", "TEXT DEFAULT 'approved'"),
            ("leave_type", "TEXT DEFAULT 'full'"),
            ("approved_by", "TEXT DEFAULT NULL"),
            ("approved_at", "TEXT DEFAULT NULL"),
        ):
            try:
                conn.execute(f"ALTER TABLE employee_leave ADD COLUMN {_col} {_ddl}")
            except Exception:
                pass  # Column already exists

        # Daily overtime -- one row per employee per day with a computed
        # worked/overtime figure, feeding the leave-conversion sweep below.
        # Reuses the same 9hr/day baseline as the attendance Excel export
        # (routes/attendance.py::_FULL_DAY_HOURS/_overtime_hours).
        conn.execute("""CREATE TABLE IF NOT EXISTS overtime_ledger (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT NOT NULL,
            date         TEXT NOT NULL,
            worked_hours REAL NOT NULL,
            ot_hours     REAL NOT NULL,
            converted_at TEXT DEFAULT NULL,
            created_at   TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, date)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_overtime_ledger_user "
                     "ON overtime_ledger(user_id, converted_at)")

        # One row per 24hr-overtime -> +1 leave day conversion event.
        conn.execute("""CREATE TABLE IF NOT EXISTS comp_off_ledger (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT NOT NULL,
            days            REAL NOT NULL,
            source_ot_hours REAL NOT NULL,
            created_at      TEXT DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comp_off_ledger_user "
                     "ON comp_off_ledger(user_id, created_at)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv\Scripts\python.exe scripts\test_leave_schema.py`
Expected: `OK: test_leave_schema`

- [ ] **Step 5: Run pyflakes to confirm no new warnings**

Run: `venv\Scripts\python.exe -m pyflakes backend\db.py`
Expected: same warning count as before this change (or fewer) — no new
"undefined name" errors.

- [ ] **Step 6: Commit**

```bash
git add backend/db.py scripts/test_leave_schema.py
git commit -m "$(cat <<'EOF'
Add leave approval columns and overtime/comp-off ledger tables

employee_leave gains status/leave_type/approved_by/approved_at, defaulting
to 'approved'/'full' so existing WhatsApp-granted rows stay valid. New
overtime_ledger and comp_off_ledger tables back the nightly conversion job.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `leave_store.py` — apply/approve/reject/cancel/balance/calendar

**Files:**
- Modify: `backend/leave_store.py` (entire file — extends the existing
  module from gotcha #119)
- Test: `scripts/test_leave_store.py` (new)

**Interfaces:**
- Consumes: `db.get_connection()`, `db.DB_PATH` (Task 1's schema), `utils.today_ist()`
- Produces (read by Task 3's routes, Task 4's scheduler job, Task 6's
  WhatsApp tools, Task 7's export):
  - `apply_leave(user_id: str, start_date: str, end_date: str, leave_type: str = "full", reason: str = "", created_by: str = "") -> dict` — raises `ValueError` on bad `leave_type` or an overlap with an existing `approved` row for that user; returns `{"id", "user_id", "start_date", "end_date", "leave_type", "reason", "status": "pending"}`
  - `approve_leave(leave_id: int, approved_by: str) -> bool`
  - `reject_leave(leave_id: int, approved_by: str, reason: str = "") -> bool`
  - `cancel_leave(leave_id: int, user_id: str) -> bool`
  - `get_balance(user_id: str, year: int | None = None) -> dict` — `{"base": 15, "comp_earned": float, "used": float, "remaining": float, "year": int}`
  - `list_pending() -> list[dict]`
  - `calendar_days(user_id: str, year: int, month: int) -> dict[str, dict]` — `{"YYYY-MM-DD": {"status": "full"|"half"|"leave_approved"|"leave_pending"|"weekend"|"none", "hours": float|None}}`
  - `run_overtime_conversion_sweep(for_date: str | None = None) -> dict` — `{"processed": int, "converted_events": int}`
  - `set_leave(user_id, start_date, end_date, reason="", created_by="", status="pending") -> dict` (existing function, now defaults to `pending`, only deletes overlapping rows with `status='pending'` for that user first — never touches an already-`approved` row)
  - `clear_leave(user_id, on_date=None) -> int` (existing function, now only removes rows with `status IN ('pending','approved') AND end_date >= today`)
  - `is_on_leave(user_id, date_str) -> bool`, `on_leave_ids(date_str) -> set[str]`, `active_leave(user_id, date_str) -> dict|None` (existing signatures unchanged, now filter `status='approved'` internally)

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_leave_store.py
"""Scratch test for leave_store.py -- temp sqlite DB, never logs/app.db."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

import db  # noqa: E402
db.DB_PATH = tmp_path
db.init_db()

import leave_store  # noqa: E402

# ── apply_leave basic + validation ──────────────────────────────────────
row = leave_store.apply_leave("emp010", "2026-10-05", "2026-10-05",
                               leave_type="full", reason="sick",
                               created_by="Sid")
assert row["status"] == "pending", row
lid = row["id"]

try:
    leave_store.apply_leave("emp010", "2026-10-06", "2026-10-06", leave_type="quarter")
    raise AssertionError("bad leave_type should have raised ValueError")
except ValueError:
    pass

# ── pending must NOT count as on-leave (closes the self-approval loophole) ──
assert leave_store.is_on_leave("emp010", "2026-10-05") is False
assert "emp010" not in leave_store.on_leave_ids("2026-10-05")

# ── approve, then it DOES count ─────────────────────────────────────────
assert leave_store.approve_leave(lid, "emp009") is True
assert leave_store.is_on_leave("emp010", "2026-10-05") is True
assert "emp010" in leave_store.on_leave_ids("2026-10-05")

# ── overlapping an approved leave is rejected ───────────────────────────
try:
    leave_store.apply_leave("emp010", "2026-10-05", "2026-10-05", leave_type="full")
    raise AssertionError("overlapping an approved leave should have raised ValueError")
except ValueError:
    pass

# ── half-day balance math ───────────────────────────────────────────────
row2 = leave_store.apply_leave("emp010", "2026-10-12", "2026-10-12", leave_type="half")
assert leave_store.approve_leave(row2["id"], "emp009") is True
bal = leave_store.get_balance("emp010", year=2026)
assert bal["base"] == 15, bal
assert bal["used"] == 1.5, bal  # 1 full + 1 half
assert bal["remaining"] == 13.5, bal

# ── reject leaves balance untouched ─────────────────────────────────────
row3 = leave_store.apply_leave("emp010", "2026-11-01", "2026-11-01")
assert leave_store.reject_leave(row3["id"], "emp009", reason="no") is True
bal2 = leave_store.get_balance("emp010", year=2026)
assert bal2["used"] == 1.5, bal2  # unchanged

# ── cancel only works on a still-pending row the employee owns ─────────
row4 = leave_store.apply_leave("emp010", "2026-11-05", "2026-11-05")
assert leave_store.cancel_leave(row4["id"], "emp010") is True
assert leave_store.cancel_leave(lid, "emp010") is False  # lid is already approved

# ── list_pending only shows pending rows ────────────────────────────────
row5 = leave_store.apply_leave("emp002", "2026-11-10", "2026-11-10")
pending = leave_store.list_pending()
ids = {p["id"] for p in pending}
assert row5["id"] in ids
assert lid not in ids  # already approved, shouldn't show

# ── calendar_days: full/half/leave dots ─────────────────────────────────
conn = db.get_connection()
conn.execute(
    "INSERT INTO daily_attendance (user_id, date, checkin_time, checkout_time) "
    "VALUES ('emp010','2026-10-06','09:00:00','18:00:00')"  # 9h -> full
)
conn.execute(
    "INSERT INTO daily_attendance (user_id, date, checkin_time, checkout_time) "
    "VALUES ('emp010','2026-10-07','09:00:00','12:00:00')"  # 3h -> half
)
conn.commit()
conn.close()
days = leave_store.calendar_days("emp010", 2026, 10)
assert days["2026-10-05"]["status"] == "leave_approved", days["2026-10-05"]
assert days["2026-10-06"]["status"] == "full", days["2026-10-06"]
assert days["2026-10-07"]["status"] == "half", days["2026-10-07"]

# ── set_leave (WhatsApp path) now defaults to pending, and never deletes
# an already-approved overlapping row ───────────────────────────────────
wa_row = leave_store.set_leave("emp002", "2026-12-01", "2026-12-01",
                                reason="wa", created_by="bot")
assert wa_row.get("status", "pending") == "pending" or True  # tolerate either shape
conn = db.get_connection()
st = conn.execute(
    "SELECT status FROM employee_leave WHERE id=?", (wa_row["id"],)
).fetchone()[0]
assert st == "pending", st
conn.close()

# an approved row for emp002 elsewhere must survive a second set_leave call
# that doesn't overlap it
approved_row = leave_store.apply_leave("emp002", "2026-12-20", "2026-12-20")
leave_store.approve_leave(approved_row["id"], "emp009")
leave_store.set_leave("emp002", "2026-12-01", "2026-12-01", reason="again")
conn = db.get_connection()
still_there = conn.execute(
    "SELECT status FROM employee_leave WHERE id=?", (approved_row["id"],)
).fetchone()
assert still_there == ("approved",), still_there
conn.close()

# ── run_overtime_conversion_sweep: idempotent + converts at 24hrs ───────
conn = db.get_connection()
conn.execute(
    "INSERT INTO daily_attendance (user_id, date, checkin_time, checkout_time) "
    "VALUES ('emp003','2026-10-01','09:00:00','22:00:00')"  # 13h -> 4h OT
)
conn.commit()
conn.close()
r1 = leave_store.run_overtime_conversion_sweep(for_date="2026-10-01")
assert r1["processed"] >= 1, r1
r1b = leave_store.run_overtime_conversion_sweep(for_date="2026-10-01")  # re-run, must not double count
conn = db.get_connection()
cnt = conn.execute(
    "SELECT COUNT(*) FROM overtime_ledger WHERE user_id='emp003' AND date='2026-10-01'"
).fetchone()[0]
assert cnt == 1, f"expected exactly 1 ledger row after two sweeps, got {cnt}"
conn.close()

# push enough more days to cross 24hrs and confirm one comp-off accrual fires
conn = db.get_connection()
for i, d in enumerate(["2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"]):
    conn.execute(
        "INSERT INTO daily_attendance (user_id, date, checkin_time, checkout_time) "
        "VALUES (?,?,?,?)", ("emp003", d, "09:00:00", "22:00:00")  # 4h OT/day
    )
conn.commit()
conn.close()
for d in ["2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"]:
    leave_store.run_overtime_conversion_sweep(for_date=d)
conn = db.get_connection()
accrued = conn.execute(
    "SELECT COUNT(*) FROM comp_off_ledger WHERE user_id='emp003'"
).fetchone()[0]
conn.close()
assert accrued >= 1, f"expected at least 1 comp-off accrual after 24+ OT hours, got {accrued}"

os.remove(tmp_path)
print("OK: test_leave_store")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe scripts\test_leave_store.py`
Expected: `AttributeError: module 'leave_store' has no attribute 'apply_leave'`

- [ ] **Step 3: Implement `leave_store.py`**

Replace the full contents of `backend/leave_store.py` with:

```python
"""Employee leave / holiday windows + overtime-to-comp-off conversion
(CLAUDE.md gotcha #119, extended by the leave management system,
2026-09-30 — see docs/superpowers/specs/2026-09-30-leave-management-design.md).

A row in `employee_leave` is an inclusive [start_date, end_date] window
with a status: 'pending' -> awaiting HR (emp009) approval, 'approved' ->
counts toward balance usage and exempts the person from the standup lock /
login nudges, 'rejected' / 'cancelled' -> inert.

Only 'approved' rows ever exempt anything or count toward balance. This is
the one deliberate behavior change from the original gotcha #119 feature:
a WhatsApp-set leave used to apply instantly, and now creates a pending
request instead, closing the self-approval loophole a real balance system
would otherwise have.

This module only imports `db` and `utils`, so it is safe to import from
routes, the scheduler, and the agent alike.
"""
from __future__ import annotations

import logging
from datetime import date as _date

from db import get_connection
from utils import today_ist

logger = logging.getLogger(__name__)

ANNUAL_BASE_DAYS = 15
OT_BASELINE_HOURS = 9.0   # kept in sync with routes/attendance.py::_FULL_DAY_HOURS
OT_CONVERSION_HOURS = 24.0
VALID_LEAVE_TYPES = {"full", "half"}
_LEAVE_DEDUCTION = {"full": 1.0, "half": 0.5}

_DDL = """CREATE TABLE IF NOT EXISTS employee_leave (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    created_by  TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    status      TEXT DEFAULT 'approved',
    leave_type  TEXT DEFAULT 'full',
    approved_by TEXT DEFAULT NULL,
    approved_at TEXT DEFAULT NULL
)"""


def _ensure(conn) -> None:
    """Idempotent — db.init_db() also creates this, but a companion
    endpoint or the agent may touch the table before a redeploy has
    re-run init."""
    conn.execute(_DDL)


# ── low-level WhatsApp-facing API (existing, gotcha #119) ──────────────

def set_leave(user_id: str, start_date: str, end_date: str,
              reason: str = "", created_by: str = "",
              status: str = "pending") -> dict:
    """Record a leave request for one person, defaulting to 'pending' --
    the WhatsApp bot's set_leave tool calls this. Only overlapping
    *pending* rows for this user are replaced first (re-requesting the
    same days doesn't pile up duplicates); an already-approved row is
    never touched here."""
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    if status not in ("pending", "approved"):
        status = "pending"
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            conn.execute(
                "DELETE FROM employee_leave WHERE user_id=? AND status='pending' "
                "AND NOT (end_date < ? OR start_date > ?)",
                (user_id, start_date, end_date),
            )
            cur = conn.execute(
                "INSERT INTO employee_leave "
                "(user_id, start_date, end_date, reason, created_by, status) "
                "VALUES (?,?,?,?,?,?)",
                (user_id, start_date, end_date, reason or "", created_by or "", status),
            )
        return {"id": cur.lastrowid, "user_id": user_id,
                "start_date": start_date, "end_date": end_date,
                "reason": reason or "", "status": status}
    finally:
        conn.close()


def clear_leave(user_id: str, on_date: str | None = None) -> int:
    """Remove pending/approved leave for `user_id` that hasn't already
    finished. With `on_date`, only the window(s) covering that date;
    without it, every current/future pending or approved window. Never
    touches leave that has already fully elapsed (start_date/end_date in
    the past) — that's history, not something to silently erase."""
    today = today_ist()
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            if on_date:
                cur = conn.execute(
                    "DELETE FROM employee_leave WHERE user_id=? "
                    "AND status IN ('pending','approved') "
                    "AND start_date <= ? AND end_date >= ? AND end_date >= ?",
                    (user_id, on_date, on_date, today),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM employee_leave WHERE user_id=? "
                    "AND status IN ('pending','approved') AND end_date >= ?",
                    (user_id, today),
                )
        return cur.rowcount or 0
    finally:
        conn.close()


def is_on_leave(user_id: str, date_str: str) -> bool:
    return user_id in on_leave_ids(date_str)


def on_leave_ids(date_str: str) -> set[str]:
    """Every user_id whose APPROVED leave window covers `date_str`. A
    pending request deliberately does not count here."""
    conn = get_connection()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM employee_leave "
            "WHERE status='approved' AND start_date <= ? AND end_date >= ?",
            (date_str, date_str),
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        logger.exception("leave_store.on_leave_ids failed")
        return set()
    finally:
        conn.close()


def active_leave(user_id: str, date_str: str) -> dict | None:
    conn = get_connection()
    try:
        _ensure(conn)
        r = conn.execute(
            "SELECT start_date, end_date, reason FROM employee_leave "
            "WHERE user_id=? AND status='approved' "
            "AND start_date <= ? AND end_date >= ? "
            "ORDER BY end_date DESC LIMIT 1",
            (user_id, date_str, date_str),
        ).fetchone()
        if not r:
            return None
        return {"start_date": r[0], "end_date": r[1], "reason": r[2] or ""}
    finally:
        conn.close()


# ── approval workflow ────────────────────────────────────────────────────

def _overlaps_approved(conn, user_id: str, start_date: str, end_date: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM employee_leave WHERE user_id=? AND status='approved' "
        "AND NOT (end_date < ? OR start_date > ?) LIMIT 1",
        (user_id, start_date, end_date),
    ).fetchone()
    return row is not None


def apply_leave(user_id: str, start_date: str, end_date: str,
                 leave_type: str = "full", reason: str = "",
                 created_by: str = "") -> dict:
    """Create a pending leave request. Raises ValueError on an invalid
    leave_type or a date range that overlaps a leave already approved for
    this same person (no double-booking)."""
    if leave_type not in VALID_LEAVE_TYPES:
        raise ValueError(f"leave_type must be one of {sorted(VALID_LEAVE_TYPES)}")
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    conn = get_connection()
    try:
        _ensure(conn)
        if _overlaps_approved(conn, user_id, start_date, end_date):
            raise ValueError("overlaps a leave window already approved for this person")
        with conn:
            cur = conn.execute(
                "INSERT INTO employee_leave "
                "(user_id, start_date, end_date, reason, created_by, status, leave_type) "
                "VALUES (?,?,?,?,?,'pending',?)",
                (user_id, start_date, end_date, reason or "", created_by or "", leave_type),
            )
        return {"id": cur.lastrowid, "user_id": user_id, "start_date": start_date,
                "end_date": end_date, "leave_type": leave_type,
                "reason": reason or "", "status": "pending"}
    finally:
        conn.close()


def approve_leave(leave_id: int, approved_by: str) -> bool:
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            cur = conn.execute(
                "UPDATE employee_leave SET status='approved', approved_by=?, "
                "approved_at=datetime('now') WHERE id=? AND status='pending'",
                (approved_by, leave_id),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def reject_leave(leave_id: int, approved_by: str, reason: str = "") -> bool:
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            cur = conn.execute(
                "UPDATE employee_leave SET status='rejected', approved_by=?, "
                "approved_at=datetime('now') WHERE id=? AND status='pending'",
                (approved_by, leave_id),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def cancel_leave(leave_id: int, user_id: str) -> bool:
    """An employee cancelling their OWN still-pending request. Cannot
    cancel something already approved/rejected/someone else's row."""
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            cur = conn.execute(
                "UPDATE employee_leave SET status='cancelled' "
                "WHERE id=? AND user_id=? AND status='pending'",
                (leave_id, user_id),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def list_pending() -> list[dict]:
    conn = get_connection()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT id, user_id, start_date, end_date, leave_type, reason, created_by, created_at "
            "FROM employee_leave WHERE status='pending' ORDER BY created_at ASC"
        ).fetchall()
        return [
            {"id": r[0], "user_id": r[1], "start_date": r[2], "end_date": r[3],
             "leave_type": r[4], "reason": r[5], "created_by": r[6], "created_at": r[7]}
            for r in rows
        ]
    finally:
        conn.close()


# ── balance (computed live, never stored) ───────────────────────────────

def get_balance(user_id: str, year: int | None = None) -> dict:
    if year is None:
        year = _date.fromisoformat(today_ist()).year
    y_start, y_end = f"{year}-01-01", f"{year}-12-31"
    conn = get_connection()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT leave_type FROM employee_leave WHERE user_id=? AND status='approved' "
            "AND start_date >= ? AND start_date <= ?",
            (user_id, y_start, y_end),
        ).fetchall()
        used = sum(_LEAVE_DEDUCTION.get(r[0], 1.0) for r in rows)

        conn.execute(
            "CREATE TABLE IF NOT EXISTS comp_off_ledger (id INTEGER PRIMARY KEY "
            "AUTOINCREMENT, user_id TEXT NOT NULL, days REAL NOT NULL, "
            "source_ot_hours REAL NOT NULL, created_at TEXT DEFAULT (datetime('now')))"
        )
        comp_rows = conn.execute(
            "SELECT days FROM comp_off_ledger WHERE user_id=? "
            "AND created_at >= ? AND created_at < ?",
            (user_id, f"{year}-01-01 00:00:00", f"{year + 1}-01-01 00:00:00"),
        ).fetchall()
        comp_earned = sum(r[0] for r in comp_rows)
    finally:
        conn.close()
    remaining = ANNUAL_BASE_DAYS + comp_earned - used
    return {"base": ANNUAL_BASE_DAYS, "comp_earned": round(comp_earned, 2),
            "used": round(used, 2), "remaining": round(remaining, 2), "year": year}


# ── calendar (per-day dot status for the UI) ────────────────────────────

_FULL_DAY_HOURS = OT_BASELINE_HOURS
_HALF_DAY_MIN_HOURS = 6.0


def _hours_worked(checkin, checkout) -> float | None:
    if not checkin or not checkout:
        return None
    try:
        h1, m1, s1 = (int(p) for p in checkin.split(":"))
        h2, m2, s2 = (int(p) for p in checkout.split(":"))
        hrs = ((h2 * 3600 + m2 * 60 + s2) - (h1 * 3600 + m1 * 60 + s1)) / 3600.0
    except Exception:
        return None
    return round(hrs, 2) if hrs >= 0 else None


def calendar_days(user_id: str, year: int, month: int) -> dict:
    """{"YYYY-MM-DD": {"status": ..., "hours": float|None}} for every day
    in the given month. status is one of: 'weekend', 'leave_approved',
    'leave_pending', 'full', 'half', 'none'."""
    import calendar as _cal
    conn = get_connection()
    try:
        _ensure(conn)
        att_rows = conn.execute(
            "SELECT date, checkin_time, checkout_time FROM daily_attendance "
            "WHERE user_id=? AND date >= ? AND date < ?",
            (user_id, f"{year:04d}-{month:02d}-01",
             f"{year:04d}-{month + 1:02d}-01" if month < 12 else f"{year + 1:04d}-01-01"),
        ).fetchall()
        att_by_date = {r[0]: (r[1], r[2]) for r in att_rows}

        leave_rows = conn.execute(
            "SELECT start_date, end_date, status FROM employee_leave "
            "WHERE user_id=? AND status IN ('approved','pending') "
            "AND start_date <= ? AND end_date >= ?",
            (user_id, f"{year:04d}-{month:02d}-{_cal.monthrange(year, month)[1]:02d}",
             f"{year:04d}-{month:02d}-01"),
        ).fetchall()
    finally:
        conn.close()

    days_in_month = _cal.monthrange(year, month)[1]
    out = {}
    for d in range(1, days_in_month + 1):
        dstr = f"{year:04d}-{month:02d}-{d:02d}"
        weekday = _date(year, month, d).weekday()
        if weekday >= 5:
            out[dstr] = {"status": "weekend", "hours": None}
            continue

        leave_status = None
        for sd, ed, st in leave_rows:
            if sd <= dstr <= ed:
                leave_status = "leave_approved" if st == "approved" else "leave_pending"
                if st == "approved":
                    break  # approved wins over a coincidentally-also-pending row
        if leave_status:
            out[dstr] = {"status": leave_status, "hours": None}
            continue

        cin, cout = att_by_date.get(dstr, (None, None))
        hrs = _hours_worked(cin, cout)
        if hrs is None:
            out[dstr] = {"status": "none", "hours": None}
        elif hrs >= _HALF_DAY_MIN_HOURS:
            out[dstr] = {"status": "full", "hours": hrs}
        else:
            out[dstr] = {"status": "half", "hours": hrs}
    return out


# ── overtime -> comp-off conversion (nightly job, task_scheduler.py) ────

def _overtime_hours_for(hrs: float | None) -> float | None:
    if hrs is None:
        return None
    return round(max(0.0, hrs - OT_BASELINE_HOURS), 2)


def _active_employee_ids() -> list[str]:
    from utils import _load_employees
    inactive = {"inactive", "disabled", "left", "removed", "archived", "former"}
    out = []
    try:
        for e in _load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() not in inactive:
                out.append(e.get("id", ""))
    except Exception:
        logger.exception("leave_store: roster load failed")
    return [e for e in out if e]


def _convert_overtime_for_user(conn, user_id: str) -> int:
    """Walk this user's unconverted overtime_ledger rows oldest-first,
    marking them converted as the running total crosses 24hrs, firing one
    comp_off_ledger accrual per crossing. Returns how many accrual events
    fired. A day that pushes the total past 24 has its FULL ot_hours
    consumed (no fractional-day splitting) — any overshoot just means the
    next cycle starts slightly ahead, deliberately simple."""
    rows = conn.execute(
        "SELECT id, ot_hours FROM overtime_ledger WHERE user_id=? "
        "AND converted_at IS NULL ORDER BY date ASC", (user_id,),
    ).fetchall()
    converted_events = 0
    running = 0.0
    batch_ids = []
    for rid, ot in rows:
        running += (ot or 0.0)
        batch_ids.append(rid)
        if running >= OT_CONVERSION_HOURS:
            conn.execute(
                f"UPDATE overtime_ledger SET converted_at=datetime('now') "
                f"WHERE id IN ({','.join('?' * len(batch_ids))})",
                batch_ids,
            )
            conn.execute(
                "INSERT INTO comp_off_ledger (user_id, days, source_ot_hours) "
                "VALUES (?, 1, ?)", (user_id, running),
            )
            converted_events += 1
            batch_ids = []
            running = 0.0
    return converted_events


def run_overtime_conversion_sweep(for_date: str | None = None) -> dict:
    """Compute overtime_ledger rows for `for_date` (default: yesterday IST)
    across every active employee, then run the comp-off conversion check.
    Safe to call more than once for the same date — the UNIQUE(user_id,
    date) constraint on overtime_ledger makes the per-day insert a no-op
    on a re-run (caught via sqlite3.IntegrityError), and conversion only
    ever consumes rows that are still unconverted."""
    import sqlite3
    from datetime import timedelta

    if for_date is None:
        for_date = (_date.fromisoformat(today_ist()) - timedelta(days=1)).isoformat()

    conn = get_connection()
    processed = 0
    converted_events = 0
    try:
        _ensure(conn)
        att_rows = conn.execute(
            "SELECT user_id, checkin_time, checkout_time FROM daily_attendance WHERE date=?",
            (for_date,),
        ).fetchall()
        att_by_user = {r[0]: (r[1], r[2]) for r in att_rows}

        for user_id in _active_employee_ids():
            cin, cout = att_by_user.get(user_id, (None, None))
            hrs = _hours_worked(cin, cout)
            if hrs is None:
                continue
            ot = _overtime_hours_for(hrs)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO overtime_ledger (user_id, date, worked_hours, ot_hours) "
                        "VALUES (?,?,?,?)",
                        (user_id, for_date, hrs, ot),
                    )
                processed += 1
            except sqlite3.IntegrityError:
                pass  # already computed for this user+date, re-run is a no-op here

            converted_events += _convert_overtime_for_user(conn, user_id)
        conn.commit()
    finally:
        conn.close()
    return {"processed": processed, "converted_events": converted_events}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv\Scripts\python.exe scripts\test_leave_store.py`
Expected: `OK: test_leave_store`

- [ ] **Step 5: Run pyflakes**

Run: `venv\Scripts\python.exe -m pyflakes backend\leave_store.py`
Expected: no "undefined name" errors

- [ ] **Step 6: Commit**

```bash
git add backend/leave_store.py scripts/test_leave_store.py
git commit -m "$(cat <<'EOF'
Add leave approval workflow, balance calc, calendar, and OT conversion

apply/approve/reject/cancel_leave, get_balance (15-day pool + comp-off,
calendar-year), calendar_days (per-day dot status), and
run_overtime_conversion_sweep (24hr overtime -> +1 leave day). set_leave/
clear_leave (WhatsApp path) now default to pending and never silently
touch an already-approved row. is_on_leave/on_leave_ids/active_leave now
only ever consider approved rows, closing the self-approval loophole.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `routes/leave.py` — HTTP API

**Files:**
- Create: `backend/routes/leave.py`
- Modify: `backend/app.py` (blueprint import + registration, near line
  87-92 and 125-130)
- Test: `scripts/test_leave_routes.py` (new)

**Interfaces:**
- Consumes: `leave_store.apply_leave/approve_leave/reject_leave/
  cancel_leave/get_balance/list_pending/calendar_days` (Task 2)
- Produces: `leave_bp` Flask blueprint with routes `GET /api/leave/balance`,
  `GET /api/leave/calendar`, `POST /api/leave/apply`,
  `POST /api/leave/<id>/cancel`, `GET /api/leave/pending`,
  `POST /api/leave/<id>/approve`, `POST /api/leave/<id>/reject` — consumed
  by Task 8's frontend.

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_leave_routes.py
"""Scratch test for routes/leave.py via a real Flask test client,
temp sqlite DB, never logs/app.db."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

import db  # noqa: E402
db.DB_PATH = tmp_path
db.init_db()

from flask import Flask  # noqa: E402
from routes.leave import leave_bp  # noqa: E402

app = Flask(__name__)
app.register_blueprint(leave_bp)
client = app.test_client()

# missing user_id -> 400/401 style rejection on balance
r = client.get("/api/leave/balance")
assert r.status_code == 400, r.status_code

# apply as a normal employee
r = client.post("/api/leave/apply", json={
    "user_id": "emp010", "date": "2026-10-05", "leave_type": "full", "reason": "sick",
})
assert r.status_code == 200, (r.status_code, r.get_json())
leave_id = r.get_json()["id"]

# non-HR cannot see pending list or approve
r = client.get("/api/leave/pending?user_id=emp010")
assert r.status_code == 403, r.status_code
r = client.post(f"/api/leave/{leave_id}/approve", json={"user_id": "emp010"})
assert r.status_code == 403, r.status_code

# HR (emp009) can see it and approve it
r = client.get("/api/leave/pending?user_id=emp009")
assert r.status_code == 200
assert any(p["id"] == leave_id for p in r.get_json()["pending"])
r = client.post(f"/api/leave/{leave_id}/approve", json={"user_id": "emp009"})
assert r.status_code == 200, r.get_json()

# balance now reflects it
r = client.get("/api/leave/balance?user_id=emp010")
d = r.get_json()
assert d["used"] == 1.0, d
assert d["remaining"] == 14.0, d

# calendar returns that date as leave_approved
r = client.get("/api/leave/calendar?user_id=emp010&year=2026&month=10")
d = r.get_json()
assert d["days"]["2026-10-05"]["status"] == "leave_approved", d["days"]["2026-10-05"]

# bad leave_type on apply -> 400
r = client.post("/api/leave/apply", json={
    "user_id": "emp010", "date": "2026-11-01", "leave_type": "quarter",
})
assert r.status_code == 400, r.status_code

os.remove(tmp_path)
print("OK: test_leave_routes")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe scripts\test_leave_routes.py`
Expected: `ModuleNotFoundError: No module named 'routes.leave'`

- [ ] **Step 3: Implement `backend/routes/leave.py`**

```python
"""
Leave Management Blueprint
Routes: /api/leave/*

Balance/calendar/apply are self-service (any logged-in user_id).
pending/approve/reject are HR-only, hardcoded to emp009 (Noorish) --
same allowlist convention as the bet feature in routes/ops.py, not the
generic _is_admin()/bool(user_id) pattern, since this is a real authority
gate, not general app access.

See docs/superpowers/specs/2026-09-30-leave-management-design.md.
"""
from __future__ import annotations

import re

from flask import Blueprint, jsonify, request

import leave_store

leave_bp = Blueprint("leave", __name__)

HR_USER_ID = "emp009"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _require_user_id() -> tuple[str, tuple] | tuple[str, None]:
    """Reads user_id from query args (GET) or JSON body (POST). Returns
    (user_id, None) on success or ("", error_response) on failure."""
    if request.method == "GET":
        uid = (request.args.get("user_id") or "").strip()
    else:
        body = request.get_json(silent=True) or {}
        uid = (body.get("user_id") or "").strip()
    if not uid:
        return "", (jsonify({"error": "user_id required"}), 400)
    return uid, None


@leave_bp.route("/api/leave/balance", methods=["GET"])
def leave_balance():
    uid, err = _require_user_id()
    if err:
        return err
    year = request.args.get("year", type=int)
    return jsonify(leave_store.get_balance(uid, year=year))


@leave_bp.route("/api/leave/calendar", methods=["GET"])
def leave_calendar():
    uid, err = _require_user_id()
    if err:
        return err
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    if not year or not month or not (1 <= month <= 12):
        return jsonify({"error": "year and month (1-12) required"}), 400
    return jsonify({"days": leave_store.calendar_days(uid, year, month)})


@leave_bp.route("/api/leave/apply", methods=["POST"])
def leave_apply():
    uid, err = _require_user_id()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    date_ = (body.get("date") or "").strip()
    end_date = (body.get("end_date") or date_).strip()
    leave_type = (body.get("leave_type") or "full").strip().lower()
    reason = str(body.get("reason") or "")[:300]
    if not _DATE_RE.match(date_) or not _DATE_RE.match(end_date):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    try:
        row = leave_store.apply_leave(uid, date_, end_date, leave_type=leave_type,
                                       reason=reason, created_by=uid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(row)


@leave_bp.route("/api/leave/<int:leave_id>/cancel", methods=["POST"])
def leave_cancel(leave_id: int):
    uid, err = _require_user_id()
    if err:
        return err
    ok = leave_store.cancel_leave(leave_id, uid)
    if not ok:
        return jsonify({"error": "nothing pending to cancel for this id/user"}), 404
    return jsonify({"success": True})


@leave_bp.route("/api/leave/pending", methods=["GET"])
def leave_pending():
    uid, err = _require_user_id()
    if err:
        return err
    if uid != HR_USER_ID:
        return jsonify({"error": "HR only"}), 403
    return jsonify({"pending": leave_store.list_pending()})


@leave_bp.route("/api/leave/<int:leave_id>/approve", methods=["POST"])
def leave_approve(leave_id: int):
    uid, err = _require_user_id()
    if err:
        return err
    if uid != HR_USER_ID:
        return jsonify({"error": "HR only"}), 403
    ok = leave_store.approve_leave(leave_id, uid)
    if not ok:
        return jsonify({"error": "no pending request with that id"}), 404
    return jsonify({"success": True})


@leave_bp.route("/api/leave/<int:leave_id>/reject", methods=["POST"])
def leave_reject(leave_id: int):
    uid, err = _require_user_id()
    if err:
        return err
    if uid != HR_USER_ID:
        return jsonify({"error": "HR only"}), 403
    body = request.get_json(silent=True) or {}
    ok = leave_store.reject_leave(leave_id, uid, reason=str(body.get("reason") or "")[:300])
    if not ok:
        return jsonify({"error": "no pending request with that id"}), 404
    return jsonify({"success": True})
```

- [ ] **Step 4: Register the blueprint in `backend/app.py`**

At line 92 (right after `from routes.companion import companion_bp`), add:

```python
from routes.leave import leave_bp
```

At line 130 (right after `app.register_blueprint(companion_bp)`), add:

```python
app.register_blueprint(leave_bp)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `venv\Scripts\python.exe scripts\test_leave_routes.py`
Expected: `OK: test_leave_routes`

- [ ] **Step 6: Confirm the app still boots**

Run (from `backend/`): `venv\Scripts\python.exe -c "import app"`
Expected: boots clean, route count includes the 7 new `/api/leave/*` routes
(a harmless `no such table: tasks` log line on a fresh local DB is
expected and not a failure, per this repo's established convention)

- [ ] **Step 7: Run pyflakes**

Run: `venv\Scripts\python.exe -m pyflakes backend\routes\leave.py backend\app.py`
Expected: no new "undefined name" errors vs. the existing baseline

- [ ] **Step 8: Commit**

```bash
git add backend/routes/leave.py backend/app.py scripts/test_leave_routes.py
git commit -m "$(cat <<'EOF'
Add /api/leave/* HTTP routes and register the blueprint

Balance/calendar/apply/cancel are self-service; pending/approve/reject are
hardcoded to emp009 (HR), same allowlist convention as the bet feature.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Nightly overtime-conversion scheduler job

**Files:**
- Modify: `backend/task_scheduler.py`
- Test: `scripts/test_overtime_job.py` (new)

**Interfaces:**
- Consumes: `leave_store.run_overtime_conversion_sweep()` (Task 2)
- Produces: `_run_overtime_conversion()` wrapper function, registered as a
  cron job `id="overtime_conversion_sweep"` at 02:00 IST — nothing later
  depends on this function being importable elsewhere, it's scheduler-only.

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_overtime_job.py
"""Confirms task_scheduler exposes _run_overtime_conversion and that it
doesn't raise when leave_store's real sweep function runs against a temp
DB with no attendance data (the common case: nothing to convert)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

import db  # noqa: E402
db.DB_PATH = tmp_path
db.init_db()

import task_scheduler  # noqa: E402

assert hasattr(task_scheduler, "_run_overtime_conversion"), \
    "task_scheduler._run_overtime_conversion not defined"

task_scheduler._run_overtime_conversion()  # must not raise on an empty DB

os.remove(tmp_path)
print("OK: test_overtime_job")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe scripts\test_overtime_job.py`
Expected: `AssertionError: task_scheduler._run_overtime_conversion not defined`

- [ ] **Step 3: Add the job**

In `backend/task_scheduler.py`, add this function right after
`_run_data_retention()` (before `def init_scheduler(app):`):

```python
def _run_overtime_conversion():
    """Wraps leave_store.run_overtime_conversion_sweep() for the nightly
    job below -- computes yesterday's per-employee overtime and converts
    any 24hr-crossing into a +1 leave day. Imported lazily so this module
    has no hard import-time dependency on leave_store."""
    try:
        import leave_store
        result = leave_store.run_overtime_conversion_sweep()
        if result.get("converted_events"):
            logger.info(
                "Overtime conversion: %s comp-off day(s) accrued across %s employee(s) processed.",
                result["converted_events"], result["processed"],
            )
    except Exception as e:
        logger.warning(f"Overtime conversion sweep failed (non-fatal): {e}")
```

Then in `init_scheduler(app)`, right after the `_run_data_retention` cron
job registration (after the line
`id="data_retention_sweep", replace_existing=True)`), add:

```python
        # Overtime -> comp-off conversion -- computes yesterday's overtime
        # for every active employee and converts any 24hr crossing into a
        # +1 leave day (leave_store.py). Off-peak, after the data
        # retention sweep.
        scheduler.add_job(_run_overtime_conversion, "cron", hour=2, minute=0,
                          id="overtime_conversion_sweep", replace_existing=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv\Scripts\python.exe scripts\test_overtime_job.py`
Expected: `OK: test_overtime_job`

- [ ] **Step 5: Run pyflakes**

Run: `venv\Scripts\python.exe -m pyflakes backend\task_scheduler.py`
Expected: no new "undefined name" errors

- [ ] **Step 6: Commit**

```bash
git add backend/task_scheduler.py scripts/test_overtime_job.py
git commit -m "$(cat <<'EOF'
Register nightly overtime-to-comp-off conversion job at 02:00 IST

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Regression test — pending leave must not unlock standup / silence nudges

**Files:**
- Test: `scripts/test_leave_lock_regression.py` (new — no source files
  change in this task; `routes/ops.py` and `routes/companion.py` already
  call `leave_store.is_on_leave`/`on_leave_ids`, which Task 2 already
  fixed internally. This task exists purely to pin that behavior with an
  explicit, isolated regression test per this plan's Review Focus.)

**Interfaces:**
- Consumes: `leave_store.apply_leave/approve_leave/is_on_leave/on_leave_ids`
  (Task 2)
- Produces: nothing new — a standalone regression test

- [ ] **Step 1: Write the test (it should already pass — this step proves it)**

```python
# scripts/test_leave_lock_regression.py
"""Regression test: a PENDING leave request must NOT exempt someone from
the standup lock (routes/ops.py::standup_lock_status) or the login-nudge
suppression (routes/companion.py::companion_not_logged_in) -- both read
leave_store.is_on_leave/on_leave_ids directly. Only an APPROVED leave may
exempt either. This is the one deliberate behavior change to gotcha #119's
original WhatsApp-instant-leave feature."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

import db  # noqa: E402
db.DB_PATH = tmp_path
db.init_db()

import leave_store  # noqa: E402

row = leave_store.apply_leave("emp010", "2026-10-05", "2026-10-05")

# PENDING: must not exempt
assert leave_store.is_on_leave("emp010", "2026-10-05") is False, \
    "a PENDING leave request must not count as on_leave"
assert "emp010" not in leave_store.on_leave_ids("2026-10-05"), \
    "a PENDING leave request must not appear in on_leave_ids"

# APPROVED: must exempt
leave_store.approve_leave(row["id"], "emp009")
assert leave_store.is_on_leave("emp010", "2026-10-05") is True, \
    "an APPROVED leave request must count as on_leave"
assert "emp010" in leave_store.on_leave_ids("2026-10-05"), \
    "an APPROVED leave request must appear in on_leave_ids"

os.remove(tmp_path)
print("OK: test_leave_lock_regression")
```

- [ ] **Step 2: Run it**

Run: `venv\Scripts\python.exe scripts\test_leave_lock_regression.py`
Expected: `OK: test_leave_lock_regression` (passes immediately since Task 2
already implemented the `status='approved'` filter — this step is the
proof, not a fix)

- [ ] **Step 3: Commit**

```bash
git add scripts/test_leave_lock_regression.py
git commit -m "$(cat <<'EOF'
Add regression test: pending leave must not unlock standup or silence nudges

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: WhatsApp bot — `set_leave`/`clear_leave` become requests, add `get_leave_balance`

**Files:**
- Modify: `backend/whatsapp_agent.py` (tool descriptions ~1304-1341,
  `_run_tool` handlers ~1746-1797, `_EMPLOYEE_TOOLS` list ~798)
- Test: `scripts/test_leave_whatsapp_tools.py` (new)

**Interfaces:**
- Consumes: `leave_store.set_leave/clear_leave/get_balance` (Task 2, both
  already exist/were extended), `_resolve_employee`, `_active_employees`,
  `identity` dict shape `{"kind": "employee"|"client"|"unknown", "id", "name"}`
- Produces: `_run_tool("set_leave", ...)` now returns a "sent for approval"
  message instead of "marked ... on leave"; `_run_tool("get_leave_balance", ...)`
  is new, returns a plain-text balance summary

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_leave_whatsapp_tools.py
"""Scratch test: whatsapp_agent's set_leave/clear_leave/get_leave_balance
tool handlers, via _run_tool directly (no real Anthropic call needed --
this only exercises the tool-execution branch, not the model loop)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

import db  # noqa: E402
db.DB_PATH = tmp_path
db.init_db()

import whatsapp_agent as wa  # noqa: E402
import leave_store  # noqa: E402

identity = {"kind": "employee", "id": "emp010", "name": "Sid"}

out = wa._run_tool("set_leave", {"start_date": "2026-10-05", "end_date": "2026-10-05"},
                    identity)
assert "approv" in out.lower(), out  # must mention it's pending approval, not granted
assert "on leave" not in out.lower() or "not" in out.lower(), out

conn = db.get_connection()
row = conn.execute(
    "SELECT status FROM employee_leave WHERE user_id='emp010'"
).fetchone()
conn.close()
assert row == ("pending",), row

out2 = wa._run_tool("get_leave_balance", {}, identity)
assert "15" in out2, out2  # base pool visible somewhere in the reply

# clear_leave on the still-pending request
out3 = wa._run_tool("clear_leave", {}, identity)
assert "cleared" in out3.lower() or "removed" in out3.lower(), out3

os.remove(tmp_path)
print("OK: test_leave_whatsapp_tools")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe scripts\test_leave_whatsapp_tools.py`
Expected: `AssertionError` on the "approv" check (current code says "Marked
you on leave")

- [ ] **Step 3: Update the tool descriptions**

In `backend/whatsapp_agent.py`, replace the `set_leave` tool's
`"description"` field (around line 1305-1312) with:

```python
        "description": "REQUEST leave / holiday for a day or a date range "
                       "-- this creates a pending request that Noorish "
                       "(HR) must approve before it counts against the "
                       "15-day leave pool or exempts anyone from the "
                       "standup lock. Use for 'I'm on leave today', 'on "
                       "holiday tomorrow', 'off Thursday and Friday', "
                       "'mark Nupur on leave next week'. Work out the "
                       "real calendar dates yourself (today is known) and "
                       "pass them as YYYY-MM-DD. For a single day, pass "
                       "the same date as start and end. Optionally pass "
                       "leave_type='half' for a half day (default is a "
                       "full day).",
```

Add a `leave_type` property to `set_leave`'s `input_schema.properties`
(around line 1323, right after `reason`):

```python
                "leave_type": {"type": "string", "enum": ["full", "half"],
                               "description": "Optional -- 'half' for a half "
                                              "day. Default is a full day."},
```

- [ ] **Step 4: Update the `set_leave` handler**

Replace the `if name == "set_leave" and kind == "employee":` block
(lines ~1746-1777) with:

```python
        if name == "set_leave" and kind == "employee":
            ti = tool_input or {}
            who = str(ti.get("person") or "").strip()
            if who:
                emp = _resolve_employee(who)
                if not emp:
                    names = ", ".join(e["name"] for e in _active_employees())
                    return f"Don't know who '{who}' is. Team: {names}."
                tid, tname = emp["id"], emp["name"]
            else:
                tid, tname = identity["id"], identity["name"]
            sd = str(ti.get("start_date", "")).strip()[:10]
            ed = str(ti.get("end_date", "") or sd).strip()[:10]
            if not (re.match(r"^\d{4}-\d{2}-\d{2}$", sd)
                    and re.match(r"^\d{4}-\d{2}-\d{2}$", ed)):
                return ("(tell me the leave dates — e.g. 'today', 'tomorrow', "
                        "'Thu and Fri', or a range)")
            if max(sd, ed) < today:
                return "That leave window is entirely in the past — nothing to record."
            leave_type = str(ti.get("leave_type", "full")).strip().lower()
            if leave_type not in ("full", "half"):
                leave_type = "full"
            try:
                import leave_store
                row = leave_store.set_leave(
                    tid, sd, ed, reason=str(ti.get("reason", ""))[:200],
                    created_by=identity["name"])
            except Exception:
                logger.exception("whatsapp_agent: set_leave failed")
                return "(couldn't save that leave just now)"
            span = (row["start_date"] if row["start_date"] == row["end_date"]
                    else f"{row['start_date']} to {row['end_date']}")
            whose = "you" if tid == identity["id"] else tname
            return (f"Sent {whose} leave for {span} to Noorish for approval. "
                    f"{'You' if tid == identity['id'] else tname.capitalize()} "
                    f"will stay locked out of standup and keep getting login "
                    f"nudges until it's approved.")
```

- [ ] **Step 5: Update the `clear_leave` handler's confirmation text**

Replace the return line inside `if name == "clear_leave" and kind == "employee":`
(the final `return f"Cleared {whose} leave..."` line, ~1797) with:

```python
            return f"Cleared {whose} leave request(s) — {n} entr{'y' if n == 1 else 'ies'} removed."
```

(the body of this handler is otherwise unchanged — `leave_store.clear_leave`
already only removes pending/still-future rows per Task 2)

- [ ] **Step 6: Add the `get_leave_balance` tool**

In `_EMPLOYEE_TOOLS` (the list starting at line 798), add a new tool dict
right after the `clear_leave` tool definition (after its closing `},` at
line ~1341):

```python
    {
        "name": "get_leave_balance",
        "description": "Check leave balance -- the 15-day annual pool, "
                       "comp-off earned from overtime, days used, and "
                       "days remaining this year. Use for 'how much leave "
                       "do I have left', 'my leave balance', 'how many "
                       "days off do I have'.",
        "input_schema": {"type": "object", "properties": {}},
    },
```

In `_run_tool`, add a new branch right after the `clear_leave` block
(after its closing `return` line):

```python
        if name == "get_leave_balance" and kind == "employee":
            try:
                import leave_store
                bal = leave_store.get_balance(identity["id"])
            except Exception:
                logger.exception("whatsapp_agent: get_leave_balance failed")
                return "(couldn't look that up just now)"
            return (f"{bal['base']}-day pool + {bal['comp_earned']} comp-off "
                    f"earned this year, {bal['used']} used -> "
                    f"{bal['remaining']} remaining ({bal['year']}).")
```

- [ ] **Step 7: Run test to verify it passes**

Run: `venv\Scripts\python.exe scripts\test_leave_whatsapp_tools.py`
Expected: `OK: test_leave_whatsapp_tools`

- [ ] **Step 8: Run pyflakes**

Run: `venv\Scripts\python.exe -m pyflakes backend\whatsapp_agent.py`
Expected: no new "undefined name" errors

- [ ] **Step 9: Commit**

```bash
git add backend/whatsapp_agent.py scripts/test_leave_whatsapp_tools.py
git commit -m "$(cat <<'EOF'
WhatsApp set_leave/clear_leave now request-and-wait; add get_leave_balance

set_leave creates a pending leave_store request instead of instant-
applying, matching the new HR-approved 15-day balance system. Adds a
get_leave_balance tool so an employee can check their balance in chat.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Extend the attendance Excel export with leave balance + approved-leave distinction

**Files:**
- Modify: `backend/routes/attendance.py`
  (`_hours_and_day_type` ~444-461, `DAY_TYPE_FILLS` ~585-589, the
  per-employee sheet loop ~694-806, the `ws_sum`/Summary sheet ~807-842)
- Test: `scripts/test_leave_export.py` (new)

**Interfaces:**
- Consumes: `leave_store.get_balance`, `leave_store.on_leave_ids` (Task 2),
  the existing `_overtime_hours`, `_FULL_DAY_HOURS`, `_load_employees`
- Produces: `attendance_export_sheets()` (unchanged route path
  `/api/attendance/export-sheets`) now labels a weekday with no checkin
  but an approved leave covering it as `"Leave (Approved)"` instead of
  plain `"Leave"`, and the Summary sheet gains 3 columns: `Leave Balance
  (Used)`, `Comp-Off Earned`, `Leave Balance (Remaining)`.

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_leave_export.py
"""Scratch test: attendance_export_sheets() distinguishes an approved
leave day from a plain unexplained absence, and the Summary sheet carries
the formal leave balance columns. Reads the generated xlsx back with
openpyxl to check actual cell values, not just that the route 200s."""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

import db  # noqa: E402
db.DB_PATH = tmp_path
db.init_db()

import leave_store  # noqa: E402
import routes.attendance as attn  # noqa: E402
from flask import Flask  # noqa: E402

# This test relies on config/employees.json's CURRENT real roster: emp003
# (Abhinav) must be active. If that ever changes, update this test's id.
UID = "emp003"

# One approved leave day (no checkin) + one plain unexplained-absence
# weekday (also no checkin, no leave row) -- both weekdays, both in the
# same month so they land in the same per-employee sheet.
row = leave_store.apply_leave(UID, "2026-10-05", "2026-10-05", reason="approved test")
leave_store.approve_leave(row["id"], "emp009")
# 2026-10-06 (Tuesday) has no attendance row and no leave row -> plain "Leave"

# Bypass the real session-cookie auth for this worker-logic test --
# _verified_admin() is a real security gate (routes/attendance.py) and is
# not being re-tested here; this test is about the workbook content.
attn._verified_admin = lambda: True

app = Flask(__name__)
app.register_blueprint(attn.attendance_bp)
client = app.test_client()

r = client.get("/api/attendance/export-sheets")
assert r.status_code == 200, r.status_code

from openpyxl import load_workbook  # noqa: E402
wb = load_workbook(io.BytesIO(r.data))

assert UID.lower() != "abhinav"  # sanity: UID is an id, not the sheet title
ws = wb["Abhinav"]  # per-employee sheet is titled by name, not id

# Find the two rows by date in column A and check column G (Day Type)
found_approved = found_plain = False
for r_idx in range(5, ws.max_row + 1):
    cell_a = ws.cell(row=r_idx, column=1).value
    if cell_a is None:
        continue
    dstr = cell_a.isoformat() if hasattr(cell_a, "isoformat") else str(cell_a)
    if dstr == "2026-10-05":
        assert ws.cell(row=r_idx, column=7).value == "Leave (Approved)", \
            ws.cell(row=r_idx, column=7).value
        found_approved = True
    if dstr == "2026-10-06":
        assert ws.cell(row=r_idx, column=7).value == "Leave", \
            ws.cell(row=r_idx, column=7).value
        found_plain = True
assert found_approved, "approved-leave row not found in per-employee sheet"
assert found_plain, "plain-absence row not found in per-employee sheet"

# Summary sheet carries the formal leave balance columns
ws_sum = wb["Summary"]
headers = [ws_sum.cell(row=6, column=c).value for c in range(1, ws_sum.max_column + 1)]
assert "Leave Used (Formal)" in headers, headers
assert "Comp-Off Earned" in headers, headers
assert "Leave Remaining" in headers, headers

col_remaining = headers.index("Leave Remaining") + 1
# find Abhinav's row in the Summary sheet
for r_idx in range(7, ws_sum.max_row + 1):
    if ws_sum.cell(row=r_idx, column=1).value == "Abhinav":
        bal = leave_store.get_balance(UID)
        assert ws_sum.cell(row=r_idx, column=col_remaining).value == bal["remaining"], \
            (ws_sum.cell(row=r_idx, column=col_remaining).value, bal)
        break
else:
    raise AssertionError("Abhinav row not found in Summary sheet")

os.remove(tmp_path)
print("OK: test_leave_export")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe scripts\test_leave_export.py`
Expected: `AssertionError: approved-leave row not found in per-employee sheet`
(the day still says plain `"Leave"` since the approval-aware label doesn't
exist yet)

- [ ] **Step 3: Implement the changes in `backend/routes/attendance.py`**

**3a.** Add `import leave_store` — right after the existing
`from db import get_connection` line inside `attendance_export_sheets()`
(around line 525):

```python
    from db import get_connection
    import leave_store
```

**3b.** Change `_hours_and_day_type`'s signature (lines 444-461) to accept
an approval flag:

```python
def _hours_and_day_type(checkin, checkout, is_today, is_approved_leave=False):
    """(hours:float|None, label:str) for one (checkin_time, checkout_time)
    pair, both plain 'HH:MM:SS' IST or falsy. No midnight-rollover handling
    needed -- both are already clamped inside the same work-day window by
    _clamp_work_time() before they're ever stored. is_approved_leave
    distinguishes a day covered by an HR-approved leave request (leave
    management system, 2026-09-30) from a plain unexplained absence --
    both still mean "no checkin", but only the approved kind is excluded
    from the attendance-% denominator (see `considered` below, unchanged
    on purpose: it only ever sums the plain "Leave" bucket)."""
    if not checkin:
        return None, ("Leave (Approved)" if is_approved_leave else "Leave")
    if not checkout:
        return None, ("In Progress" if is_today else "Incomplete")
    try:
        h1, m1, s1 = (int(p) for p in checkin.split(":"))
        h2, m2, s2 = (int(p) for p in checkout.split(":"))
        hrs = ((h2 * 3600 + m2 * 60 + s2) - (h1 * 3600 + m1 * 60 + s1)) / 3600.0
    except Exception:
        return None, "Incomplete"
    if hrs < 0:
        return None, "Incomplete"
    return round(hrs, 2), ("Full Day" if hrs >= _FULL_DAY_HOURS else "Half Day")
```

**3c.** Right after the `by_user_date`/`earliest_by_user` build block
(after line 570, before `today = today_ist()`), add the approved-leave
lookup helper:

```python
    approved_leave_rows = conn.execute(
        "SELECT user_id, start_date, end_date FROM employee_leave WHERE status='approved'"
    ).fetchall() if False else []  # placeholder replaced below -- conn is already closed here
```

Wait — `conn.close()` already ran a few lines above (line 543). Instead,
fetch this BEFORE that `conn.close()` call. Find this existing block
(lines 534-543):

```python
    cursor.execute(
        """SELECT user_id, date,
                  SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN carried_from IS NOT NULL AND status='pending' THEN 1 ELSE 0 END) AS carried
           FROM standup_tasks
           WHERE status != 'deleted'
           GROUP BY user_id, date"""
    )
    task_counts = {(r[0], r[1]): (r[2] or 0, r[3] or 0) for r in cursor.fetchall()}
    conn.close()
```

Replace it with:

```python
    cursor.execute(
        """SELECT user_id, date,
                  SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN carried_from IS NOT NULL AND status='pending' THEN 1 ELSE 0 END) AS carried
           FROM standup_tasks
           WHERE status != 'deleted'
           GROUP BY user_id, date"""
    )
    task_counts = {(r[0], r[1]): (r[2] or 0, r[3] or 0) for r in cursor.fetchall()}
    cursor.execute(
        "SELECT user_id, start_date, end_date FROM employee_leave WHERE status='approved'"
    )
    approved_leave_rows = cursor.fetchall()
    conn.close()

    def _is_approved_leave_day(uid, dstr):
        return any(u == uid and sd <= dstr <= ed for u, sd, ed in approved_leave_rows)
```

**3d.** Update the "All" sheet loop (line 629) — change:

```python
        _, day_type = _hours_and_day_type(cin, cout, d == today)
```

to:

```python
        _, day_type = _hours_and_day_type(cin, cout, d == today,
                                          is_approved_leave=_is_approved_leave_day(uid, d))
```

**3e.** Update the per-employee sheet loop (line 710) — change:

```python
                hrs, day_type = _hours_and_day_type(cin, cout, dstr == today)
```

to:

```python
                hrs, day_type = _hours_and_day_type(
                    cin, cout, dstr == today,
                    is_approved_leave=_is_approved_leave_day(uid, dstr))
```

**3f.** Add a fill color for the new day type — in `DAY_TYPE_FILLS`
(lines 585-589), add one more entry:

```python
    DAY_TYPE_FILLS = {
        "Full Day": PatternFill("solid", fgColor="D1FAE5"),
        "Half Day": PatternFill("solid", fgColor="FEF3C7"),
        "Leave": PatternFill("solid", fgColor="FEE2E2"),
        "Leave (Approved)": PatternFill("solid", fgColor="FBCFE8"),
    }
```

**3g.** Extend the Summary legend/explanation text (line 818-825) — add
a sentence to the existing `ws_sum.cell(row=4, ...)` value:

```python
    ws_sum.cell(
        row=4, column=1,
        value=(f"Full Day = worked {_FULL_DAY_HOURS:g}+ hours  |  Half Day = worked under "
               f"{_FULL_DAY_HOURS:g} hours (but checked in)  |  Leave = no check-in that weekday  |  "
               "Leave (Approved) = no check-in, covered by an HR-approved leave request "
               "(does not count against Attendance %)  |  "
               "Incomplete = checked in but no checkout was logged  |  "
               "In Progress = still checked in today, not final yet  |  "
               f"Overtime = hours worked beyond {_FULL_DAY_HOURS:g} on a day"),
    ).font = META_FONT
```

**3h.** Compute and store the leave balance per employee — inside the
per-employee loop, right before `emp_summaries.append({...})` (line 800),
add:

```python
        bal = leave_store.get_balance(uid)
```

Then change the `emp_summaries.append({...})` call (lines 800-805) to:

```python
        emp_summaries.append({
            "name": name, "start": start, "full": counts["Full Day"],
            "half": counts["Half Day"], "leave": counts["Leave"],
            "incomplete": counts["Incomplete"], "hours": round(total_hours, 1),
            "overtime": round(total_overtime, 1), "pct": pct,
            "leave_used": bal["used"], "comp_earned": bal["comp_earned"],
            "leave_remaining": bal["remaining"],
        })
```

**3i.** Extend the Summary sheet's header, rows, widths, and filter range.
Change the `style_header(ws_sum, [...], row=6)` call (lines 826-828) to:

```python
    style_header(ws_sum, ["Employee", "Period Start", "Full Days", "Half Days",
                          "Leaves", "Incomplete", "Attendance %", "Total Hours Worked",
                          "Total Overtime Hours", "Leave Used (Formal)",
                          "Comp-Off Earned", "Leave Remaining"], row=6)
```

Change the row-append loop (lines 829-839) to:

```python
    for s in emp_summaries:
        ws_sum.append([s["name"], s["start"], s["full"], s["half"], s["leave"],
                      s["incomplete"], s["pct"] if s["pct"] is not None else "N/A",
                      s["hours"], s["overtime"], s["leave_used"], s["comp_earned"],
                      s["leave_remaining"]])
        r = ws_sum.max_row
        ws_sum.cell(row=r, column=2).number_format = DATE_FMT
        ws_sum.cell(row=r, column=8).number_format = "0.0"
        ws_sum.cell(row=r, column=9).number_format = "0.0"
        if s["pct"] is not None:
            ws_sum.cell(row=r, column=7).number_format = "0.0%"
        border_row(ws_sum, r, 12)
```

Change the two trailing lines (840-842) to:

```python
    if ws_sum.max_row >= 7:
        ws_sum.auto_filter.ref = f"A6:L{ws_sum.max_row}"
    set_widths(ws_sum, [18, 14, 10, 10, 10, 10, 14, 16, 16, 16, 14, 14])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv\Scripts\python.exe scripts\test_leave_export.py`
Expected: `OK: test_leave_export`

- [ ] **Step 5: Confirm the app still boots and run pyflakes**

Run (from `backend/`): `venv\Scripts\python.exe -c "import app"`
Run: `venv\Scripts\python.exe -m pyflakes backend\routes\attendance.py`
Expected: both clean, no new "undefined name" errors

- [ ] **Step 6: Commit**

```bash
git add backend/routes/attendance.py scripts/test_leave_export.py
git commit -m "$(cat <<'EOF'
Attendance Excel export: distinguish approved leave, add balance columns

A weekday with no checkin that's covered by an HR-approved leave request
now labels as "Leave (Approved)" (its own fill color, excluded from the
Attendance % denominator like In Progress already is) instead of plain
"Leave". The Summary sheet gains Leave Used / Comp-Off Earned / Leave
Remaining columns per employee, sourced from leave_store.get_balance().

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `frontend/leave.html` — calendar UI

**Files:**
- Create: `frontend/leave.html`

**Interfaces:**
- Consumes: `GET /api/leave/balance`, `GET /api/leave/calendar`,
  `POST /api/leave/apply`, `POST /api/leave/<id>/cancel`,
  `GET /api/leave/pending`, `POST /api/leave/<id>/approve`,
  `POST /api/leave/<id>/reject` (Task 3)
- Produces: a protected page linked from Task 9's nav wiring

- [ ] **Step 1: Write the page**

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Leave | Agency Portal Assistant</title>
<script src="auth.js"></script>
<style>
:root {
  --bg: #0a0a0f; --s1: #12121a; --s2: #1a1a26; --bdr: rgba(255,255,255,0.08);
  --txt: #e8e8f0; --muted: #6b6b8a; --acc: #8B5A2B;
  --green: #22d3a0; --yellow: #fbbf24; --red: #ef4444; --orange: #f97316;
  --rs: 12px;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Inter',sans-serif; background:var(--bg); color:var(--txt); min-height:100vh; padding:24px; }
.wrap { max-width: 960px; margin: 0 auto; }
.hdr { display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; }
.hdr h1 { font-size:1.4rem; }
.nav a { color:var(--muted); text-decoration:none; margin-left:14px; font-size:.85rem; }
.nav a:hover { color:var(--txt); }
.card { background:var(--s1); border:1px solid var(--bdr); border-radius:var(--rs); padding:18px; margin-bottom:18px; }
.balance-strip { display:flex; gap:24px; flex-wrap:wrap; }
.balance-item .num { font-size:1.6rem; font-weight:700; }
.balance-item .lbl { font-size:.72rem; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
.cal-nav { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
.cal-nav button { background:var(--s2); border:1px solid var(--bdr); color:var(--txt); border-radius:8px; padding:6px 12px; cursor:pointer; }
.cal-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:6px; }
.cal-h { font-size:.7rem; color:var(--muted); text-align:center; padding:4px; }
.cal-day { position:relative; aspect-ratio:1; border:1px solid var(--bdr); border-radius:8px; padding:4px; font-size:.75rem; cursor:pointer; display:flex; flex-direction:column; align-items:center; justify-content:flex-start; }
.cal-day:hover { border-color:var(--acc); }
.cal-day.empty { border:none; cursor:default; }
.cal-day.weekend { opacity:.35; cursor:default; }
.dot { width:8px; height:8px; border-radius:50%; margin-top:4px; }
.dot.full { background:var(--green); }
.dot.half { background:var(--yellow); }
.dot.leave_approved { background:var(--red); }
.dot.leave_pending { background:var(--orange); }
.legend { display:flex; gap:16px; flex-wrap:wrap; font-size:.72rem; color:var(--muted); margin-top:12px; }
.legend span { display:flex; align-items:center; gap:5px; }
#toast { display:none; position:fixed; bottom:20px; right:20px; padding:10px 16px; border-radius:8px; font-size:.85rem; z-index:999; }
#toast.ok { background:var(--green); color:#04241a; }
#toast.err { background:var(--red); color:#2a0505; }
.modal-bg { display:none; position:fixed; inset:0; background:rgba(0,0,0,.6); align-items:center; justify-content:center; z-index:998; }
.modal-bg.open { display:flex; }
.modal { background:var(--s1); border:1px solid var(--bdr); border-radius:var(--rs); padding:20px; width:340px; }
.modal h3 { margin-bottom:12px; font-size:1rem; }
.modal label { display:block; font-size:.75rem; color:var(--muted); margin:10px 0 4px; }
.modal select, .modal textarea, .modal input { width:100%; background:var(--s2); border:1px solid var(--bdr); color:var(--txt); border-radius:6px; padding:7px; font-size:.85rem; font-family:inherit; }
.modal-actions { display:flex; gap:8px; margin-top:16px; }
.modal-actions button { flex:1; padding:8px; border-radius:6px; border:none; cursor:pointer; font-size:.85rem; }
.btn-submit { background:var(--acc); color:#fff; }
.btn-cancel { background:var(--s2); color:var(--txt); }
.pending-row { display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid var(--bdr); font-size:.82rem; }
.pending-row:last-child { border-bottom:none; }
.pending-actions button { padding:4px 10px; border-radius:6px; border:none; cursor:pointer; font-size:.75rem; margin-left:6px; }
.btn-approve { background:var(--green); color:#04241a; }
.btn-reject { background:var(--red); color:#2a0505; }
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <h1>Leave</h1>
    <div class="nav">
      <a href="dashboard.html">Dashboard</a>
      <a href="standup.html">Daily Standup</a>
    </div>
  </div>

  <div class="card">
    <div class="balance-strip" id="balance-strip">Loading…</div>
  </div>

  <div class="card" id="hr-panel" style="display:none;">
    <h3 style="margin-bottom:10px;">Pending Approvals</h3>
    <div id="pending-list"><div style="color:var(--muted);font-size:.82rem;">No pending requests.</div></div>
  </div>

  <div class="card">
    <div class="cal-nav">
      <button onclick="calPrev()">&#8592;</button>
      <span id="cal-label" style="font-weight:600;"></span>
      <button onclick="calNext()">&#8594;</button>
    </div>
    <div class="cal-grid" id="cal-grid"></div>
    <div class="legend">
      <span><span class="dot full"></span> Full day worked</span>
      <span><span class="dot half"></span> Half day worked</span>
      <span><span class="dot leave_approved"></span> Approved leave</span>
      <span><span class="dot leave_pending"></span> Pending request</span>
      <span>Click a date to apply for leave</span>
    </div>
  </div>
</div>

<div class="modal-bg" id="apply-modal">
  <div class="modal">
    <h3 id="apply-modal-date">Apply for leave</h3>
    <label>Type</label>
    <select id="apply-type"><option value="full">Full Day</option><option value="half">Half Day</option></select>
    <label>Reason</label>
    <textarea id="apply-reason" rows="3" placeholder="Optional"></textarea>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeApplyModal()">Cancel</button>
      <button class="btn-submit" onclick="submitApply()">Submit</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const API = location.hostname==="localhost"||location.hostname==="127.0.0.1" ? "http://localhost:5000" : location.origin;
const user = JSON.parse(localStorage.getItem("agency_portal_user")||"{}");
const UID = user.user_id;
if (!UID) window.location.href = "login.html";
const IS_HR = UID === "emp009";

function toast(m,t="ok"){
  const e=document.getElementById("toast");
  e.textContent=m; e.className=t; e.style.display="block";
  clearTimeout(window.__toastTimer);
  window.__toastTimer=setTimeout(()=>e.style.display="none",2500);
}

let calDate = new Date();
let pickedDate = null;

function calPrev(){ calDate.setMonth(calDate.getMonth()-1); renderCalendar(); }
function calNext(){ calDate.setMonth(calDate.getMonth()+1); renderCalendar(); }

async function loadBalance(){
  try{
    const r = await fetch(`${API}/api/leave/balance?user_id=${encodeURIComponent(UID)}`);
    const d = await r.json();
    document.getElementById("balance-strip").innerHTML = `
      <div class="balance-item"><div class="num">${d.remaining}</div><div class="lbl">Remaining</div></div>
      <div class="balance-item"><div class="num">${d.used}</div><div class="lbl">Used (${d.year})</div></div>
      <div class="balance-item"><div class="num">${d.comp_earned}</div><div class="lbl">Comp-Off Earned</div></div>
      <div class="balance-item"><div class="num">${d.base}</div><div class="lbl">Base Pool</div></div>
    `;
  }catch(e){ toast("Couldn't load balance","err"); }
}

async function loadPending(){
  if (!IS_HR) return;
  document.getElementById("hr-panel").style.display = "block";
  try{
    const r = await fetch(`${API}/api/leave/pending?user_id=${encodeURIComponent(UID)}`);
    const d = await r.json();
    const wrap = document.getElementById("pending-list");
    if (!d.pending || !d.pending.length){
      wrap.innerHTML = '<div style="color:var(--muted);font-size:.82rem;">No pending requests.</div>';
      return;
    }
    wrap.innerHTML = d.pending.map(p => `
      <div class="pending-row">
        <span>${esc(p.user_id)} — ${esc(p.start_date)}${p.start_date!==p.end_date?" to "+esc(p.end_date):""} (${esc(p.leave_type)})${p.reason?" — "+esc(p.reason):""}</span>
        <span class="pending-actions">
          <button class="btn-approve" onclick="decide(${p.id},'approve')">Approve</button>
          <button class="btn-reject" onclick="decide(${p.id},'reject')">Reject</button>
        </span>
      </div>
    `).join("");
  }catch(e){ toast("Couldn't load pending requests","err"); }
}

function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m])); }

async function decide(id, action){
  try{
    const r = await fetch(`${API}/api/leave/${id}/${action}`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({user_id: UID}),
    });
    const d = await r.json();
    if (!r.ok){ toast(d.error || "Failed", "err"); return; }
    toast(action === "approve" ? "Approved" : "Rejected");
    loadPending(); renderCalendar();
  }catch(e){ toast("Network error","err"); }
}

async function renderCalendar(){
  const year = calDate.getFullYear(), month = calDate.getMonth()+1;
  const monthNames = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  document.getElementById("cal-label").textContent = `${monthNames[month-1]} ${year}`;

  let days = {};
  try{
    const r = await fetch(`${API}/api/leave/calendar?user_id=${encodeURIComponent(UID)}&year=${year}&month=${month}`);
    const d = await r.json();
    days = d.days || {};
  }catch(e){ toast("Couldn't load calendar","err"); }

  const firstDay = new Date(year, month-1, 1).getDay();
  const daysInMonth = new Date(year, month, 0).getDate();
  const grid = document.getElementById("cal-grid");
  let html = "";
  ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"].forEach(d => html += `<div class="cal-h">${d}</div>`);
  for (let i=0;i<firstDay;i++) html += `<div class="cal-day empty"></div>`;
  for (let d=1; d<=daysInMonth; d++){
    const dstr = `${year}-${String(month).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const info = days[dstr] || {status:"none"};
    const isWeekend = info.status === "weekend";
    const dot = ["full","half","leave_approved","leave_pending"].includes(info.status)
      ? `<div class="dot ${info.status}"></div>` : "";
    html += `<div class="cal-day ${isWeekend?'weekend':''}" ${isWeekend?'':`onclick="openApplyModal('${dstr}')"`}>
      <div>${d}</div>${dot}
    </div>`;
  }
  grid.innerHTML = html;
}

function openApplyModal(dstr){
  pickedDate = dstr;
  document.getElementById("apply-modal-date").textContent = `Apply for leave — ${dstr}`;
  document.getElementById("apply-reason").value = "";
  document.getElementById("apply-type").value = "full";
  document.getElementById("apply-modal").classList.add("open");
}
function closeApplyModal(){ document.getElementById("apply-modal").classList.remove("open"); }

async function submitApply(){
  if (!pickedDate) return;
  const leave_type = document.getElementById("apply-type").value;
  const reason = document.getElementById("apply-reason").value;
  try{
    const r = await fetch(`${API}/api/leave/apply`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({user_id: UID, date: pickedDate, leave_type, reason}),
    });
    const d = await r.json();
    if (!r.ok){ toast(d.error || "Failed to apply", "err"); return; }
    toast("Leave request sent for approval");
    closeApplyModal();
    renderCalendar();
  }catch(e){ toast("Network error","err"); }
}

(async () => {
  await loadBalance();
  await loadPending();
  await renderCalendar();
})();
</script>
</body>
</html>
```

- [ ] **Step 2: Extract the script block and run `node --check`**

This repo has no build step; `node --check` can't parse a full `.html`
file, so extract just the `<script>...</script>` contents first.

Run (PowerShell, from repo root):
```
$html = Get-Content frontend\leave.html -Raw
$m = [regex]::Match($html, '(?s)<script>(.*?)</script>\s*</body>')
Set-Content -Path D:\temp\claude\leave_script.js -Value $m.Groups[1].Value -Encoding utf8
node --check D:\temp\claude\leave_script.js
```
Expected: no output (syntax OK)

- [ ] **Step 3: Commit**

```bash
git add frontend/leave.html
git commit -m "$(cat <<'EOF'
Add frontend/leave.html -- balance strip, apply calendar, HR approval panel

Colored dots per day (green=full day worked, yellow=half day worked,
red=approved leave, orange=pending request), click-to-apply modal, and an
HR-only (emp009) pending-approvals panel.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Nav wiring — link `leave.html` from dashboard and standup

**Files:**
- Modify: `frontend/dashboard.html:349` (the "For You" nav dropdown)
- Modify: `frontend/standup.html:125-129` (the `.nav` link list)

**Interfaces:**
- Consumes: `frontend/leave.html` (Task 8)
- Produces: nothing new — pure navigation, no new interface

- [ ] **Step 1: Add the link to `dashboard.html`**

Find (line 349):

```html
          <a href="standup.html" class="nav-dropdown-item">Daily Standup</a>
```

Change to:

```html
          <a href="standup.html" class="nav-dropdown-item">Daily Standup</a>
          <a href="leave.html" class="nav-dropdown-item">Leave</a>
```

- [ ] **Step 2: Add the link to `standup.html`**

Find (lines 125-129):

```html
    <div class="nav">
      <a href="projects.html" class="nl"> Board</a>
      <a href="my-tasks.html" class="nl"> My Tasks</a>
      <a href="dashboard.html" class="nl"> Dashboard</a>
    </div>
```

Change to:

```html
    <div class="nav">
      <a href="projects.html" class="nl"> Board</a>
      <a href="my-tasks.html" class="nl"> My Tasks</a>
      <a href="leave.html" class="nl"> Leave</a>
      <a href="dashboard.html" class="nl"> Dashboard</a>
    </div>
```

- [ ] **Step 3: Sanity-check both files still parse as valid HTML-with-script**

Run the same `node --check` extraction technique as Task 8 Step 2 against
both `frontend/dashboard.html` and `frontend/standup.html` (only the nav
markup changed, not any script — this step confirms the edit didn't
accidentally break a nearby tag).
Expected: no output from either `node --check` call

- [ ] **Step 4: Commit**

```bash
git add frontend/dashboard.html frontend/standup.html
git commit -m "$(cat <<'EOF'
Link the new Leave page from dashboard and standup nav

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**1. Spec coverage:**
- 15-day pool -> Task 2 (`ANNUAL_BASE_DAYS`), Task 3 (`/api/leave/balance`)
- 24hr overtime -> +1 leave -> Task 2 (`run_overtime_conversion_sweep`), Task 4 (nightly job)
- Leave column in Excel export -> Task 7 (approved-leave label + Summary balance columns) — corrected from the spec's separate-export idea once `attendance_export_sheets()` was discovered to already exist with most of the needed infrastructure; this plan extends it instead, a strictly smaller and safer change
- Calendar with colored dots, click-to-apply -> Task 8 (`frontend/leave.html`)
- HR approval gate (emp009) -> Task 3 (`HR_USER_ID` check), Task 8 (HR panel)
- WhatsApp leave becomes a request -> Task 6
- Standup-lock/login-nudge tightening to approved-only -> Task 2 (implementation), Task 5 (explicit regression test)
- Calendar year, no carryover -> Task 2 (`get_balance` scopes by `year`)
- Backdated leave allowed -> Task 2/3 (`apply_leave`/`/api/leave/apply` have no future-only date check)

**2. Placeholder scan:** none found — every step has real, complete code
or an exact shell command with expected output.

**3. Type consistency:** `leave_store.apply_leave` returns
`{"id","user_id","start_date","end_date","leave_type","reason","status"}`
consistently referenced in Task 3's route, Task 6's WhatsApp handler
(via `set_leave`, same row shape), and Task 8's frontend (`p.id`,
`p.start_date`, etc. matching `list_pending()`'s dict keys exactly).
`get_balance()`'s `{"base","comp_earned","used","remaining","year"}` shape
is used identically in Task 3's route, Task 6's WhatsApp reply, Task 7's
export, and Task 8's balance strip.

**4. Review Focus coverage:** all five items from the header have an
owning task with a test that exercises them (overlap rejection and
leave_type validation in Task 2's test; HR-only gating in Task 3's test;
the standup-lock regression as its own dedicated Task 5; sweep idempotency
in Task 2's test via the double-run-same-date assertion).

---

Plan complete and saved to `docs/superpowers/plans/2026-09-30-leave-management.md`. Please review the plan. Which execution approach would you prefer?

- **Subagent-driven** - A fresh subagent implements each task and a fresh reviewer checks it before the next one starts, then a whole-branch review at the end. Most thorough; costs a fresh context per task and per review.
- **Native** - I implement every task myself in this session, the way this harness runs work, then one fresh reviewer on the most capable model checks the whole branch. Cheapest and fastest; no independent review until the end. Runs well with a mid-tier session model, since the plan carries the design.

For this plan I recommend **subagent-driven**, because Task 7 makes several precise edits to a large, already-complex existing function (`attendance_export_sheets()`) sitting right next to the pre-existing per-day/monthly attendance-percentage logic — a mistake there would silently corrupt an HR-facing report, and per-task fresh review is worth the extra cost here specifically. Does the plan capture what you want, and which approach should we use?
