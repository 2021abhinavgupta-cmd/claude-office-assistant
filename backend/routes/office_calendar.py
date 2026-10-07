"""
Office Calendar Blueprint
Routes: /api/office-calendar*

HR (emp009, Noorish) maintains a company-wide calendar of public holidays,
work-from-home days, in-person office days and timed events. Every logged-in
employee can read it (it is rendered on their Calendar page); only HR can
write.

Events (kind='event') carry a start time (and optional end time). Creating,
changing or deleting one announces it to everyone by email + WhatsApp when
`notify` is on, and office_events.reminder_sweep() reminds everyone shortly
before it starts.

Identity comes from the real session_token cookie, never a client-supplied
user_id -- same pattern as routes/leave.py.
"""
from __future__ import annotations

import re
from datetime import date as _date

from flask import Blueprint, jsonify, request

from db import get_connection
from routes.leave import _require_hr, _require_session

office_calendar_bp = Blueprint("office_calendar", __name__)

KINDS = ("holiday", "wfh", "office", "event")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MAX_SPAN_DAYS = 366
_COLS = "id, kind, title, start_date, end_date, created_by, start_time, end_time, notify"


def _valid_date(s: str) -> bool:
    if not _DATE_RE.match(s or ""):
        return False
    try:
        _date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _row(r) -> dict:
    return {"id": r[0], "kind": r[1], "title": r[2], "start_date": r[3],
            "end_date": r[4], "created_by": r[5], "start_time": r[6] or "",
            "end_time": r[7] or "", "notify": bool(r[8]) if r[8] is not None else True}


def _get(conn, event_id: int):
    r = conn.execute(f"SELECT {_COLS} FROM office_calendar WHERE id=?", (event_id,)).fetchone()
    return _row(r) if r else None


def _parse_body():
    """-> (fields dict, None) or (None, error response)."""
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or "").strip().lower()
    start = (body.get("start_date") or "").strip()
    end = (body.get("end_date") or start).strip()
    title = str(body.get("title") or "").strip()[:120]
    st = str(body.get("start_time") or "").strip()
    et = str(body.get("end_time") or "").strip()
    notify = bool(body.get("notify", True))
    if kind not in KINDS:
        return None, (jsonify({"error": "kind must be holiday, wfh, office or event"}), 400)
    if not _valid_date(start) or not _valid_date(end):
        return None, (jsonify({"error": "dates must be YYYY-MM-DD"}), 400)
    if end < start:
        return None, (jsonify({"error": "end date is before start date"}), 400)
    if (_date.fromisoformat(end) - _date.fromisoformat(start)).days > _MAX_SPAN_DAYS:
        return None, (jsonify({"error": "range too long"}), 400)
    if kind in ("holiday", "event") and not title:
        return None, (jsonify({"error": f"a {kind} needs a name"}), 400)
    if kind == "event":
        if not _TIME_RE.match(st):
            return None, (jsonify({"error": "an event needs a start time (HH:MM)"}), 400)
        if et and not _TIME_RE.match(et):
            return None, (jsonify({"error": "end time must be HH:MM"}), 400)
        if et and start == end and et <= st:
            return None, (jsonify({"error": "end time is before start time"}), 400)
    else:
        st, et, notify = "", "", False
    return {"kind": kind, "title": title, "start_date": start, "end_date": end,
            "start_time": st or None, "end_time": et or None, "notify": notify}, None


def _announce(ev: dict, action: str) -> None:
    try:
        import office_events
        office_events.announce(ev, action)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("office calendar: announce failed")


def _is_future(ev: dict) -> bool:
    from utils import today_ist
    return (ev.get("end_date") or ev.get("start_date") or "") >= today_ist()


@office_calendar_bp.route("/api/office-calendar", methods=["GET"])
def oc_list():
    _, err = _require_session()
    if err:
        return err
    start = (request.args.get("start") or "").strip()
    end = (request.args.get("end") or "").strip()
    if not _valid_date(start) or not _valid_date(end):
        return jsonify({"error": "start and end (YYYY-MM-DD) required"}), 400
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT {_COLS} FROM office_calendar "
            "WHERE start_date <= ? AND end_date >= ? ORDER BY start_date, start_time, id",
            (end, start)).fetchall()
    finally:
        conn.close()
    return jsonify({"events": [_row(r) for r in rows]})


@office_calendar_bp.route("/api/office-calendar", methods=["POST"])
def oc_create():
    uid, err = _require_hr()
    if err:
        return err
    f, err = _parse_body()
    if err:
        return err
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO office_calendar (kind, title, start_date, end_date, created_by, "
                "start_time, end_time, notify) VALUES (?,?,?,?,?,?,?,?)",
                (f["kind"], f["title"], f["start_date"], f["end_date"], uid,
                 f["start_time"], f["end_time"], 1 if f["notify"] else 0))
            new_id = cur.lastrowid
    finally:
        conn.close()
    out = {"id": new_id, **f, "created_by": uid}
    if f["kind"] == "event" and f["notify"] and _is_future(f):
        _announce(f, "new")
        out["announced"] = True
    return jsonify(out)


@office_calendar_bp.route("/api/office-calendar/<int:event_id>", methods=["PUT"])
def oc_update(event_id: int):
    uid, err = _require_hr()
    if err:
        return err
    f, err = _parse_body()
    if err:
        return err
    conn = get_connection()
    try:
        before = _get(conn, event_id)
        if not before:
            return jsonify({"error": "not found"}), 404
        changed = any((before.get(k) or "") != (f.get(k) or "")
                      for k in ("title", "start_date", "end_date", "start_time", "end_time"))
        with conn:
            # A moved/renamed event gets a fresh reminder.
            conn.execute(
                "UPDATE office_calendar SET kind=?, title=?, start_date=?, end_date=?, "
                "start_time=?, end_time=?, notify=?"
                + (", reminded_at=NULL" if changed else "") + " WHERE id=?",
                (f["kind"], f["title"], f["start_date"], f["end_date"],
                 f["start_time"], f["end_time"], 1 if f["notify"] else 0, event_id))
    finally:
        conn.close()
    out = {"id": event_id, **f}
    if f["kind"] == "event" and f["notify"] and changed and _is_future(f):
        _announce(f, "updated")
        out["announced"] = True
    return jsonify(out)


@office_calendar_bp.route("/api/office-calendar/<int:event_id>", methods=["DELETE"])
def oc_delete(event_id: int):
    uid, err = _require_hr()
    if err:
        return err
    conn = get_connection()
    try:
        before = _get(conn, event_id)
        if not before:
            return jsonify({"error": "not found"}), 404
        with conn:
            conn.execute("DELETE FROM office_calendar WHERE id=?", (event_id,))
    finally:
        conn.close()
    if before["kind"] == "event" and before["notify"] and _is_future(before):
        _announce(before, "cancelled")
    return jsonify({"success": True})
