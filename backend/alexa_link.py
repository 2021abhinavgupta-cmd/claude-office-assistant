"""Alexa <-> employee linking, and long answers waiting to be collected.

Alexa never tells us WHO is talking, only an opaque Amazon user id (and a
voice-profile person id when the skill has personalisation). So each employee
links once: they send "link my alexa" to the WhatsApp bot, get a 6-digit code
(valid CODE_TTL_MIN minutes), and say "Alexa, tell Lumina code 1 2 3 4 5 6".
From then on that Amazon account / voice profile is that employee.

Guessing codes is capped per Alexa account and globally (MAX_FAILS_*), on top
of the request-signature check in routes/alexa.py.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from db import get_connection

logger = logging.getLogger(__name__)

CODE_TTL_MIN = 10
MAX_FAILS_PER_HOUR = 5          # per Alexa account
MAX_FAILS_GLOBAL_10MIN = 30     # everyone together; stops a brute-force sweep
PENDING_MAX_AGE_MIN = 30        # an uncollected long answer is forgotten after this


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ensure(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS alexa_links (
        alexa_key    TEXT PRIMARY KEY,
        employee_id  TEXT NOT NULL,
        linked_at    TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alexa_link_codes (
        code         TEXT PRIMARY KEY,
        employee_id  TEXT NOT NULL,
        expires_at   TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alexa_link_fails (
        alexa_key    TEXT,
        at           TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alexa_pending (
        employee_id  TEXT PRIMARY KEY,
        question     TEXT,
        answer       TEXT,
        status       TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        finished_at  TEXT
    )""")


# ---------- linking -------------------------------------------------------
def new_code(employee_id: str) -> str:
    """A fresh 6-digit code for this employee (any older one is replaced)."""
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            conn.execute("DELETE FROM alexa_link_codes WHERE employee_id=? OR expires_at<?",
                         (employee_id, _iso(_now())))
            for _ in range(20):
                code = str(secrets.randbelow(900000) + 100000)
                if not conn.execute("SELECT 1 FROM alexa_link_codes WHERE code=?", (code,)).fetchone():
                    break
            conn.execute("INSERT INTO alexa_link_codes (code, employee_id, expires_at) VALUES (?,?,?)",
                         (code, employee_id, _iso(_now() + timedelta(minutes=CODE_TTL_MIN))))
        return code
    finally:
        conn.close()


def link(alexa_key: str, code: str) -> tuple[str, str | None]:
    """('ok', employee_id) | ('bad', None) | ('locked', None)."""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    conn = get_connection()
    try:
        _ensure(conn)
        now = _now()
        mine = conn.execute("SELECT COUNT(*) FROM alexa_link_fails WHERE alexa_key=? AND at>=?",
                            (alexa_key, _iso(now - timedelta(hours=1)))).fetchone()[0]
        everyone = conn.execute("SELECT COUNT(*) FROM alexa_link_fails WHERE at>=?",
                                (_iso(now - timedelta(minutes=10)),)).fetchone()[0]
        if mine >= MAX_FAILS_PER_HOUR or everyone >= MAX_FAILS_GLOBAL_10MIN:
            return "locked", None
        row = conn.execute("SELECT employee_id FROM alexa_link_codes WHERE code=? AND expires_at>=?",
                           (digits, _iso(now))).fetchone() if len(digits) == 6 else None
        with conn:
            if not row:
                conn.execute("INSERT INTO alexa_link_fails (alexa_key, at) VALUES (?,?)",
                             (alexa_key, _iso(now)))
                conn.execute("DELETE FROM alexa_link_fails WHERE at<?",
                             (_iso(now - timedelta(days=1)),))
                return "bad", None
            emp = row[0]
            conn.execute("DELETE FROM alexa_link_codes WHERE code=?", (digits,))
            conn.execute("INSERT INTO alexa_links (alexa_key, employee_id, linked_at) VALUES (?,?,?) "
                         "ON CONFLICT(alexa_key) DO UPDATE SET employee_id=excluded.employee_id, "
                         "linked_at=excluded.linked_at", (alexa_key, emp, _iso(now)))
            conn.execute("DELETE FROM alexa_link_fails WHERE alexa_key=?", (alexa_key,))
        return "ok", emp
    finally:
        conn.close()


def employee_for(keys: list) -> str | None:
    """First linked employee among the request's keys (voice profile first)."""
    if not keys:
        return None
    conn = get_connection()
    try:
        _ensure(conn)
        for k in keys:
            row = conn.execute("SELECT employee_id FROM alexa_links WHERE alexa_key=?", (k,)).fetchone()
            if row:
                return row[0]
        return None
    finally:
        conn.close()


def unlink_employee(employee_id: str) -> int:
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            return conn.execute("DELETE FROM alexa_links WHERE employee_id=?", (employee_id,)).rowcount
    finally:
        conn.close()


def links() -> list:
    conn = get_connection()
    try:
        _ensure(conn)
        return [{"key": k[:18] + "...", "employee_id": e, "linked_at": t} for k, e, t in
                conn.execute("SELECT alexa_key, employee_id, linked_at FROM alexa_links ORDER BY linked_at")]
    finally:
        conn.close()


# ---------- long answers waiting to be collected ---------------------------
def start_pending(employee_id: str, question: str) -> None:
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            conn.execute("INSERT INTO alexa_pending (employee_id, question, answer, status, created_at) "
                         "VALUES (?,?,NULL,'running',?) ON CONFLICT(employee_id) DO UPDATE SET "
                         "question=excluded.question, answer=NULL, status='running', "
                         "created_at=excluded.created_at, finished_at=NULL",
                         (employee_id, (question or "")[:500], _iso(_now())))
    finally:
        conn.close()


def finish_pending(employee_id: str, answer: str) -> None:
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            conn.execute("UPDATE alexa_pending SET answer=?, status='done', finished_at=? "
                         "WHERE employee_id=? AND status='running'",
                         ((answer or "")[:8000], _iso(_now()), employee_id))
    finally:
        conn.close()


def get_pending(employee_id: str) -> dict | None:
    """{'status': 'running'|'done', 'question', 'answer'} or None (nothing / too old)."""
    conn = get_connection()
    try:
        _ensure(conn)
        row = conn.execute("SELECT question, answer, status, created_at FROM alexa_pending "
                           "WHERE employee_id=?", (employee_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        age = _now() - datetime.fromisoformat(row[3])
    except Exception:
        age = timedelta(0)
    if age > timedelta(minutes=PENDING_MAX_AGE_MIN):
        return None
    return {"question": row[0], "answer": row[1], "status": row[2]}


def clear_pending(employee_id: str) -> None:
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            conn.execute("DELETE FROM alexa_pending WHERE employee_id=?", (employee_id,))
    finally:
        conn.close()
