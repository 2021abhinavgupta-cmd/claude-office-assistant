"""
Leave Management Blueprint
Routes: /api/leave/*

Balance/calendar/apply/cancel are self-service (any real logged-in
employee, acting on their own leave). pending/approve/reject are HR-only,
hardcoded to emp009 (Noorish) -- same allowlist convention as the bet
feature in routes/ops.py, not the generic _is_admin()/bool(user_id)
pattern, since this is a real authority gate, not general app access.

Every route below is authenticated off the real session_token cookie
(routes.auth._verify_session()), never a client-supplied user_id param --
see routes/attendance.py::_verified_admin() for the identical pattern
this mirrors. A client-supplied user_id is just a string anyone can set
to anything, and combined with this app's CORS(origins="*"), trusting it
used to mean anyone could approve/reject/read any employee's leave, or
file leave in a colleague's name, by simply changing that one field.

See docs/superpowers/specs/2026-09-30-leave-management-design.md.
"""
from __future__ import annotations

import re

from flask import Blueprint, jsonify, request

import leave_store

leave_bp = Blueprint("leave", __name__)

HR_USER_ID = "emp009"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _session_user_id() -> str:
    """The real logged-in user id from the session_token cookie, or "" if
    the cookie is missing or the session is invalid/expired. This is the
    ONLY source of truth for identity in this file -- never a query
    param or JSON body field, which is just a string the caller wrote."""
    from routes.auth import _verify_session
    token = request.cookies.get("session_token", "")
    return _verify_session(token) or ""


def _require_session() -> tuple[str, tuple | None]:
    """(user_id, None) on a valid session, or ("", 401 response) with no
    valid session at all. Used by every self-service route -- the acting
    user is always the session holder, never a body/query-supplied id."""
    uid = _session_user_id()
    if not uid:
        return "", (jsonify({"error": "login required"}), 401)
    return uid, None


def _require_hr() -> tuple[str, tuple | None]:
    """(user_id, None) when the session belongs to HR (emp009); otherwise
    an error response -- 401 with no valid session at all, 403 for a real,
    valid session that just isn't HR's."""
    uid = _session_user_id()
    if not uid:
        return "", (jsonify({"error": "login required"}), 401)
    if uid != HR_USER_ID:
        return "", (jsonify({"error": "HR only"}), 403)
    return uid, None


@leave_bp.route("/api/leave/balance", methods=["GET"])
def leave_balance():
    uid, err = _require_session()
    if err:
        return err
    year = request.args.get("year", type=int)
    return jsonify(leave_store.get_balance(uid, year=year))


@leave_bp.route("/api/leave/calendar", methods=["GET"])
def leave_calendar():
    uid, err = _require_session()
    if err:
        return err
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    if not year or not month or not (1 <= month <= 12):
        return jsonify({"error": "year and month (1-12) required"}), 400
    if not (2000 <= year <= 2100):
        return jsonify({"error": "year out of range"}), 400
    return jsonify({"days": leave_store.calendar_days(uid, year, month)})


@leave_bp.route("/api/leave/apply", methods=["POST"])
def leave_apply():
    uid, err = _require_session()
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
    uid, err = _require_session()
    if err:
        return err
    ok = leave_store.cancel_leave(leave_id, uid)
    if not ok:
        return jsonify({"error": "nothing pending to cancel for this id/user"}), 404
    return jsonify({"success": True})


@leave_bp.route("/api/leave/convert-ot", methods=["POST"])
def leave_convert_ot():
    """Self-service: convert the caller's own pending overtime into
    comp-off leave right now, instead of waiting for the nightly sweep.
    Whole 24h blocks convert; any leftover hours stay pending and carry
    forward (leave_store._convert_overtime_for_user)."""
    uid, err = _require_session()
    if err:
        return err
    return jsonify(leave_store.convert_overtime_now(uid))


@leave_bp.route("/api/leave/pending", methods=["GET"])
def leave_pending():
    uid, err = _require_hr()
    if err:
        return err
    return jsonify({"pending": leave_store.list_pending()})


@leave_bp.route("/api/leave/<int:leave_id>/approve", methods=["POST"])
def leave_approve(leave_id: int):
    uid, err = _require_hr()
    if err:
        return err
    ok = leave_store.approve_leave(leave_id, uid)
    if not ok:
        return jsonify({"error": "no pending request with that id"}), 404
    return jsonify({"success": True})


@leave_bp.route("/api/leave/<int:leave_id>/reject", methods=["POST"])
def leave_reject(leave_id: int):
    uid, err = _require_hr()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    ok = leave_store.reject_leave(leave_id, uid, reason=str(body.get("reason") or "")[:300])
    if not ok:
        return jsonify({"error": "no pending request with that id"}), 404
    return jsonify({"success": True})
