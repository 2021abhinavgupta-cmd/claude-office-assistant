"""
Timed company events on HR's office calendar (office_calendar rows with
kind='event'): announcing them to everyone and reminding before they start.

Channels: email (mailer.py -- a no-op until SMTP_* is configured on Railway)
and WhatsApp (wa_outbox -> the always-on laptop's bridge). Both are
best-effort; nothing here ever raises into the HTTP request that created the
event.

Who: every active employee (employees.json, status not inactive/left/...).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from db import get_connection

logger = logging.getLogger(__name__)

_INACTIVE = {"inactive", "disabled", "left", "removed", "archived", "former"}
REMINDER_LEAD_MIN = 30


def _people() -> list[dict]:
    try:
        import utils
        return [e for e in utils._load_employees().get("employees", [])
                if str(e.get("status", "active")).strip().lower() not in _INACTIVE]
    except Exception:
        logger.exception("office_events: roster load failed")
        return []


def fmt_time(hhmm: str | None) -> str:
    """'15:00' -> '3:00 PM'; '' / None -> ''."""
    if not hhmm:
        return ""
    try:
        return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return hhmm


def fmt_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%a, %d %b %Y")
    except ValueError:
        return iso


def when_text(ev: dict) -> str:
    sd, ed = ev.get("start_date", ""), ev.get("end_date", "")
    day = fmt_date(sd) if sd == ed or not ed else f"{fmt_date(sd)} to {fmt_date(ed)}"
    st, et = fmt_time(ev.get("start_time")), fmt_time(ev.get("end_time"))
    if st and et:
        return f"{day}, {st} to {et}"
    if st:
        return f"{day} at {st}"
    return day


def _send_all(subject: str, email_body: str, wa_text: str) -> dict:
    """Email + WhatsApp every active employee. Returns counts."""
    people = _people()
    emailed = whatsapped = 0
    try:
        import mailer
        to = [a for a in (mailer.email_for(e.get("id", "")) for e in people) if a]
        if to and mailer.send(to, subject, email_body):
            emailed = len(to)
    except Exception:
        logger.exception("office_events: email failed")
    try:
        import wa_outbox
        for e in people:
            jid = wa_outbox.wa_jid(e.get("whatsapp", ""))
            if jid and wa_outbox.enqueue(jid, wa_text):
                whatsapped += 1
    except Exception:
        logger.exception("office_events: WhatsApp enqueue failed")
    logger.info(f"office_events: '{subject}' -> {emailed} email(s), {whatsapped} WhatsApp(s)")
    return {"emailed": emailed, "whatsapped": whatsapped}


def announce(ev: dict, action: str = "new") -> dict:
    """action: 'new' | 'updated' | 'cancelled'."""
    title = (ev.get("title") or "Event").strip()
    when = when_text(ev)
    head = {"new": "New event", "updated": "Event updated",
            "cancelled": "Event cancelled"}.get(action, "Event")
    subject = f"{head}: {title} ({when})"
    if action == "cancelled":
        body = f"{title}, planned for {when}, has been cancelled."
        wa = f"*Event cancelled:* {title}\n{when}"
    else:
        body = (f"{title}\nWhen: {when}\n\n"
                f"You'll get a reminder {REMINDER_LEAD_MIN} minutes before it starts.\n"
                "See the Calendar in Lumina for details.")
        wa = (f"*{head}:* {title}\n{when}\n"
              f"I'll remind you {REMINDER_LEAD_MIN} min before it starts.")
    return _send_all(subject, body, wa)


def reminder_sweep(now: datetime | None = None) -> int:
    """Remind everyone about events starting within the next
    REMINDER_LEAD_MIN minutes. Each event is reminded once (reminded_at).
    `now` is IST wall-clock (the scheduler passes nothing -> computed here).
    Returns how many events were reminded."""
    if now is None:
        try:
            from utils import now_ist, today_ist
            now = datetime.strptime(f"{today_ist()} {now_ist()}", "%Y-%m-%d %H:%M:%S")
        except Exception:
            logger.exception("office_events: IST clock unavailable")
            return 0
    today = now.strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, title, start_date, end_date, start_time, end_time FROM office_calendar "
            "WHERE kind='event' AND COALESCE(notify,1)=1 AND reminded_at IS NULL "
            "AND start_date=? AND start_time IS NOT NULL AND start_time<>''", (today,)).fetchall()
    except Exception:
        logger.exception("office_events: reminder query failed")
        conn.close()
        return 0
    done = 0
    for rid, title, sd, ed, st, et in rows:
        try:
            start = datetime.strptime(f"{sd} {st}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if not (now <= start <= now + timedelta(minutes=REMINDER_LEAD_MIN)):
            continue
        # Claim it first so an overlapping sweep can't double-send.
        with conn:
            claimed = conn.execute(
                "UPDATE office_calendar SET reminded_at=datetime('now') "
                "WHERE id=? AND reminded_at IS NULL", (rid,)).rowcount
        if not claimed:
            continue
        ev = {"title": title, "start_date": sd, "end_date": ed, "start_time": st, "end_time": et}
        mins = max(1, int(round((start - now).total_seconds() / 60)))
        t = (title or "Event").strip()
        _send_all(f"Starting in {mins} min: {t}",
                  f"Reminder: {t} starts in {mins} minutes ({when_text(ev)}).",
                  f"*Reminder:* {t} starts in {mins} min\n{when_text(ev)}")
        done += 1
    conn.close()
    return done
