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

_OVERTIME_LEDGER_DDL = """CREATE TABLE IF NOT EXISTS overtime_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    date         TEXT NOT NULL,
    worked_hours REAL NOT NULL,
    ot_hours     REAL NOT NULL,
    converted_at TEXT DEFAULT NULL,
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, date)
)"""


def _ensure(conn) -> None:
    """Idempotent — db.init_db() also creates this, but a companion
    endpoint or the agent may touch the table before a redeploy has
    re-run init."""
    conn.execute(_DDL)


def _ensure_overtime_ledger(conn) -> None:
    """Same idempotent-defensive-create idiom as _ensure() above, so every
    public function in this module tolerates being called before
    db.init_db() has ever run — not just the ones that happened to touch
    employee_leave/comp_off_ledger already."""
    conn.execute(_OVERTIME_LEDGER_DDL)


# ── low-level WhatsApp-facing API (existing, gotcha #119) ──────────────

def set_leave(user_id: str, start_date: str, end_date: str,
              reason: str = "", created_by: str = "",
              status: str = "pending", leave_type: str = "full") -> dict:
    """Record a leave request for one person, defaulting to 'pending' --
    the WhatsApp bot's set_leave tool calls this. Only overlapping
    *pending* rows for this user are replaced first (re-requesting the
    same days doesn't pile up duplicates); an already-approved row is
    never touched here. `leave_type` ('full' or 'half') is normalized
    permissively — an invalid value silently falls back to 'full' rather
    than raising, unlike apply_leave()'s strict validation."""
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    if status not in ("pending", "approved"):
        status = "pending"
    if leave_type not in VALID_LEAVE_TYPES:
        leave_type = "full"
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
                "(user_id, start_date, end_date, reason, created_by, status, leave_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (user_id, start_date, end_date, reason or "", created_by or "", status,
                 leave_type),
            )
        return {"id": cur.lastrowid, "user_id": user_id,
                "start_date": start_date, "end_date": end_date,
                "reason": reason or "", "status": status, "leave_type": leave_type}
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
            "SELECT id, start_date, end_date, status FROM employee_leave "
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
        leave_id = None
        for lid, sd, ed, st in leave_rows:
            if sd <= dstr <= ed:
                leave_status = "leave_approved" if st == "approved" else "leave_pending"
                leave_id = lid
                if st == "approved":
                    break  # approved wins over a coincidentally-also-pending row
        if leave_status:
            out[dstr] = {"status": leave_status, "hours": None, "leave_id": leave_id}
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
        _ensure_overtime_ledger(conn)
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
