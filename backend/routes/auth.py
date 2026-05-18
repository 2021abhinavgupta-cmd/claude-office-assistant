import os
import json
import secrets
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
from flask import Blueprint, request, jsonify
import logging

from utils import _load_employees, _save_employees, _is_admin, now_ist, today_ist, IST

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__)

def _sessions_conn():
    from db import get_connection
    return get_connection()

def _attendance_conn():
    from db import get_connection
    return get_connection()

def _attendance_checkout(user_id: str):
    """Always updates checkout_time to latest IST logout (UPSERT)."""
    d = today_ist()
    t = now_ist()
    ts = datetime.now(IST).isoformat(timespec="seconds")
    conn = _attendance_conn()
    with conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO daily_attendance (user_id, date, checkout_time)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, date) DO UPDATE SET checkout_time = excluded.checkout_time""",
            (user_id, d, t),
        )
        conn.execute(
            "INSERT INTO attendance (user_id, action, timestamp) VALUES (?, 'out', ?)",
            (user_id, ts),
        )
        cur.execute(
            "SELECT checkout_time FROM daily_attendance WHERE user_id=? AND date=?",
            (user_id, d),
        )
    conn.close()
    return d, t

def _create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat() + "Z"
    conn = _sessions_conn()
    with conn:
        conn.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
                     (token, user_id, expires))
    conn.close()
    return token

def _verify_session(token: str) -> Optional[str]:
    """Returns user_id if token is valid and not expired, else None."""
    if not token:
        return None
    conn = _sessions_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, expires_at FROM sessions WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    user_id, expires_at = row
    if datetime.utcnow().isoformat() + "Z" > expires_at:
        return None  # Expired
    return user_id

@auth_bp.route("/api/auth/login", methods=["POST"])
def auth_login():
    """PIN login — returns a server-side session token."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "").strip()
    pin     = body.get("pin", "").strip()

    if not user_id or not pin:
        return jsonify({"error": "user_id and pin required"}), 400

    data = _load_employees()
    emp = next((e for e in data.get("employees", []) if e["id"] == user_id), None)
    if not emp:
        return jsonify({"error": "Employee not found"}), 404

    if emp.get("pin", "0000") != pin:
        return jsonify({"error": "Incorrect PIN"}), 401

    token = _create_session(user_id)
    resp = jsonify({
        "success": True,
        "token":   token,
        "user":    {"id": emp["id"], "name": emp["name"], "role": emp["role"],
                    "is_admin": _is_admin(user_id)},
    })
    # Set HttpOnly cookie so JS can't read the token (XSS protection)
    is_secure = os.getenv("FLASK_ENV", "development") != "development"
    resp.set_cookie(
        "session_token", token,
        httponly=True,
        samesite="Lax",
        secure=is_secure,
        max_age=30 * 24 * 60 * 60,  # 30 days
    )
    return resp

@auth_bp.route("/api/auth/verify", methods=["GET"])
def auth_verify():
    """Verify a session token. Used by frontend auth guard."""
    token = (
        request.cookies.get("session_token", "")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
        or request.headers.get("X-Session-Token", "")
        or request.args.get("token", "")
    )
    user_id = _verify_session(token)
    if not user_id:
        return jsonify({"valid": False}), 401
    data = _load_employees()
    emp = next((e for e in data.get("employees", []) if e["id"] == user_id), {})
    return jsonify({"valid": True, "user_id": user_id, "name": emp.get("name",""),
                    "role": emp.get("role",""), "is_admin": _is_admin(user_id)})

@auth_bp.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """Invalidate a session token and clear the HttpOnly cookie."""
    token = (
        request.cookies.get("session_token", "")
        or (request.get_json(silent=True) or {}).get("token", "")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
    )
    if token:
        user_id = _verify_session(token)
        if user_id:
            _attendance_checkout(user_id)
        conn = _sessions_conn()
        with conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.close()
    resp = jsonify({"success": True})
    resp.delete_cookie("session_token")
    return resp

@auth_bp.route("/api/auth/change_pin", methods=["POST"])
def auth_change_pin():
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    old_pin = body.get("old_pin")
    new_pin = body.get("new_pin")
    
    data = _load_employees()
    found = False
    for emp in data.get("employees", []):
        if emp.get("id") == user_id:
            if str(emp.get("pin")) != str(old_pin):
                return jsonify({"error": "Incorrect old PIN"}), 401
            emp["pin"] = new_pin
            found = True
            break
            
    if not found:
        return jsonify({"error": "Employee not found"}), 404
        
    try:
        _save_employees(data)
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Failed to save new PIN: {e}")
        return jsonify({"error": "Could not save PIN"}), 500
