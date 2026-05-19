"""
Standup, Alerts, Notion, Quick-Tasks, SQLite helpers, and Export Blueprint
Routes: /api/standup/*, /api/alerts/*, /api/notion/*, /api/quick-tasks, /api/sqlite/*, /api/export
"""
import logging
import re
from datetime import datetime

import notion_store
import task_scheduler
from flask import Blueprint, Response, jsonify, request, send_file
from utils import _is_admin

logger = logging.getLogger(__name__)
ops_bp = Blueprint("ops", __name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _su_conn():
    from db import get_connection
    return get_connection()

def _pt_conn():
    from db import get_connection
    return get_connection()

def _task_row_to_dict(row) -> dict:
    return {
        "id": row[0], "client_id": row[1], "title": row[2],
        "description": row[3], "assigned_to": row[4], "status": row[5],
        "progress": row[6], "due_date": row[7], "submission_note": row[8],
        "submission_file": row[9], "rejection_note": row[10],
        "submission_count": row[11], "opened_at": row[12], "created_at": row[13],
    }


# ══════════════════════════════════════════════════════════════════════════════
# STANDUP
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/standup", methods=["POST"])
def submit_standup():
    """Submit daily standup. One per user per day."""
    body = request.get_json(silent=True) or {}
    user_id   = body.get("user_id", "").strip()
    yesterday = body.get("yesterday", "").strip()
    today_txt = body.get("today", "").strip()
    blockers  = body.get("blockers", "").strip()

    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    if not yesterday and not today_txt:
        return jsonify({"error": "Provide at least one update field"}), 400

    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    conn = _su_conn()
    try:
        with conn:
            conn.execute("""
                INSERT INTO standups (user_id, date, yesterday, today, blockers, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, date) DO UPDATE SET
                    yesterday=excluded.yesterday,
                    today=excluded.today,
                    blockers=excluded.blockers,
                    submitted_at=excluded.submitted_at
            """, (user_id, date_str, yesterday, today_txt, blockers, datetime.utcnow().isoformat()+"Z"))
    finally:
        conn.close()
    return jsonify({"success": True, "date": date_str})


@ops_bp.route("/api/standup/today", methods=["GET"])
def get_standups_today():
    """Get all standups for a specific date (defaults to today). Founder view."""
    date_str = request.args.get("date") or datetime.utcnow().strftime("%Y-%m-%d")
    user_id  = request.args.get("user_id", "")
    conn = _su_conn()
    cur = conn.cursor()
    
    # Fetch text standups
    if user_id:
        cur.execute("SELECT user_id,date,yesterday,today,blockers,submitted_at FROM standups WHERE date=? AND user_id=?", (date_str, user_id))
    else:
        cur.execute("SELECT user_id,date,yesterday,today,blockers,submitted_at FROM standups WHERE date=? ORDER BY submitted_at", (date_str,))
    rows = cur.fetchall()
    
    # Fetch task lists
    if user_id:
        cur.execute("SELECT user_id, title, status, blocker FROM standup_tasks WHERE date=? AND user_id=? AND status != 'deleted' ORDER BY id ASC", (date_str, user_id))
    else:
        cur.execute("SELECT user_id, title, status, blocker FROM standup_tasks WHERE date=? AND status != 'deleted' ORDER BY id ASC", (date_str,))
    task_rows = cur.fetchall()
    
    conn.close()
    
    tasks_by_user = {}
    for uid, title, status, blocker in task_rows:
        if uid not in tasks_by_user:
            tasks_by_user[uid] = []
        tasks_by_user[uid].append({"title": title, "status": status, "blocker": blocker})

    EMP_NAMES = {"emp001":"Vidit","emp002":"Nupur","emp003":"Abhinav",
                 "emp004":"Kshitij","emp005":"Raj","emp006":"Mohit",
                 "emp007":"Tanaya","emp008":"Happy"}
    standups = [{"user_id":r[0],"name":EMP_NAMES.get(r[0],r[0]),"date":r[1],
                 "yesterday":r[2],"today":r[3],"blockers":r[4],"submitted_at":r[5]} for r in rows]
                 
    return jsonify({"standups": standups, "tasks_by_user": tasks_by_user, "date": date_str})


@ops_bp.route("/api/standup/history", methods=["GET"])
def get_standup_history():
    """Get standup history for a user (last 7 days)."""
    user_id = request.args.get("user_id", "")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("""SELECT user_id,date,yesterday,today,blockers,submitted_at
                   FROM standups WHERE user_id=? ORDER BY date DESC LIMIT 7""", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return jsonify({"history": [{"date":r[1],"yesterday":r[2],"today":r[3],"blockers":r[4],"submitted_at":r[5]} for r in rows]})


# ══════════════════════════════════════════════════════════════════════════════
# PERSONAL DAILY TASK TRACKER (separate from project tasks)
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/standup/my-tasks", methods=["GET"])
def get_my_tasks():
    """
    Get an employee's task list for a given date.
    Carried-over tasks from yesterday are automatically seeded when first
    fetching today if they don't exist yet.
    Query: user_id, date (optional — defaults to today UTC)
    """
    user_id  = request.args.get("user_id", "").strip()
    date_str = request.args.get("date", "") or datetime.utcnow().strftime("%Y-%m-%d")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    conn = _su_conn()
    cur  = conn.cursor()

    # Auto-carry-over: if no tasks exist for today, copy pending ones from yesterday
    cur.execute("SELECT id FROM standup_tasks WHERE user_id=? AND date=?", (user_id, date_str))
    existing = cur.fetchall()
    if not existing and date_str == datetime.utcnow().strftime("%Y-%m-%d"):
        from datetime import timedelta
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        cur.execute(
            "SELECT title, blocker FROM standup_tasks WHERE user_id=? AND date=? AND status='pending'",
            (user_id, yesterday),
        )
        pending = cur.fetchall()
        if pending:
            with conn:
                for title, blocker in pending:
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, carried_from, blocker) VALUES (?,?,?,'pending',?,?)",
                        (user_id, date_str, title, yesterday, blocker),
                    )

    import json
    cur.execute(
        "SELECT id, title, status, carried_from, created_at, blocker, notion_id, subtasks FROM standup_tasks WHERE user_id=? AND date=? ORDER BY id ASC",
        (user_id, date_str),
    )
    rows = cur.fetchall()
    conn.close()
    
    tasks = []
    for r in rows:
        if r[2] == "deleted": continue
        st = []
        try:
            st = json.loads(r[7]) if r[7] else []
        except: pass
        
        tasks.append({
            "id": r[0], "title": r[1], "status": r[2],
            "carried_from": r[3], "created_at": r[4], 
            "blocker": r[5], "notion_id": r[6], "subtasks": st
        })

    return jsonify({"tasks": tasks, "date": date_str})


@ops_bp.route("/api/standup/my-tasks", methods=["POST"])
def add_my_task():
    """
    Add a task to today's personal task list.
    Body: { user_id, title }
    """
    body    = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "").strip()
    title   = body.get("title", "").strip()
    notion_id = body.get("notion_id", None)
    if not user_id or not title:
        return jsonify({"error": "user_id and title required"}), 400

    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    conn = _su_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO standup_tasks (user_id, date, title, notion_id) VALUES (?, ?, ?, ?)",
            (user_id, date_str, title, notion_id),
        )
        task_id = cur.lastrowid
    conn.close()
    return jsonify({"success": True, "task_id": task_id, "date": date_str}), 201


@ops_bp.route("/api/standup/auto-fill", methods=["POST"])
def auto_fill_standup():
    """
    Fetch tasks from Notion that are active (in_progress) or due today, 
    and add them to today's standup list (avoiding duplicates).
    Body: { user_id, assigned_name }
    """
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "").strip()
    assigned_name = body.get("assigned_name", "").strip()
    
    if not user_id or not assigned_name:
        return jsonify({"error": "user_id and assigned_name required"}), 400

    try:
        import notion_store
        # Only works if Notion is configured
        if not notion_store.is_configured():
            return jsonify({"error": "Notion is not configured"}), 400
            
        all_tasks = notion_store.list_tasks(assigned_to=assigned_name)
    except Exception as e:
        logger.error(f"Failed to fetch Notion tasks for auto-fill: {e}")
        return jsonify({"error": str(e)}), 500

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    
    # Filter for tasks that are "in_progress" OR due today
    # Notion status uses human strings like "In Progress"
    valid_tasks = []
    for t in all_tasks:
        s = t.get("status", "").lower().replace(" ", "_").replace("-", "_")
        d = t.get("due_date", "")
        # Add if status is active, or if it's due today and not approved/submitted
        is_active = s == "in_progress"
        is_due_today = (d == today_str and s not in ("approved", "done", "submitted", "in_review", "pending_review"))
        
        if is_active or is_due_today:
            valid_tasks.append(t)

    if not valid_tasks:
        return jsonify({"success": True, "added": 0})

    conn = _su_conn()
    added_count = 0
    with conn:
        cur = conn.cursor()
        for vt in valid_tasks:
            nid = vt.get("notion_id")
            title = vt.get("title", "Untitled").strip()
            
            # Check if this task is already in today's standup (by notion_id or title)
            if nid:
                cur.execute("SELECT id FROM standup_tasks WHERE user_id=? AND date=? AND (notion_id=? OR title=?)", 
                            (user_id, today_str, nid, title))
            else:
                cur.execute("SELECT id FROM standup_tasks WHERE user_id=? AND date=? AND title=?", 
                            (user_id, today_str, title))
                            
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO standup_tasks (user_id, date, title, notion_id) VALUES (?, ?, ?, ?)",
                    (user_id, today_str, title, nid)
                )
                added_count += 1
    conn.close()
    
    return jsonify({"success": True, "added": added_count})


@ops_bp.route("/api/standup/my-tasks/<int:task_id>", methods=["PATCH"])
def update_my_task(task_id: int):
    """
    Update a task's status or blocker.
    Body: { status?: 'done' | 'pending', blocker?: str }
    """
    body   = request.get_json(silent=True) or {}
    status = body.get("status")
    blocker = body.get("blocker")
    title = body.get("title")
    progress = body.get("progress")  # optional progress override (int 0-100)
    subtasks = body.get("subtasks")  # list of dicts: [{"title": "x", "done": true}]
    
    updates = []
    params = []
    if status is not None:
        if status not in ("done", "pending"):
            return jsonify({"error": "status must be 'done' or 'pending'"}), 400
        updates.append("status=?")
        params.append(status)
    if blocker is not None:
        updates.append("blocker=?")
        params.append(blocker)
    if title is not None:
        updates.append("title=?")
        params.append(title.strip())
    if subtasks is not None:
        import json
        updates.append("subtasks=?")
        params.append(json.dumps(subtasks))

    if not updates:
        return jsonify({"error": "no updates provided"}), 400
        
    params.append(task_id)

    conn = _su_conn()
    with conn:
        conn.execute(f"UPDATE standup_tasks SET {', '.join(updates)} WHERE id=?", params)

    # Sync to Notion if progress is provided explicitly, OR if subtasks are updated
    cur = conn.cursor()
    cur.execute("SELECT notion_id, subtasks, status FROM standup_tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    conn.close()

    notion_id = row[0] if row else None
    current_subtasks_json = row[1] if row else '[]'
    current_status = row[2] if row else 'pending'

    if notion_id:
        try:
            import notion_store
            notion_status = None
            notion_progress = None
            
            # Explicit progress override
            if progress is not None and current_status == "done":
                notion_progress = int(progress)
                if notion_progress == 100: notion_status = "submitted"
                elif notion_progress > 0: notion_status = "in_progress"
            
            # Auto-calculate progress from subtasks
            elif subtasks is not None:
                import json
                st = json.loads(current_subtasks_json) if current_subtasks_json else []
                if st:
                    done_count = sum(1 for s in st if s.get("done"))
                    notion_progress = int((done_count / len(st)) * 100)
                    if notion_progress == 100: notion_status = "submitted"
                    elif notion_progress > 0: notion_status = "in_progress"
            
            if notion_progress is not None:
                notion_store.update_task(notion_id, progress=notion_progress, status=notion_status)
        except Exception as e:
            logger.warning(f"Notion sync failed for task {notion_id}: {e}")

    return jsonify({"success": True})


@ops_bp.route("/api/standup/my-tasks/<int:task_id>", methods=["DELETE"])
def delete_my_task(task_id: int):
    """Delete a task from the personal list."""
    conn = _su_conn()
    with conn:
        conn.execute("UPDATE standup_tasks SET status='deleted' WHERE id=?", (task_id,))
    conn.close()
    return jsonify({"success": True})


@ops_bp.route("/api/standup/carry-over", methods=["POST"])
def carry_over_tasks():
    """
    Manually trigger carry-over of today's pending tasks to tomorrow.
    Called when the employee wraps up their day.
    Body: { user_id }
    Returns: count of tasks carried over.
    """
    body    = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    from datetime import timedelta
    today    = datetime.utcnow().strftime("%Y-%m-%d")
    tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")

    conn = _su_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT title, blocker FROM standup_tasks WHERE user_id=? AND date=? AND status='pending'",
        (user_id, today),
    )
    pending = cur.fetchall()

    carried = 0
    if pending:
        with conn:
            for title, blocker in pending:
                # Avoid duplicating if already carried (idempotent)
                cur2 = conn.cursor()
                cur2.execute(
                    "SELECT id FROM standup_tasks WHERE user_id=? AND date=? AND title=? AND carried_from=?",
                    (user_id, tomorrow, title, today),
                )
                if not cur2.fetchone():
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, carried_from, blocker) VALUES (?,?,?,'pending',?,?)",
                        (user_id, tomorrow, title, today, blocker),
                    )
                    carried += 1
    conn.close()
    logger.info(f"Carry-over: {user_id} → {carried} tasks moved to {tomorrow}")
    return jsonify({"success": True, "carried": carried, "date": tomorrow})




# ══════════════════════════════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/alerts", methods=["GET"])
def get_alerts():
    """Return AT_RISK and CRITICAL tasks for founder dashboard."""
    user_id = request.args.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    alerts = task_scheduler.get_all_alerts()
    return jsonify({"alerts": alerts})


@ops_bp.route("/api/alerts/run-check", methods=["POST"])
def run_alert_check():
    """Manually trigger the overdue check. Admin only."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    fired = task_scheduler.check_overdue_tasks()
    return jsonify({"success": True, "alerts_fired": len(fired), "details": fired})


# ══════════════════════════════════════════════════════════════════════════════
# NOTION
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/notion/status", methods=["GET"])
def notion_status():
    configured = notion_store.is_configured()
    return jsonify({
        "configured": configured,
        "message": "Notion is connected" if configured else (
            "Set NOTION_TOKEN, NOTION_CLIENTS_DB_ID, NOTION_TASKS_DB_ID in config/.env"
        ),
    })


@ops_bp.route("/api/notion/clients", methods=["GET"])
def notion_list_clients():
    status_filter = request.args.get("status", "")
    clients = notion_store.list_clients(status_filter=status_filter)
    return jsonify({"clients": clients, "count": len(clients)})


@ops_bp.route("/api/notion/clients", methods=["POST"])
def notion_create_client():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not notion_store.is_configured():
        return jsonify({"error": "Notion is not configured. Add NOTION_TOKEN and DB IDs to config/.env"}), 503

    client = notion_store.create_client(
        name=name,
        contact=body.get("contact", ""),
        requirements=body.get("requirements", ""),
        deadline=body.get("deadline", ""),
        budget=body.get("budget", ""),
        notes=body.get("notes", ""),
        status="active",
    )
    if not client:
        return jsonify({"error": "Failed to create client in Notion"}), 500

    EMP_NAMES = {
        "emp001": "Vidit", "emp002": "Nupur", "emp003": "Abhinav",
        "emp004": "Kshitij", "emp005": "Raj", "emp006": "Mohit",
        "emp007": "Tanaya", "emp008": "Happy",
    }
    SVC_TASKS = {
        "content":  [("Content Brief & Research", "emp006"), ("Write Copy / Content Draft", "emp006"), ("Content Review & Approval", "emp004")],
        "video":    [("Video Script Writing", "emp006"), ("Video Shoot / Production", "emp005"), ("Video Editing & Post", "emp005"), ("AI Video Enhancements", "emp008")],
        "design":   [("Design Brief & Moodboard", "emp002"), ("UI/UX Design — Wireframes", "emp002"), ("Final Design Handoff", "emp002")],
        "website":  [("Website Architecture Plan", "emp001"), ("Frontend Development", "emp003"), ("Backend / Integrations", "emp001"), ("QA & Launch", "emp003")],
        "social":   [("Social Media Strategy", "emp006"), ("Content Calendar", "emp006"), ("Graphics & Templates", "emp002")],
        "accounts": [("Invoice & Payment Setup", "emp007"), ("Monthly Reporting", "emp007")],
    }
    deadline = body.get("deadline", "")
    tasks_created = 0
    custom_tasks = body.get("tasks", [])

    if custom_tasks:
        for t in custom_tasks:
            task_title = t.get("title", "").strip()
            if not task_title:
                continue
            emp_ids   = [e.strip() for e in t.get("who", "emp001").split(",")]
            emp_names = ", ".join(EMP_NAMES.get(e, e) for e in emp_ids)
            svc_map   = {"emp001":"website","emp002":"design","emp003":"website",
                         "emp004":"accounts","emp005":"video","emp006":"content",
                         "emp007":"accounts","emp008":"video"}
            first_emp = emp_ids[0] if emp_ids else "emp001"
            result = notion_store.create_task(
                title=task_title, client_name=name,
                client_notion_id=client["notion_id"], assigned_to=emp_names,
                due_date=t.get("due_date", "") or deadline,
                status="not_started", service=svc_map.get(first_emp, "general"),
            )
            if result:
                tasks_created += 1
    else:
        for svc in body.get("services", []):
            for (task_title, emp_id) in SVC_TASKS.get(svc, []):
                result = notion_store.create_task(
                    title=task_title, client_name=name,
                    client_notion_id=client["notion_id"],
                    assigned_to=EMP_NAMES.get(emp_id, emp_id),
                    due_date=deadline, status="not_started", service=svc,
                )
                if result:
                    tasks_created += 1

    logger.info(f"Notion: onboarded client '{name}' with {tasks_created} tasks")
    return jsonify({
        "success": True,
        "notion_id": client["notion_id"],
        "name": name,
        "tasks_created": tasks_created,
        "notion_url": f"https://notion.so/{client['notion_id'].replace('-', '')}",
    })


@ops_bp.route("/api/notion/tasks", methods=["GET"])
def notion_list_tasks():
    tasks = notion_store.list_tasks(
        assigned_to=request.args.get("assigned_to", ""),
        client_notion_id=request.args.get("client_id", ""),
        status_filter=request.args.get("status", ""),
    )
    return jsonify({"tasks": tasks, "count": len(tasks)})


@ops_bp.route("/api/notion/tasks/<string:notion_id>", methods=["PATCH"])
def notion_update_task(notion_id: str):
    body   = request.get_json(silent=True) or {}
    result = notion_store.update_task(
        notion_id=notion_id, status=body.get("status"),
        progress=body.get("progress"), submission_note=body.get("submission_note"),
        assigned_to=body.get("assigned_to"), new_title=body.get("new_title"),
        due_date=body.get("due_date"), task_title=body.get("task_title", ""),
        assignee=body.get("assignee", ""), client_name=body.get("client_name", ""),
    )
    if result:
        return jsonify({"success": True})
    return jsonify({"error": "Failed to update task in Notion"}), 500


@ops_bp.route("/api/notion/tasks/<string:notion_id>", methods=["DELETE"])
def notion_delete_task(notion_id: str):
    if notion_store.archive_notion_page(notion_id):
        return jsonify({"success": True})
    return jsonify({"error": "Failed to delete task"}), 500


@ops_bp.route("/api/notion/clients/<string:notion_id>", methods=["DELETE"])
def notion_delete_client(notion_id: str):
    all_client_tasks = notion_store.list_tasks(client_notion_id=notion_id)
    for t in all_client_tasks:
        tid = t.get("notion_id") or t.get("id")
        if tid:
            notion_store.archive_notion_page(tid)
    if notion_store.archive_notion_page(notion_id):
        return jsonify({"success": True})
    return jsonify({"error": "Failed to delete client"}), 500


@ops_bp.route("/api/notion/dashboard", methods=["GET"])
def notion_dashboard():
    data = notion_store.get_dashboard_data()
    return jsonify(data)


# ══════════════════════════════════════════════════════════════════════════════
# QUICK TASKS
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/quick-tasks", methods=["GET"])
def get_quick_tasks():
    if notion_store.is_configured():
        tasks = notion_store.list_tasks(client_notion_id="__quick__")
        return jsonify({"tasks": tasks})
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id,client_id,title,description,assigned_to,status,progress,
               due_date,submission_note,submission_file,rejection_note,
               submission_count,opened_at,created_at
        FROM tasks WHERE client_id IS NULL ORDER BY created_at DESC
    """)
    tasks = [_task_row_to_dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({"tasks": tasks})


@ops_bp.route("/api/quick-tasks", methods=["POST"])
def save_quick_task():
    body = request.get_json(silent=True) or {}
    title       = body.get("title", "").strip()
    assigned_to = body.get("assigned_to", "")
    due_date    = body.get("due_date", "")
    notes       = body.get("notes", "")
    if not title:
        return jsonify({"error": "title required"}), 400

    if notion_store.is_configured():
        result = notion_store.create_task(
            title=title, client_name="Quick Task", client_notion_id="__quick__",
            assigned_to=assigned_to, due_date=due_date, status="not_started",
        )
        if result:
            return jsonify({"success": True, "notion_id": result["notion_id"]}), 201
        return jsonify({"error": "Failed to create quick task in Notion"}), 500

    conn = _pt_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks (client_id,title,description,assigned_to,due_date,status,progress) VALUES (?,?,?,?,?,'not_started',0)",
            (None, title, notes, assigned_to, due_date)
        )
        task_id = cur.lastrowid
    conn.close()
    return jsonify({"success": True, "task_id": task_id}), 201


# ══════════════════════════════════════════════════════════════════════════════
# SQLITE FALLBACK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/sqlite/tasks/<int:task_id>", methods=["PATCH"])
def sqlite_patch_task(task_id: int):
    body = request.get_json(silent=True) or {}
    updates, vals = [], []
    for field, col in [("new_title","title"),("assigned_to","assigned_to"),
                       ("due_date","due_date"),("status","status"),("progress","progress")]:
        if field in body:
            updates.append(f"{col}=?"); vals.append(body[field])
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    vals.append(task_id)
    conn = _pt_conn()
    with conn:
        conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", vals)
    conn.close()
    return jsonify({"success": True})


@ops_bp.route("/api/sqlite/tasks/<int:task_id>", methods=["DELETE"])
def sqlite_delete_task(task_id: int):
    conn = _pt_conn()
    with conn:
        conn.execute("DELETE FROM dependencies WHERE task_id=? OR depends_on_task_id=?", (task_id, task_id))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.close()
    return jsonify({"success": True})


@ops_bp.route("/api/sqlite/clients/<int:client_id>", methods=["DELETE"])
def sqlite_delete_client(client_id: int):
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM tasks WHERE client_id=?", (client_id,))
    task_ids = [r[0] for r in cur.fetchall()]
    with conn:
        for tid in task_ids:
            conn.execute("DELETE FROM dependencies WHERE task_id=? OR depends_on_task_id=?", (tid, tid))
        conn.execute("DELETE FROM tasks WHERE client_id=?", (client_id,))
        conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
    conn.close()
    return jsonify({"success": True})


# ══════════════════════════════════════════════════════════════════════════════
# DOCUMENT EXPORT
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/export", methods=["POST"])
def export_document():
    """Convert AI-generated markdown to a downloadable DOCX, PDF, or PPTX file."""
    from document_exporter import export_docx, export_pdf, export_pptx

    body    = request.get_json(silent=True) or {}
    content = body.get("content", "").strip()
    fmt     = body.get("format", "pdf").lower()
    title   = body.get("title", "Claude Export")[:120]

    if not content:
        return jsonify({"error": "No content provided"}), 400
    if fmt not in ("docx", "pdf", "pptx"):
        return jsonify({"error": "Unsupported format. Use docx, pdf, or pptx."}), 400

    try:
        if fmt == "docx":
            buf = export_docx(content, title=title)
            mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ext = "docx"
        elif fmt == "pdf":
            buf = export_pdf(content, title=title)
            mimetype = "application/pdf"
            ext = "pdf"
        else:
            buf = export_pptx(content, title=title)
            mimetype = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ext = "pptx"

        safe_name = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_") or "export"
        return send_file(buf, mimetype=mimetype, as_attachment=True,
                         download_name=f"{safe_name}.{ext}")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception:
        logger.exception("Export failed")
        return jsonify({"error": "Export failed"}), 500


@ops_bp.route("/api/export/standup-tasks", methods=["GET"])
def export_standup_tasks():
    """Export all personal standup tasks to a CSV file."""
    import csv
    from io import StringIO
    from flask import Response
    
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, date, title, status, blocker, carried_from, created_at FROM standup_tasks ORDER BY date DESC, user_id ASC")
    rows = cur.fetchall()
    conn.close()

    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["user_id", "date", "title", "status", "blocker", "carried_from", "created_at"])
    cw.writerows(rows)
    
    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=standup_tasks_export.csv"}
    )
