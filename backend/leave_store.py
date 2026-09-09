"""Employee leave / holiday windows (CLAUDE.md gotcha #119).

A row in `employee_leave` is an inclusive [start_date, end_date] window.
While a person is on leave they are exempt from:
  - the daily-standup lock (`/api/standup/lock-status`)
  - every recurring "you haven't logged in" nudge (personal DM + group ping)

Set and cleared through the WhatsApp bot (`set_leave` / `clear_leave`
tools in whatsapp_agent.py). This module only imports `db`, so it is safe
to import from routes and from the agent alike.
"""
from __future__ import annotations

import logging

from db import get_connection

logger = logging.getLogger(__name__)

_DDL = """CREATE TABLE IF NOT EXISTS employee_leave (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    reason     TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)"""


def _ensure(conn) -> None:
    """Idempotent — db.init_db() also creates this, but a companion endpoint
    or the agent may touch the table before a redeploy has re-run init."""
    conn.execute(_DDL)


def set_leave(user_id: str, start_date: str, end_date: str,
              reason: str = "", created_by: str = "") -> dict:
    """Record (or replace) a leave window for one person. Any existing rows
    that overlap the new window are removed first, so 'on leave today' after
    'on leave all week' just narrows it rather than stacking."""
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            conn.execute(
                "DELETE FROM employee_leave WHERE user_id=? "
                "AND NOT (end_date < ? OR start_date > ?)",
                (user_id, start_date, end_date),
            )
            cur = conn.execute(
                "INSERT INTO employee_leave "
                "(user_id, start_date, end_date, reason, created_by) "
                "VALUES (?,?,?,?,?)",
                (user_id, start_date, end_date, reason or "", created_by or ""),
            )
        return {"id": cur.lastrowid, "user_id": user_id,
                "start_date": start_date, "end_date": end_date,
                "reason": reason or ""}
    finally:
        conn.close()


def clear_leave(user_id: str, on_date: str | None = None) -> int:
    """Remove leave for `user_id`. With `on_date`, only the window(s)
    covering that date; without it, every current/future window
    (end_date >= on_date is not applied — it wipes all rows for the user)."""
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            if on_date:
                cur = conn.execute(
                    "DELETE FROM employee_leave WHERE user_id=? "
                    "AND start_date <= ? AND end_date >= ?",
                    (user_id, on_date, on_date),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM employee_leave WHERE user_id=?", (user_id,))
        return cur.rowcount or 0
    finally:
        conn.close()


def is_on_leave(user_id: str, date_str: str) -> bool:
    return user_id in on_leave_ids(date_str)


def on_leave_ids(date_str: str) -> set[str]:
    """Every user_id whose leave window covers `date_str`."""
    conn = get_connection()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM employee_leave "
            "WHERE start_date <= ? AND end_date >= ?",
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
            "WHERE user_id=? AND start_date <= ? AND end_date >= ? "
            "ORDER BY end_date DESC LIMIT 1",
            (user_id, date_str, date_str),
        ).fetchone()
        if not r:
            return None
        return {"start_date": r[0], "end_date": r[1], "reason": r[2] or ""}
    finally:
        conn.close()
