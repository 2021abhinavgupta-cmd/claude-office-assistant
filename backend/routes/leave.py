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
