"""Read-only Google Calendar for the assistant (meetings, shoots, calls).

Uses the SAME service account as the Google Sheets sync
(GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON). No sign-in flow: share each calendar you
want it to see with the service account's email ("See all event details"), then
add the calendar id (for a person's main calendar it is their email address):

    app_settings key 'gcal_calendar_ids'  (comma separated)   or
    env GOOGLE_CALENDAR_IDS

The Calendar API must be enabled for the service account's Google Cloud
project. Read-only; the assistant never creates or edits events.
Everything returns an empty result on any failure.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from db import get_connection

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
_IST = timezone(timedelta(hours=5, minutes=30))
_session = None


def calendar_ids() -> list:
    raw = os.getenv("GOOGLE_CALENDAR_IDS", "")
    if not raw:
        try:
            conn = get_connection()
            row = conn.execute("SELECT value FROM app_settings WHERE key='gcal_calendar_ids'").fetchone()
            conn.close()
            raw = row[0] if row and row[0] else ""
        except Exception:
            raw = ""
    return [x.strip() for x in str(raw).replace("\n", ",").split(",") if x.strip()]


def configured() -> bool:
    return bool(os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON", "").strip()) and bool(calendar_ids())


def _get_session():
    global _session
    if _session is not None:
        return _session
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession
    info = json.loads(os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON", "{}"))
    _session = AuthorizedSession(Credentials.from_service_account_info(info, scopes=_SCOPES))
    return _session


def _fmt_event(ev: dict, cal: str) -> dict:
    st = ev.get("start") or {}
    en = ev.get("end") or {}
    all_day = "date" in st
    def _p(x):
        v = x.get("dateTime") or x.get("date") or ""
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=_IST)
        except Exception:
            return None
    s, e = _p(st), _p(en)
    return {
        "title": ev.get("summary") or "(no title)",
        "start": s.astimezone(_IST) if s else None,
        "end": e.astimezone(_IST) if e else None,
        "all_day": all_day,
        "where": ev.get("location") or "",
        "calendar": cal,
        "attendees": [a.get("displayName") or a.get("email", "") for a in (ev.get("attendees") or [])
                      if not a.get("self")][:6],
    }


def upcoming(days: float = 1, start: datetime | None = None) -> list:
    """Events from `start` (default now) for `days`, soonest first."""
    if not configured():
        return []
    start = start or datetime.now(_IST)
    end = start + timedelta(days=days)
    out = []
    try:
        s = _get_session()
        for cal in calendar_ids():
            r = s.get(f"https://www.googleapis.com/calendar/v3/calendars/{cal}/events",
                      params={"timeMin": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                              "timeMax": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                              "singleEvents": "true", "orderBy": "startTime", "maxResults": 40},
                      timeout=20)
            if r.status_code != 200:
                logger.warning("gcal: %s -> %s %s", cal, r.status_code, r.text[:120])
                continue
            for ev in r.json().get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                out.append(_fmt_event(ev, cal))
    except Exception:
        logger.exception("gcal: fetch failed")
        return []
    out.sort(key=lambda e: e["start"] or datetime.max.replace(tzinfo=_IST))
    return out


def format_events(events: list) -> str:
    if not events:
        return ""
    lines = []
    for e in events:
        if not e["start"]:
            continue
        when = e["start"].strftime("%a %d %b") + ("" if e["all_day"] else e["start"].strftime(" %H:%M"))
        extra = (f" at {e['where']}" if e["where"] else "") + \
                (f" with {', '.join(e['attendees'])}" if e["attendees"] else "")
        lines.append(f"{when} {e['title']}{extra}")
    return "\n".join(lines)
