"""
Task Delay Scheduler — runs daily to check overdue tasks and escalate risk levels.
Integrated into Flask via APScheduler on app startup.
"""
import logging
from datetime import datetime, date, timedelta
from db import get_connection

logger = logging.getLogger(__name__)

import json
from pathlib import Path

# Risk escalation thresholds (days overdue)
RISK_THRESHOLDS = {
    "day1": 1,   # Friendly reminder to assignee
    "day2": 2,   # Reminder + notify founder
    "day3": 3,   # Mark AT_RISK, alert founder
    "day5": 5,   # Mark CRITICAL
}

def _load_emp_names() -> dict:
    try:
        emp_path = Path(__file__).parent.parent / "config" / "employees.json"
        with open(emp_path) as f:
            return {e["id"]: e["name"] for e in json.load(f).get("employees", [])}
    except Exception:
        return {}

def _today_str():
    return date.today().isoformat()

def _days_overdue(due_date_str: str) -> int:
    try:
        due = date.fromisoformat(due_date_str)
        delta = date.today() - due
        return max(0, delta.days)
    except Exception:
        return 0

def check_overdue_tasks():
    """
    Called daily. Scans all non-approved tasks with a due_date.
    Updates risk level in task_risk table and logs alerts.
    """
    logger.info("🕐 Running daily overdue task check...")
    today = _today_str()
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT t.id, t.title, t.assigned_to, t.due_date, t.status, c.name as client_name
        FROM tasks t
        JOIN clients c ON t.client_id = c.id
        WHERE t.due_date IS NOT NULL
          AND t.due_date != ''
          AND t.status NOT IN ('approved', 'not_started')
    """)
    tasks = cur.fetchall()

    alerts_fired = []
    emp_names = _load_emp_names()

    for (tid, title, assignee, due_date, status, client_name) in tasks:
        days = _days_overdue(due_date)
        if days == 0:
            continue

        emp_name = emp_names.get(assignee, assignee)

        # Fetch current risk row
        cur.execute("SELECT risk_level, alerted_day1, alerted_day2, alerted_day3, alerted_day5, last_checked FROM task_risk WHERE task_id=?", (tid,))
        row = cur.fetchone()
        risk_level    = row[0] if row else "normal"
        alerted_day1  = row[1] if row else 0
        alerted_day2  = row[2] if row else 0
        alerted_day3  = row[3] if row else 0
        alerted_day5  = row[4] if row else 0
        last_checked  = row[5] if row else None

        # Resilience: skip if already fully processed today (#2)
        if last_checked == today:
            continue


        new_risk = risk_level
        new_alerts = {}

        if days >= RISK_THRESHOLDS["day5"] and not alerted_day5:
            new_risk = "critical"
            new_alerts["alerted_day5"] = 1
            alerts_fired.append({
                "level": "CRITICAL",
                "task": title,
                "client": client_name,
                "assignee": emp_name,
                "days": days,
                "message": f"🚨 CRITICAL: {emp_name}'s task '{title}' for {client_name} is {days} days overdue."
            })

        elif days >= RISK_THRESHOLDS["day3"] and not alerted_day3:
            new_risk = "at_risk"
            new_alerts["alerted_day3"] = 1
            alerts_fired.append({
                "level": "AT_RISK",
                "task": title,
                "client": client_name,
                "assignee": emp_name,
                "days": days,
                "message": f"🔴 AT RISK: {emp_name}'s task '{title}' for {client_name} is {days} days overdue. Founder notified."
            })

        elif days >= RISK_THRESHOLDS["day2"] and not alerted_day2:
            new_alerts["alerted_day2"] = 1
            alerts_fired.append({
                "level": "WARNING",
                "task": title,
                "client": client_name,
                "assignee": emp_name,
                "days": days,
                "message": f"⚠️ {emp_name}'s task '{title}' is {days} days overdue. Founder CC'd."
            })

        elif days >= RISK_THRESHOLDS["day1"] and not alerted_day1:
            new_alerts["alerted_day1"] = 1
            alerts_fired.append({
                "level": "REMINDER",
                "task": title,
                "client": client_name,
                "assignee": emp_name,
                "days": days,
                "message": f"📋 Reminder: {emp_name} has '{title}' due {days} day(s) ago."
            })

        if new_alerts:
            # Build UPSERT — stamp last_checked=today to prevent re-firing after restart (#2)
            fields = ", ".join(new_alerts.keys())
            placeholders = ", ".join(["?"] * len(new_alerts))
            updates = ", ".join(f"{k}=?" for k in new_alerts.keys())
            with conn:
                conn.execute(f"""
                    INSERT INTO task_risk (task_id, risk_level, updated_at, last_checked, {fields})
                    VALUES (?, ?, ?, ?, {placeholders})
                    ON CONFLICT(task_id) DO UPDATE SET
                        risk_level=excluded.risk_level,
                        updated_at=excluded.updated_at,
                        last_checked=excluded.last_checked,
                        {updates}
                """, (tid, new_risk, today, today, *new_alerts.values(), *new_alerts.values()))


    conn.close()

    if alerts_fired:
        for a in alerts_fired:
            logger.warning(f"[TASK ALERT][{a['level']}] {a['message']}")
    else:
        logger.info("✅ No new overdue alerts today.")

    return alerts_fired


def get_task_risk_levels() -> dict:
    """Return dict of task_id → risk_level for dashboard display."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT task_id, risk_level FROM task_risk")
    result = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()
    return result


def get_all_alerts(days_back: int = 7) -> list:
    """
    Return recent alert data for the founder dashboard.
    Pulls all tasks with risk_level != 'normal' and their current state.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT tr.task_id, tr.risk_level, tr.updated_at,
               t.title, t.assigned_to, t.due_date, t.status,
               c.name as client_name
        FROM task_risk tr
        JOIN tasks t ON tr.task_id = t.id
        JOIN clients c ON t.client_id = c.id
        WHERE tr.risk_level IN ('at_risk', 'critical')
          AND t.status NOT IN ('approved')
        ORDER BY CASE tr.risk_level WHEN 'critical' THEN 0 WHEN 'at_risk' THEN 1 ELSE 2 END
    """)
    rows = cur.fetchall()
    conn.close()
    
    emp_names = _load_emp_names()
    
    return [
        {
            "task_id":     r[0],
            "risk_level":  r[1],
            "updated_at":  r[2],
            "title":       r[3],
            "assigned_to": r[4],
            "assignee_name": emp_names.get(r[4], r[4]),
            "due_date":    r[5],
            "status":      r[6],
            "client_name": r[7],
        }
        for r in rows
    ]


def init_scheduler(app):
    """Call this once from app.py to register the background job."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        # Run daily at 8:00 AM
        scheduler.add_job(check_overdue_tasks, "cron", hour=8, minute=0,
                          id="daily_overdue_check", replace_existing=True)
        scheduler.start()
        logger.info("✅ Task delay scheduler started (runs daily at 08:00).")

        # Run immediately on startup to catch anything already overdue
        import threading
        threading.Thread(target=check_overdue_tasks, daemon=True).start()

        return scheduler
    except Exception as e:
        logger.warning(f"Scheduler failed to start (non-fatal): {e}")
        return None
