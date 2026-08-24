"""
Attendance & Employee Blueprint
Routes: /api/attendance/*, /api/employees/*
"""
import csv
import json
import logging
import re
from datetime import datetime, time as dt_time
from io import StringIO

from flask import Blueprint, Response, jsonify, request

from utils import (IST, _is_admin, _load_employees, _save_employees,
                   now_ist, today_ist)

logger = logging.getLogger(__name__)
attendance_bp = Blueprint("attendance", __name__)

# Work-day window for the daily_attendance summary (checkin_time/checkout_time
# -- what the Live Attendance dashboard cards and "Total" hours are computed
# from). A login/logout outside this window (e.g. checking Lumina at
# midnight) still happened and is still logged verbatim in the raw
# `attendance` audit table, but must not be allowed to stretch the day's
# recorded work window earlier/later than the business day actually runs.
WORK_START = dt_time(9, 0, 0)
WORK_END = dt_time(22, 30, 0)


def _clamp_work_time(hhmmss: str) -> str:
    try:
        h, m, s = (int(p) for p in hhmmss.split(":"))
        actual = dt_time(h, m, s)
    except Exception:
        return hhmmss
    if actual < WORK_START:
        return WORK_START.strftime("%H:%M:%S")
    if actual > WORK_END:
        return WORK_END.strftime("%H:%M:%S")
    return hhmmss


# ── DB helpers ────────────────────────────────────────────────────────────────

def _attendance_conn():
    from db import get_connection
    return get_connection()


def _attendance_payload():
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return body
    raw = request.get_data(cache=False, as_text=True) or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _attendance_checkin(user_id: str):
    """First IST login of day wins; ON CONFLICT DO NOTHING prevents overwriting.
    checkin_time is clamped to the work-day window (see WORK_START/WORK_END)
    so a login before 9am doesn't record the day as having started at 2am."""
    d = today_ist()
    t = _clamp_work_time(now_ist())
    ts = datetime.now(IST).isoformat(timespec="seconds")
    conn = _attendance_conn()
    with conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO daily_attendance (user_id, date, checkin_time)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, date) DO NOTHING""",
            (user_id, d, t),
        )
        if cur.rowcount > 0:
            conn.execute(
                "INSERT INTO attendance (user_id, action, timestamp) VALUES (?, 'in', ?)",
                (user_id, ts),
            )
        cur.execute(
            "SELECT checkin_time FROM daily_attendance WHERE user_id=? AND date=?",
            (user_id, d),
        )
        row = cur.fetchone()
        stored_checkin = row[0] if row else t
    conn.close()
    return d, stored_checkin


def _attendance_ping(user_id: str) -> str:
    """Lightweight presence heartbeat -- called every 60s while a Lumina page
    is open (see auth.js). Only writes last_seen_at, nothing else. Real
    checkout is inferred later by sweep_stale_checkouts() (task_scheduler.py)
    from staleness of this timestamp, NOT from any unload/pagehide event --
    that approach was tried and reverted (see CLAUDE.md gotcha #70/#71: a
    pagehide-triggered checkout write fires on every internal page
    navigation, not just a real tab close, and the resulting write volume
    was implicated in a production DB hang)."""
    d = today_ist()
    t = now_ist()
    conn = _attendance_conn()
    with conn:
        conn.execute(
            """INSERT INTO daily_attendance (user_id, date, last_seen_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, date) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
            (user_id, d, t),
        )
    conn.close()
    return t


def _attendance_checkout(user_id: str):
    """Always updates checkout_time to latest IST logout (UPSERT).
    checkout_time is clamped to the work-day window (see WORK_START/WORK_END)
    so a logout after 10:30pm doesn't stretch the recorded work day later."""
    d = today_ist()
    t = _clamp_work_time(now_ist())
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
        row = cur.fetchone()
        stored_checkout = row[0] if row else t
    conn.close()
    return d, stored_checkout


# ── Attendance routes ─────────────────────────────────────────────────────────

@attendance_bp.route("/api/attendance/checkin", methods=["POST"])
def attendance_checkin():
    body = _attendance_payload()
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    date_ist, checkin_time = _attendance_checkin(user_id)
    return jsonify({
        "success": True,
        "user_id": user_id,
        "date": date_ist,
        "checkin_time": checkin_time,
        "timezone": "IST",
    })


@attendance_bp.route("/api/attendance/checkout", methods=["POST"])
def attendance_checkout():
    body = _attendance_payload()
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    date_ist, checkout_time = _attendance_checkout(user_id)
    return jsonify({
        "success": True,
        "user_id": user_id,
        "date": date_ist,
        "checkout_time": checkout_time,
        "timezone": "IST",
    })


@attendance_bp.route("/api/attendance/ping", methods=["POST"])
def attendance_ping():
    body = _attendance_payload()
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    last_seen = _attendance_ping(user_id)
    return jsonify({"success": True, "last_seen_at": last_seen})


def sweep_stale_checkouts(stale_after_seconds: int = 240):
    """Called by task_scheduler.py every ~3min. For today's rows with a
    checkin but no checkout, whose last_seen_at is older than
    stale_after_seconds, mark checkout_time = last_seen_at -- i.e. the
    employee's tab went away (closed / crashed / lost network) and their
    heartbeat simply stopped. Explicit Logout (attendance_checkout above)
    already handles the instant case; this is the fallback for everyone
    who just closes the tab."""
    d = today_ist()
    now_dt = datetime.strptime(now_ist(), "%H:%M:%S")
    conn = _attendance_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT user_id, last_seen_at FROM daily_attendance
           WHERE date=? AND checkin_time IS NOT NULL
             AND checkout_time IS NULL AND last_seen_at IS NOT NULL""",
        (d,),
    )
    rows = cur.fetchall()
    swept = 0
    with conn:
        for user_id, last_seen_at in rows:
            try:
                last_seen_dt = datetime.strptime(last_seen_at, "%H:%M:%S")
            except ValueError:
                continue
            if (now_dt - last_seen_dt).total_seconds() >= stale_after_seconds:
                conn.execute(
                    "UPDATE daily_attendance SET checkout_time=? WHERE user_id=? AND date=?",
                    (_clamp_work_time(last_seen_at), user_id, d),
                )
                swept += 1
    conn.close()
    return swept


@attendance_bp.route("/api/attendance/summary", methods=["GET"])
def attendance_summary():
    user_id = request.args.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    conn = _attendance_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT date, checkin_time, checkout_time FROM daily_attendance WHERE user_id=? ORDER BY date DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return jsonify({
        "user_id": user_id,
        "timezone": "IST",
        "records": [
            {"date": r[0], "checkin_time": r[1], "checkout_time": r[2]}
            for r in rows
        ],
    })


@attendance_bp.route("/api/attendance/today", methods=["GET"])
def attendance_today():
    date_ist = today_ist()
    conn = _attendance_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, checkin_time, checkout_time FROM daily_attendance WHERE date=?",
        (date_ist,),
    )
    rows = cur.fetchall()
    conn.close()
    records = [
        {"user_id": r[0], "date": date_ist, "checkin_time": r[1], "checkout_time": r[2]}
        for r in rows
    ]
    return jsonify({"date": date_ist, "timezone": "IST", "records": records})


@attendance_bp.route("/api/attendance/logs", methods=["GET"])
def attendance_logs():
    admin_id = request.args.get("user_id")
    if not _is_admin(admin_id):
        return jsonify({"error": "Unauthorized"}), 403
    from db import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, action, timestamp FROM attendance ORDER BY timestamp DESC LIMIT 500")
    logs = [{"id": r[0], "user_id": r[1], "action": r[2], "timestamp": r[3]} for r in cursor.fetchall()]
    conn.close()
    return jsonify({"logs": logs})


@attendance_bp.route("/api/attendance/export", methods=["GET"])
def attendance_export():
    admin_id = request.args.get("user_id")
    if not _is_admin(admin_id):
        return "Unauthorized", 403

    # daily_attendance (not the raw attendance event log) is already one row
    # per user per day with checkin_time/checkout_time -- both stored as
    # plain HH:MM:SS IST (see now_ist()/_clamp_work_time() above), so no
    # timezone conversion is needed here.
    from db import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT date, user_id, checkin_time, checkout_time FROM daily_attendance ORDER BY date DESC, user_id"
    )
    rows = cursor.fetchall()

    # Standup task counts per (user_id, date), same definitions the Velocity
    # chart uses (routes/ops.py::get_velocity) so the two stay consistent:
    # completed = marked done that day; carried = still-pending tasks that
    # were carried in from an earlier day (not a new task started that day).
    cursor.execute(
        """SELECT user_id, date,
                  SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN carried_from IS NOT NULL AND status='pending' THEN 1 ELSE 0 END) AS carried
           FROM standup_tasks
           WHERE status != 'deleted'
           GROUP BY user_id, date"""
    )
    task_counts = {(r[0], r[1]): (r[2] or 0, r[3] or 0) for r in cursor.fetchall()}
    conn.close()

    emp_map = {}
    try:
        emps = _load_employees().get("employees", [])
        for e in emps:
            emp_map[e["id"]] = e["name"]
            if e.get("whatsapp"):
                emp_map[e["whatsapp"]] = e["name"]
                emp_map[e["whatsapp"].replace('+', '')] = e["name"]
    except Exception:
        pass

    def format_user(uid):
        if uid in emp_map:
            return emp_map[uid]
        if re.match(r"^\+?\d{10,15}$", uid):
            return f"WhatsApp ({uid[-4:]})"
        return uid

    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["Date", "In", "Out", "Employee", "Tasks Completed", "Tasks Carried Forward"])
    for date, user_id, checkin_time, checkout_time in rows:
        completed, carried = task_counts.get((user_id, date), (0, 0))
        cw.writerow([date, checkin_time or "", checkout_time or "", format_user(user_id), completed, carried])

    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=claude_attendance_logs.csv"}
    )


# ── Employee routes ───────────────────────────────────────────────────────────

@attendance_bp.route("/api/employees", methods=["GET"])
def get_employees():
    """Returns all employees with their current status."""
    data = _load_employees()
    return jsonify(data)


@attendance_bp.route("/api/employees/checkin", methods=["POST"])
def employee_checkin():
    """
    Records an employee check-in or check-out.
    Body: { emp_id OR whatsapp, action: 'in'|'out', notes? }
    """
    body     = request.get_json(silent=True) or {}
    emp_id   = body.get("emp_id", "").strip()
    whatsapp = body.get("whatsapp", "").strip()
    action   = body.get("action", "in").strip()
    notes    = body.get("notes", "")

    if not emp_id and not whatsapp:
        return jsonify({"error": "emp_id or whatsapp required"}), 400

    data = _load_employees()
    found = None
    for emp in data["employees"]:
        if emp_id and emp.get("id") == emp_id:
            found = emp
            break
        if whatsapp and emp.get("whatsapp") == whatsapp:
            found = emp
            break

    if not found:
        return jsonify({"error": "Employee not found"}), 404

    entry = {
        "timestamp": datetime.now(IST).isoformat(timespec="seconds"),
        "action":    action,
        "notes":     notes,
    }
    found.setdefault("checkins", []).append(entry)
    found["last_action"] = action
    found["last_seen"]   = entry["timestamp"]

    _save_employees(data)
    if action == "out":
        date_ist, time_ist = _attendance_checkout(found.get("id", ""))
    else:
        date_ist, time_ist = _attendance_checkin(found.get("id", ""))
    logger.info(f"Employee {found['name']} checked {action} at {entry['timestamp']}")

    return jsonify({
        "success":  True,
        "employee": found["name"],
        "action":   action,
        "time":     entry["timestamp"],
        "date":     date_ist,
        "time_ist": time_ist,
        "timezone": "IST",
    })


@attendance_bp.route("/api/employees/summary", methods=["GET"])
def employee_summary():
    """Returns today's attendance summary."""
    data  = _load_employees()
    today = today_ist()
    conn = _attendance_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, checkin_time, checkout_time FROM daily_attendance WHERE date=?",
        (today,),
    )
    attendance_map = {r[0]: {"checkin_time": r[1], "checkout_time": r[2]} for r in cur.fetchall()}
    conn.close()

    summary = []
    for emp in data["employees"]:
        daily = attendance_map.get(emp.get("id", ""), {})
        checkin_time = daily.get("checkin_time")
        checkout_time = daily.get("checkout_time")
        if checkin_time and not checkout_time:
            status = "in"
        elif checkin_time and checkout_time:
            status = "out"
        else:
            status = "not checked in"
        summary.append({
            "emp_id":        emp.get("id", ""),
            "name":          emp["name"],
            "role":          emp["role"],
            "department":    emp["department"],
            "status":        status,
            "checkin_time":  checkin_time,
            "checkout_time": checkout_time,
            "today_logs":    [],
        })

    return jsonify({"date": today, "employees": summary, "total": len(summary)})
