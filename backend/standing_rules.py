"""Standing instructions -- persistent "from now on, always..." rules.

A standing rule is something a person tells the bot ONCE and expects it to
honour indefinitely: "always tell me when a client uploads something",
"never put design work on Charulata without asking", "every Monday remind
me to send invoices".

Two halves, deliberately:

  1. Every active rule for a person is injected into their system prompt on
     every message, so the bot obeys it in conversation without being
     reminded. This is the part that covers most real rules.

  2. A rule may ALSO carry a weekday + time (`remind_day` / `remind_time`),
     set by the model when the rule is clearly a recurring reminder. The
     follow-up sweep (followups.py) fires those. This is deliberately a
     narrow hook -- a weekday and a clock time -- rather than a
     natural-language cron engine: one-off timed things already have
     remind_me / schedule_group_message, and anything more exotic than
     "every <day> at <time>" is honoured conversationally via (1) instead
     of being silently half-parsed.

Set and cleared through the WhatsApp bot (set_standing_rule /
list_standing_rules / remove_standing_rule in whatsapp_agent.py). Only
`db` is imported, so this is safe to import from routes, the agent and the
scheduler alike -- same contract as leave_store.py.
"""
from __future__ import annotations

import logging

from db import get_connection

logger = logging.getLogger(__name__)

MAX_RULES_PER_USER = 25

_DDL = """CREATE TABLE IF NOT EXISTS standing_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    rule        TEXT NOT NULL,
    remind_day  TEXT DEFAULT '',
    remind_time TEXT DEFAULT '',
    active      INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
)"""

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]


def _ensure(conn) -> None:
    """Idempotent -- db.init_db() creates this too, but the agent or a route
    may touch the table before a redeploy has re-run init."""
    conn.execute(_DDL)


def normalize_day(raw: str) -> str:
    """'Mon'/'monday'/'MONDAY' -> 'monday'; anything unrecognized -> ''."""
    d = str(raw or "").strip().lower()
    if not d:
        return ""
    for w in _WEEKDAYS:
        if w == d or w.startswith(d[:3]):
            return w
    return ""


def weekday_index(day: str) -> int:
    """'monday' -> 0, matching datetime.weekday(). -1 if not a weekday name."""
    d = normalize_day(day)
    return _WEEKDAYS.index(d) if d in _WEEKDAYS else -1


def add_rule(user_id: str, rule: str, remind_day: str = "",
             remind_time: str = "") -> dict:
    """Save one standing rule. Returns the stored row.

    A rule whose text already exists for this person (case-insensitively) is
    updated in place rather than duplicated -- restating a rule is the
    natural way someone adjusts its schedule.
    """
    rule = str(rule or "").strip()
    if not rule:
        raise ValueError("empty rule")
    day = normalize_day(remind_day)
    tm = str(remind_time or "").strip()
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            existing = conn.execute(
                "SELECT id FROM standing_rules WHERE user_id=? "
                "AND LOWER(rule)=LOWER(?) AND active=1",
                (user_id, rule),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE standing_rules SET remind_day=?, remind_time=? WHERE id=?",
                    (day, tm, existing[0]),
                )
                rid = existing[0]
            else:
                cur = conn.execute(
                    "INSERT INTO standing_rules "
                    "(user_id, rule, remind_day, remind_time) VALUES (?,?,?,?)",
                    (user_id, rule[:500], day, tm),
                )
                rid = cur.lastrowid
                # Oldest-first cap, so a runaway rule list can't grow the
                # system prompt without bound.
                conn.execute(
                    "UPDATE standing_rules SET active=0 WHERE id IN ("
                    "  SELECT id FROM standing_rules WHERE user_id=? AND active=1"
                    "  ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?)",
                    (user_id, MAX_RULES_PER_USER),
                )
        return {"id": rid, "rule": rule, "remind_day": day, "remind_time": tm}
    finally:
        conn.close()


def list_rules(user_id: str) -> list:
    conn = get_connection()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT id, rule, remind_day, remind_time FROM standing_rules "
            "WHERE user_id=? AND active=1 ORDER BY id",
            (user_id,),
        ).fetchall()
        return [{"id": r[0], "rule": r[1], "remind_day": r[2] or "",
                 "remind_time": r[3] or ""} for r in rows]
    except Exception:
        logger.exception("standing_rules.list_rules failed")
        return []
    finally:
        conn.close()


def remove_rule(user_id: str, which: str) -> dict | None:
    """Deactivate one rule, found by its number (as shown by list_rules) or
    by a substring of its text. Returns the removed rule, or None."""
    which = str(which or "").strip()
    if not which:
        return None
    rules = list_rules(user_id)
    if not rules:
        return None

    target = None
    if which.isdigit():
        n = int(which)
        # Accept both the 1-based position shown to the user and a raw row id.
        if 1 <= n <= len(rules):
            target = rules[n - 1]
        else:
            target = next((r for r in rules if r["id"] == n), None)
    if target is None:
        low = which.lower()
        target = next((r for r in rules if low in r["rule"].lower()), None)
    if target is None:
        return None

    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            conn.execute("UPDATE standing_rules SET active=0 WHERE id=?",
                         (target["id"],))
        return target
    finally:
        conn.close()


def rules_text(user_id: str) -> str:
    """The block injected into this person's system prompt. '' when none."""
    rules = list_rules(user_id)
    if not rules:
        return ""
    lines = []
    for r in rules:
        line = r["rule"]
        if r["remind_day"] and r["remind_time"]:
            line += f" (recurring: every {r['remind_day']} at {r['remind_time']})"
        lines.append(f"- {line}")
    return (
        "STANDING INSTRUCTIONS from this person -- they set these once and "
        "expect you to honour them from then on, without being reminded. "
        "Follow them unless the current message clearly overrides one:\n"
        + "\n".join(lines)
    )


def due_reminders(user_id: str, weekday: int, hhmm: str) -> list:
    """Recurring rules for this person whose day matches `weekday` and whose
    time has arrived (<= hhmm). The caller is responsible for not re-sending
    the same one twice in a day (followups.py does this via followup_log)."""
    out = []
    for r in list_rules(user_id):
        if not r["remind_day"] or not r["remind_time"]:
            continue
        if weekday_index(r["remind_day"]) != weekday:
            continue
        if str(r["remind_time"])[:5] <= str(hhmm)[:5]:
            out.append(r)
    return out
