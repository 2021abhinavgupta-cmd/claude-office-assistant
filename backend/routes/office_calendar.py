"""
Office Calendar Blueprint
Routes: /api/office-calendar*

HR (emp009, Noorish) maintains a company-wide calendar of public holidays,
work-from-home days and in-person office days. Every logged-in employee can
read it (it is rendered on their Leave page); only HR can write.

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

KINDS = ("holiday", "wfh", "office")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_SPAN_DAYS = 366


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
            "end_date": r[4], "created_by": r[5]}


def _parse_body():
    """-> (fields dict, None) or (None, error response)."""
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or "").strip().lower()
    start = (body.get("start_date") or "").strip()
    end = (body.get("end_date") or start).strip()
    title = str(body.get("title") or "").strip()[:120]
    if kind not in KINDS:
        return None, (jsonify({"error": "kind must be holiday, wfh or office"}), 400)
    if not _valid_date(start) or not _valid_date(end):
        return None, (jsonify({"error": "dates must be YYYY-MM-DD"}), 400)
    if end < start:
        return None, (jsonify({"error": "end date is before start date"}), 400)
    if (_date.fromisoformat(end) - _date.fromisoformat(start)).days > _MAX_SPAN_DAYS:
        return None, (jsonify({"error": "range too long"}), 400)
    if kind == "holiday" and not title:
        return None, (jsonify({"error": "a holiday needs a name"}), 400)
    return {"kind": kind, "title": title, "start_date": start, "end_date": end}, None


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
            "SELECT id, kind, title, start_date, end_date, created_by FROM office_calendar "
            "WHERE start_date <= ? AND end_date >= ? ORDER BY start_date, id",
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
                "INSERT INTO office_calendar (kind, title, start_date, end_date, created_by) "
                "VALUES (?,?,?,?,?)",
                (f["kind"], f["title"], f["start_date"], f["end_date"], uid))
            new_id = cur.lastrowid
    finally:
        conn.close()
    return jsonify({"id": new_id, **f, "created_by": uid})


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
        with conn:
            cur = conn.execute(
                "UPDATE office_calendar SET kind=?, title=?, start_date=?, end_date=? WHERE id=?",
                (f["kind"], f["title"], f["start_date"], f["end_date"], event_id))
            changed = cur.rowcount
    finally:
        conn.close()
    if not changed:
        return jsonify({"error": "not found"}), 404
    return jsonify({"id": event_id, **f})


@office_calendar_bp.route("/api/office-calendar/<int:event_id>", methods=["DELETE"])
def oc_delete(event_id: int):
    uid, err = _require_hr()
    if err:
        return err
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute("DELETE FROM office_calendar WHERE id=?", (event_id,))
            changed = cur.rowcount
    finally:
        conn.close()
    if not changed:
        return jsonify({"error": "not found"}), 404
    return jsonify({"success": True})
