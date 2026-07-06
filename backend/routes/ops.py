"""
Standup, Alerts, Notion, Quick-Tasks, SQLite helpers, and Export Blueprint
Routes: /api/standup/*, /api/alerts/*, /api/notion/*, /api/quick-tasks, /api/sqlite/*, /api/export
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta

import notion_store
import task_scheduler
from flask import Blueprint, Response, jsonify, request, send_file
from utils import _load_employees, _save_employees, now_ist, today_ist, IST
from werkzeug.security import generate_password_hash
from routes.auth import _verify_session
from utils import _is_admin

logger = logging.getLogger(__name__)
ops_bp = Blueprint("ops", __name__)


def _claude_call(system: str, user: str, max_tokens: int = 1024) -> str:
    """Call System API using Haiku model for fast, cheap task assistant responses."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        model = os.getenv("HAIKU_MODEL", "claude-haiku-4-5")
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"System API call failed: {e}")
        raise


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
    
    # Auto-carry-over for all users if querying today
    if date_str == datetime.utcnow().strftime("%Y-%m-%d") and not user_id:
        cur.execute("SELECT DISTINCT user_id FROM standup_tasks WHERE status='pending' AND date < ? AND user_id NOT IN (SELECT user_id FROM standup_tasks WHERE date=?)", (date_str, date_str))
        missing_users = [r[0] for r in cur.fetchall()]
        for u in missing_users:
            cur.execute("SELECT MAX(date) FROM standup_tasks WHERE user_id=? AND date<?", (u, date_str))
            last_date_row = cur.fetchone()
            if last_date_row and last_date_row[0]:
                last_date = last_date_row[0]
                cur.execute("SELECT title, blocker, carried_from, notion_id, due_date, subtasks, delegated_to, delegated_from FROM standup_tasks WHERE user_id=? AND date=? AND status='pending'", (u, last_date))
                pending = cur.fetchall()
                if pending:
                    with conn:
                        for title, blocker, carried_from, nid, dd, sub, d_to, d_from in pending:
                            orig_carry_from = carried_from if carried_from else last_date
                            conn.execute("INSERT INTO standup_tasks (user_id, date, title, status, carried_from, blocker, notion_id, due_date, subtasks, delegated_to, delegated_from) VALUES (?,?,?,'pending',?,?,?,?,?,?,?)", (u, date_str, title, orig_carry_from, blocker, nid, dd, sub, d_to, d_from))

    # Fetch text standups
    if user_id:
        cur.execute("SELECT user_id,date,yesterday,today,blockers,submitted_at FROM standups WHERE date=? AND user_id=?", (date_str, user_id))
    else:
        cur.execute("SELECT user_id,date,yesterday,today,blockers,submitted_at FROM standups WHERE date=? ORDER BY submitted_at", (date_str,))
    rows = cur.fetchall()
    
    # Fetch task lists
    if user_id:
        cur.execute("SELECT user_id, title, status, blocker, carried_from, subtasks, notion_id, due_date FROM standup_tasks WHERE date=? AND user_id=? AND status NOT IN ('deleted', 'delegated') ORDER BY CASE WHEN due_date IS NULL OR due_date = '' THEN '9999-12-31' ELSE due_date END ASC, id ASC", (date_str, user_id))
    else:
        cur.execute("SELECT user_id, title, status, blocker, carried_from, subtasks, notion_id, due_date FROM standup_tasks WHERE date=? AND status NOT IN ('deleted', 'delegated') ORDER BY CASE WHEN due_date IS NULL OR due_date = '' THEN '9999-12-31' ELSE due_date END ASC, id ASC", (date_str,))
    task_rows = cur.fetchall()
    
    conn.close()
    
    tasks_by_user = {}
    for uid, title, status, blocker, carried_from, subtasks_json, notion_id, due_date in task_rows:
        if uid not in tasks_by_user:
            tasks_by_user[uid] = []
            
        st = []
        try:
            st = json.loads(subtasks_json) if subtasks_json else []
        except: pass
            
        tasks_by_user[uid].append({"id": len(tasks_by_user[uid]), "title": title, "status": status, "blocker": blocker, "carried_from": carried_from, "subtasks": st, "notion_id": notion_id, "due_date": due_date})

    # Sort each user's tasks chronologically
    import re
    def parse_date_for_sort(d):
        if not d:
            return "9999-12-31"
        if re.match(r"^\d{2}-\d{2}-\d{4}$", d.strip()):
            return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
        if re.match(r"^\d{2}/\d{2}/\d{4}$", d.strip()):
            return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
        return d.strip()

    for uid in tasks_by_user:
        tasks_by_user[uid].sort(key=lambda x: (parse_date_for_sort(x["due_date"]), x["id"]))

    # Load employee names dynamically from employees.json
    try:
        _emp_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "employees.json")
        with open(_emp_path, "r", encoding="utf-8") as _f:
            _emp_data = json.load(_f)
        EMP_NAMES = {e["id"]: e["name"] for e in _emp_data.get("employees", [])}
    except Exception:
        EMP_NAMES = {"emp001":"Vidit","emp002":"Nupur","emp003":"Abhinav",
                     "emp004":"Kshitij","emp006":"Mohit",
                     "emp007":"Palak","emp008":"Happy"}
    standups = [{"user_id":r[0],"name":EMP_NAMES.get(r[0],r[0]),"date":r[1],
                 "yesterday":r[2],"today":r[3],"blockers":r[4],"submitted_at":r[5]} for r in rows]
                 
    return jsonify({"standups": standups, "tasks_by_user": tasks_by_user, "date": date_str})


@ops_bp.route("/api/standup/ai-coach", methods=["POST"])
def ai_priority_advisor():
    """Provides AI priority advice based on the user's tasks."""
    body = request.get_json(silent=True) or {}
    question = body.get("question", "")
    assigned_name = body.get("assigned_name", "")
    tasks = body.get("tasks", [])
    
    if not tasks:
        return jsonify({"reply": "I don't see any active tasks for you right now. You're all caught up!"})
        
    system_prompt = f"You are an AI priority advisor for {assigned_name}. The user will provide a list of their tasks and ask a question. Provide a direct, actionable, and encouraging answer."
    user_prompt = f"Here are my current tasks:\n" + "\n".join(tasks) + f"\n\nQuestion: {question}"
    
    reply = _claude_call(system_prompt, user_prompt)
    return jsonify({"reply": reply})


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


@ops_bp.route("/api/standup/velocity", methods=["GET"])
def get_velocity():
    """
    GET /api/standup/velocity?user_id=&days=14
    Returns per-day counts of completed vs pending/carried tasks.
    If user_id is omitted, returns team-wide aggregates.
    """
    user_id = request.args.get("user_id", "").strip()
    days    = int(request.args.get("days", 14))

    conn = _su_conn()
    cur  = conn.cursor()

    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    if user_id:
        cur.execute("""
            SELECT date,
                   SUM(CASE WHEN status='done' THEN 1 ELSE 0 END)     AS completed,
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END)   AS pending,
                   SUM(CASE WHEN carried_from IS NOT NULL AND status='pending' THEN 1 ELSE 0 END) AS carried
            FROM standup_tasks
            WHERE user_id=? AND date >= ? AND status != 'deleted'
            GROUP BY date ORDER BY date ASC
        """, (user_id, since))
    else:
        cur.execute("""
            SELECT date,
                   SUM(CASE WHEN status='done' THEN 1 ELSE 0 END)     AS completed,
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END)   AS pending,
                   SUM(CASE WHEN carried_from IS NOT NULL AND status='pending' THEN 1 ELSE 0 END) AS carried
            FROM standup_tasks
            WHERE date >= ? AND status != 'deleted'
            GROUP BY date ORDER BY date ASC
        """, (since,))

    rows = cur.fetchall()
    conn.close()

    data = [{"date": r[0], "completed": r[1] or 0, "pending": r[2] or 0, "carried": r[3] or 0} for r in rows]
    return jsonify({"velocity": data})


# ══════════════════════════════════════════════════════════════════════════════
# PERSONAL DAILY TASK TRACKER (separate from project tasks)
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/standup/actions", methods=["POST"])
def execute_standup_actions():
    data = request.json or {}
    actions = data.get("actions", [])
    user_id = data.get("user_id", "")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    conn = _su_conn()
    cur = conn.cursor()
    results = []

    for action_item in actions:
        action = action_item.get("action")
        try:
            if action == "update_task":
                task_id = action_item.get("task_id")
                new_status = action_item.get("status")
                if task_id and new_status:
                    cur.execute("SELECT notion_id, title FROM standup_tasks WHERE id=?", (task_id,))
                    row = cur.fetchone()
                    if row:
                        cur.execute("UPDATE standup_tasks SET status=? WHERE id=?", (new_status, task_id))
                        notion_id = row[0]
                        if notion_id and notion_store.is_configured():
                            n_status = "Done" if new_status == "done" else "In progress" if new_status == "pending" else new_status
                            notion_store.update_task(notion_id, status=n_status)
                        results.append({"task_id": task_id, "status": new_status, "updated": True})
            elif action == "add_task":
                title = action_item.get("title")
                due_date = action_item.get("due_date") or datetime.utcnow().strftime("%Y-%m-%d")
                status = action_item.get("status", "pending")
                if title:
                    cur.execute("INSERT INTO standup_tasks (user_id, date, due_date, title, status) VALUES (?, ?, ?, ?, ?)",
                                (user_id, datetime.utcnow().strftime("%Y-%m-%d"), due_date, title, status))
                    new_id = cur.lastrowid
                    results.append({"task_id": new_id, "title": title, "added": True})
        except Exception as e:
            logger.error(f"Error executing standup action {action_item}: {e}")

    conn.commit()
    conn.close()
    return jsonify({"success": True, "results": results})



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
        cur.execute("SELECT MAX(date) FROM standup_tasks WHERE user_id=? AND date<?", (user_id, date_str))
        last_date_row = cur.fetchone()
        if last_date_row and last_date_row[0]:
            last_date = last_date_row[0]
            cur.execute(
                "SELECT title, blocker, carried_from, notion_id, due_date, subtasks, delegated_to, delegated_from FROM standup_tasks WHERE user_id=? AND date=? AND status='pending'",
                (user_id, last_date),
            )
            pending = cur.fetchall()
            if pending:
                with conn:
                    for title, blocker, carried_from, nid, dd, sub, d_to, d_from in pending:
                        orig_carry_from = carried_from if carried_from else last_date
                        conn.execute(
                            "INSERT INTO standup_tasks (user_id, date, title, status, carried_from, blocker, notion_id, due_date, subtasks, delegated_to, delegated_from) VALUES (?,?,?,'pending',?,?,?,?,?,?,?)",
                            (user_id, date_str, title, orig_carry_from, blocker, nid, dd, sub, d_to, d_from),
                        )

    import json
    import re

    def parse_date_for_sort(d):
        if not d:
            return "9999-12-31"
        # If DD-MM-YYYY, convert to YYYY-MM-DD
        if re.match(r"^\d{2}-\d{2}-\d{4}$", d.strip()):
            return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
        # If DD/MM/YYYY, convert to YYYY-MM-DD
        if re.match(r"^\d{2}/\d{2}/\d{4}$", d.strip()):
            return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
        return d.strip()

    cur.execute(
        "SELECT id, title, status, carried_from, created_at, blocker, notion_id, subtasks, delegated_to, delegated_from, due_date FROM standup_tasks WHERE user_id=? AND date=?",
        (user_id, date_str),
    )
    rows = cur.fetchall()
    conn.close()
    
    tasks = []
    for r in rows:
        if r[2] in ("deleted", "delegated"): continue
        st = []
        try:
            st = json.loads(r[7]) if r[7] else []
        except: pass
        
        tasks.append({
            "id": r[0], "title": r[1], "status": r[2],
            "carried_from": r[3], "created_at": r[4], 
            "blocker": r[5], "notion_id": r[6], "subtasks": st,
            "delegated_to": r[8], "delegated_from": r[9], "due_date": r[10]
        })

    tasks.sort(key=lambda x: (parse_date_for_sort(x["due_date"]), x["id"]))

    return jsonify({"tasks": tasks, "date": date_str})


@ops_bp.route("/api/standup/tasks/<int:task_id>/delegate", methods=["POST"])
def delegate_task(task_id):
    body = request.get_json(silent=True) or {}
    target_user_id = body.get("target_user_id")
    target_user_name = body.get("target_user_name")
    
    if not target_user_id or not target_user_name:
        return jsonify({"error": "target_user_id and target_user_name required"}), 400
        
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, date, title, blocker FROM standup_tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Task not found"}), 404
        
    orig_user_id, old_date_str, title, blocker = row
    
    EMP_NAMES = {"emp001":"Vidit","emp002":"Nupur","emp003":"Abhinav",
                 "emp004":"Kshitij","emp006":"Mohit",
                 "emp007":"Palak","emp008":"Happy"}
    orig_user_name = EMP_NAMES.get(orig_user_id, orig_user_id)
    
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    
    with conn:
        # 1. Mark original task as delegated
        conn.execute("UPDATE standup_tasks SET status='delegated', delegated_to=? WHERE id=?", (target_user_name, task_id))
        
        # 2. Create new task for target user on TODAY's date (so it appears immediately)
        conn.execute(
            "INSERT INTO standup_tasks (user_id, date, title, status, blocker, delegated_from) VALUES (?, ?, ?, 'pending', ?, ?)",
            (target_user_id, today_str, title, blocker, orig_user_name)
        )
    conn.close()
    return jsonify({"success": True})


def _normalize_title(t: str) -> str:
    if not t: return ""
    t = t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = re.sub(r'^\[.*?\]\s*', '', t)
    return t.lower().strip()


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
    Also includes social media tasks whose Creation Date is today
    (i.e. work starts today — the assignee sees it for the first time).
    Body: { user_id, assigned_name }
    """
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "").strip()
    assigned_name = body.get("assigned_name", "").strip()
    sync_all = body.get("sync_all", False)
    
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    try:
        import notion_store
        if not notion_store.is_configured():
            return jsonify({"error": "Notion is not configured"}), 400
            
        if sync_all:
            all_tasks = notion_store.list_tasks()
        else:
            if not assigned_name:
                return jsonify({"error": "assigned_name required"}), 400
            all_tasks = notion_store.list_tasks(assigned_to=assigned_name)
    except Exception as e:
        logger.error(f"Failed to fetch Notion tasks for auto-fill: {e}")
        return jsonify({"error": str(e)}), 500

    # Load employee name-to-id mapping for sync_all
    emp_name_to_id = {}
    try:
        _emp_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "employees.json")
        with open(_emp_path, "r", encoding="utf-8") as _f:
            _emp_data = json.load(_f)
        emp_name_to_id = {e["name"]: e["id"] for e in _emp_data.get("employees", [])}
    except Exception:
        emp_name_to_id = {"Vidit":"emp001","Nupur":"emp002","Abhinav":"emp003",
                          "Kshitij":"emp004","Mohit":"emp006",
                          "Palak":"emp007","Happy":"emp008"}

    today = datetime.utcnow()
    today_str = today.strftime("%Y-%m-%d")
    
    # Pre-fetch existing notion_ids for today to ensure we update them even if they are future tasks
    existing_notion_ids = set()
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("SELECT notion_id FROM standup_tasks WHERE date=? OR status NOT IN ('Completed', 'Archived')", (today_str,))
    for r in cur.fetchall():
        if r[0]: existing_notion_ids.add(r[0])
    
    valid_tasks = []
    for t in all_tasks:
        s = t.get("status", "").lower().replace(" ", "_").replace("-", "_")
        d = t.get("due_date", "")
        raw_title = t.get("title", "").strip()
        nid = t.get("id")

        client = t.get("client_name", "").strip()
        content = t.get("content", "").strip() or t.get("description", "").strip() or t.get("brief", "").strip()
        
        search_title = raw_title
        if client and not search_title.startswith(client):
            search_title = f"{client} — {search_title}"
        if content:
            preview = content[:40] + "..." if len(content) > 40 else content
            search_title = f"{search_title} ({preview})"

        # AUTO-REPAIR: If a task already exists locally but lost its due_date/notion_id 
        # (e.g. from a past carry-over bug), restore it now regardless of status.
        if search_title:
            # First figure out target_user_id for this task
            target_uids = []
            assignees = t.get("assigned_to", "")
            if not sync_all and assigned_name:
                target_uids = [user_id]
            else:
                names = [n.strip() for n in assignees.split(",") if n.strip()]
                target_uids = [emp_name_to_id.get(n) for n in names if emp_name_to_id.get(n)]
                if not target_uids:
                    target_uids = list(emp_name_to_id.values())
                
            for target_user_id in target_uids:
                cur.execute("SELECT id, due_date, notion_id, title FROM standup_tasks WHERE user_id=? AND (date=? OR status NOT IN ('Completed', 'Archived'))", (target_user_id, today_str))
                local_tasks = cur.fetchall()
                
                matched_id = None
                norm_search = _normalize_title(search_title)
                norm_raw = _normalize_title(raw_title)
                
                for lid, ldue, lnid, ltitle in local_tasks:
                    if lnid and nid and lnid == nid:
                        matched_id = lid
                        break
                    norm_ltitle = _normalize_title(ltitle)
                    if norm_ltitle and (norm_ltitle == norm_search or norm_ltitle == norm_raw or 
                                        norm_search.endswith(norm_ltitle) or norm_ltitle.endswith(norm_raw)):
                        matched_id = lid
                        break
                
                if matched_id:
                    # Update local task with accurate Notion data
                    cur.execute("UPDATE standup_tasks SET title=?, due_date=?, notion_id=? WHERE id=?", (search_title, d, nid, matched_id))

        # Don't pull finished tasks
        if s in ("approved", "done", "submitted", "in_review", "pending_review"):
            continue
            
        # Add if status is active
        is_active = (s == "in_progress")
        
        # Add if due date is today EXACTLY. Overdue tasks should not be indiscriminately added 
        # unless they are carried over via the app's wrap-up feature.
        is_due = False
        if d:
            try:
                due_dt = d.split("T")[0]
                if due_dt == today_str:
                    is_due = True
            except: pass
            
        # Check if this is a task with a specific Creation Date
        is_creation_today = False
        has_creation_date = False
        desc = t.get("description", "") or t.get("notes", "") or ""
        if desc:
            import re as _re
            cr_match = _re.search(r'Creation Date:\s*([\d-]+)', desc)
            if cr_match:
                has_creation_date = True
                try:
                    cr_date = cr_match.group(1).strip()
                    if cr_date == today_str and s == "not_started":
                        is_creation_today = True
                except: pass

        # If the user has "not started" tasks, pull them in if they are due within 7 days
        # BUT skip this for tasks that have a specific Creation Date (they should only appear on their exact Creation Date)
        is_upcoming = False
        if d and s == "not_started" and not has_creation_date:
            try:
                due_dt = datetime.strptime(d.split("T")[0], "%Y-%m-%d")
                if (due_dt - today).days <= 7:
                    is_upcoming = True
            except: pass

        # Also, if body explicitly requested "upcoming", we can pull all not_started tasks
        pull_all_upcoming = body.get("pull_upcoming", False)
        
        if is_due or is_upcoming or is_creation_today or (pull_all_upcoming and s == "not_started") or (t.get("notion_id") in existing_notion_ids):
            valid_tasks.append(t)

    conn.commit()
    conn.close()

    if not valid_tasks:
        return jsonify({"success": True, "added": 0})

    conn = _su_conn()
    added_count = 0
    with conn:
        cur = conn.cursor()
        for vt in valid_tasks:
            nid = vt.get("notion_id")
            raw_title = vt.get("title", "Untitled").strip()
            d = vt.get("due_date", "")
            
            # Add context for tasks (especially from social sheet)
            client = vt.get("client_name", "").strip()
            content = vt.get("content", "").strip() or vt.get("description", "").strip() or vt.get("brief", "").strip()
            
            search_title = raw_title
            if client and not search_title.startswith(client):
                search_title = f"{client} — {search_title}"
            if content:
                # Append a short preview of the content
                preview = content[:40] + "..." if len(content) > 40 else content
                search_title = f"{search_title} ({preview})"
                
            # If sync_all is true, we update tasks for ALL assignees
            if sync_all:
                emp_name_to_id = {
                    "Vidit":"emp001","Nupur":"emp002","Abhinav":"emp003",
                    "Kshitij":"emp004","Mohit":"emp006",
                    "Palak":"emp007","Happy":"emp008", "Prathmesh":"emp009", "Om":"emp010"
                }
                assignees = vt.get("assigned_to", "")
                names = [n.strip() for n in assignees.split(",") if n.strip()]
                target_uids = [emp_name_to_id.get(n) for n in names if emp_name_to_id.get(n)]
                if target_uids:
                    insert_allowed = True
                else:
                    target_uids = list(emp_name_to_id.values())
                    insert_allowed = False
            else:
                target_uids = [user_id]
                insert_allowed = True
                
            for target_user_id in target_uids:
                cur.execute("SELECT id, notion_id, title FROM standup_tasks WHERE user_id=? AND (date=? OR status NOT IN ('Completed', 'Archived'))", (target_user_id, today_str))
                local_tasks = cur.fetchall()
                
                matched_id = None
                norm_search = _normalize_title(search_title)
                norm_raw = _normalize_title(raw_title)
                
                for lid, lnid, ltitle in local_tasks:
                    if lnid and nid and lnid == nid:
                        matched_id = lid
                        break
                    norm_ltitle = _normalize_title(ltitle)
                    if norm_ltitle and (norm_ltitle == norm_search or norm_ltitle == norm_raw or 
                                        norm_search.endswith(norm_ltitle) or norm_ltitle.endswith(norm_raw)):
                        matched_id = lid
                        break
                                
                if not matched_id:
                    if insert_allowed:
                        cur.execute(
                            "INSERT INTO standup_tasks (user_id, date, title, notion_id, due_date) VALUES (?, ?, ?, ?, ?)",
                            (target_user_id, today_str, search_title, nid, d)
                        )
                        added_count += 1
                else:
                    cur.execute(
                        "UPDATE standup_tasks SET title=?, due_date=?, notion_id=? WHERE id=?",
                        (search_title, d, nid, matched_id)
                    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "added": added_count})


@ops_bp.route("/api/debug/tasks", methods=["GET"])
def debug_tasks():
    user_id = request.args.get("user_id", "emp008")
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, title, notion_id, date, status FROM standup_tasks WHERE user_id=? AND date=? ORDER BY id DESC", (user_id, date_str))
    db_tasks = [{"id": r[0], "title": r[1], "notion_id": r[2], "date": r[3], "status": r[4]} for r in cur.fetchall()]
    conn.close()
    
    import notion_store
    notion_tasks = notion_store.list_tasks(assigned_to="Happy")
    
    processed_notion = []
    for vt in notion_tasks:
        title = vt.get("title", "Untitled").strip()
        client = vt.get("client_name", "").strip()
        content = vt.get("content", "").strip() or vt.get("description", "").strip() or vt.get("brief", "").strip()
        
        if client and not title.startswith(client):
            title = f"{client} — {title}"
        if content:
            preview = content[:40] + "..." if len(content) > 40 else content
            title = f"{title} ({preview})"
            
        processed_notion.append({
            "notion_id": vt.get("notion_id"),
            "original_title": vt.get("title"),
            "computed_title": title,
            "client_name": client,
            "content": content
        })
        
    return jsonify({
        "db_tasks": db_tasks,
        "notion_tasks": processed_notion
    })

@ops_bp.route("/api/debug/cleanup-today", methods=["GET"])
def cleanup_today():
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    conn = _su_conn()
    cur = conn.cursor()
    # Delete all auto-synced tasks for today (that were not carried over from yesterday)
    cur.execute("DELETE FROM standup_tasks WHERE date=? AND carried_from IS NULL AND notion_id IS NOT NULL", (today_str,))
    deleted_count = cur.rowcount
    conn.commit()
    conn.close()
    return f"Successfully cleaned up {deleted_count} mistakenly synced tasks for today! You can close this tab and click 'Sync All Tasks' again."


@ops_bp.route("/api/standup/smart-add", methods=["POST"])
def standup_smart_add():
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    assigned_to = body.get("assigned_to", "")
    title = body.get("title", "").strip()
    due_date = body.get("due_date", "").strip()

    if not user_id or not title:
        return jsonify({"error": "user_id and title required"}), 400

    system_prompt = """You are an AI task router.
The user just typed a new task into their daily standup list.
Is this a "Project Task" that should be tracked in a main project board (e.g. creating a feature, designing a page, writing a proposal), or is it a "Quick Chore" (e.g. check email, call client, meeting, lunch)?
If it's a Project Task, guess the Client Name from the title if possible (otherwise use "Internal").
Respond ONLY in valid JSON format:
{
  "is_project_task": true/false,
  "client_name": "Name or Internal"
}"""
    
    try:
        resp = _claude_call(system_prompt, title, 200)
        import re
        match = re.search(r'\{.*\}', resp, re.DOTALL)
        resp_json = json.loads(match.group(0)) if match else json.loads(resp)
            
        is_project = resp_json.get("is_project_task", False)
        client = resp_json.get("client_name", "Internal")
    except Exception as e:
        logger.error(f"Auto-Router failed: {e}")
        is_project = False
        client = "Internal"

    notion_id = None
    
    if is_project and notion_store.is_configured():
        if not due_date:
            due_date = datetime.utcnow().strftime("%Y-%m-%d")
        created = notion_store.create_task(
            title=title,
            client_name=client,
            client_notion_id="",
            assigned_to=assigned_to,
            due_date=due_date,
            status="in_progress"
        )
        if created and "id" in created:
            notion_id = created["id"]
            
    conn = _su_conn()
    with conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO standup_tasks (user_id, title, status, date, notion_id, due_date) VALUES (?, ?, 'pending', date('now'), ?, ?)",
            (user_id, title, notion_id, due_date)
        )
        task_id = cur.lastrowid
        
    return jsonify({
        "success": True, 
        "task_id": task_id, 
        "title": title, 
        "notion_id": notion_id,
        "is_project": is_project
    })


@ops_bp.route("/api/standup/push-to-notion/<int:task_id>", methods=["POST"])
def push_to_notion(task_id):
    body = request.get_json(silent=True) or {}
    assigned_to = body.get("assigned_to", "")
    
    conn = _su_conn()
    with conn:
        cur = conn.cursor()
        cur.execute("SELECT title, notion_id FROM standup_tasks WHERE id=?", (task_id,))
        row = cur.fetchone()
        
    if not row:
        return jsonify({"error": "Task not found"}), 404
        
    title = row[0]
    if row[1]:
        return jsonify({"error": "Task already in Notion"}), 400
        
    system_prompt = """Extract client name from this task title. Return ONLY json: {"client_name": "Client Name or Internal"}"""
    try:
        resp = _claude_call(system_prompt, title, 100)
        import re
        match = re.search(r'\{.*\}', resp, re.DOTALL)
        client = json.loads(match.group(0)).get("client_name", "Internal") if match else "Internal"
    except:
        client = "Internal"
        
    due_date = datetime.utcnow().strftime("%Y-%m-%d")
    created = notion_store.create_task(
        title=title,
        client_name=client,
        client_notion_id="",
        assigned_to=assigned_to,
        due_date=due_date,
        status="in_progress"
    )
    if created and "id" in created:
        notion_id = created["id"]
        with conn:
            conn.execute("UPDATE standup_tasks SET notion_id=?, due_date=? WHERE id=?", (notion_id, due_date, task_id))
        return jsonify({"success": True, "notion_id": notion_id})
    return jsonify({"error": "Failed to push to Notion"}), 500


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
                if notion_progress == 100: notion_status = "Done"
                elif notion_progress > 0: notion_status = "In Progress"
            
            # Auto-calculate progress from subtasks
            elif subtasks is not None:
                import json
                st = json.loads(current_subtasks_json) if current_subtasks_json else []
                if st:
                    done_count = sum(1 for s in st if s.get("done"))
                    notion_progress = int((done_count / len(st)) * 100)
                    if notion_progress == 100: notion_status = "Done"
                    elif notion_progress > 0: notion_status = "In Progress"
            
            if notion_progress is not None:
                if notion_status == "Done":
                    task_type = notion_store.get_task_type(notion_id)
                    if task_type and task_type.lower() == "social media":
                        notion_status = "need_for_approval"
                        
                        # Also update local db to reflect this
                        conn = _su_conn()
                        conn.execute("UPDATE standup_tasks SET status='need_for_approval' WHERE id=?", (task_id,))
                        conn.commit()
                        conn.close()

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
        "SELECT title, blocker, carried_from, notion_id, due_date, subtasks, delegated_to, delegated_from FROM standup_tasks WHERE user_id=? AND date=? AND status='pending'",
        (user_id, today),
    )
    pending = cur.fetchall()

    carried = 0
    if pending:
        with conn:
            for title, blocker, carried_from, nid, dd, sub, d_to, d_from in pending:
                orig_carry_from = carried_from if carried_from else today
                # Avoid duplicating if already carried (idempotent)
                cur2 = conn.cursor()
                cur2.execute(
                    "SELECT id FROM standup_tasks WHERE user_id=? AND date=? AND title=? AND carried_from=?",
                    (user_id, tomorrow, title, orig_carry_from),
                )
                if not cur2.fetchone():
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, carried_from, blocker, notion_id, due_date, subtasks, delegated_to, delegated_from) VALUES (?,?,?,'pending',?,?,?,?,?,?,?)",
                        (user_id, tomorrow, title, orig_carry_from, blocker, nid, dd, sub, d_to, d_from),
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
    """Return AT_RISK and CRITICAL tasks for founderDashboard."""
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
    from utils import _is_admin
    token = request.cookies.get("session_token", "")
    user_id = _verify_session(token)
    if not user_id or not _is_admin(user_id):
        return jsonify({"error": "Unauthorized: Admin access required"}), 403

    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not notion_store.is_configured():
        return jsonify({"error": "Notion is not configured. Add NOTION_TOKEN and DB IDs to config/.env"}), 503

    c_user = body.get("client_username", "").strip()
    c_pass = body.get("client_password", "").strip()
    if c_user:
        try:
            # Admin is forcing creation: wipe any stuck or old credential with this username
            # so the new client can claim it seamlessly.
            conn = _pt_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM client_users WHERE username=?", (c_user,))
            conn.commit()
            conn.close()
        except Exception as e:
            pass

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

    c_user = body.get("client_username", "").strip()
    c_pass = body.get("client_password", "").strip()
    if c_user and c_pass:
        try:
            hashed_pass = generate_password_hash(c_pass)
            conn = _pt_conn()
            with conn:
                conn.execute(
                    "INSERT INTO client_users (username, password, client_name, client_notion_id) VALUES (?,?,?,?)",
                    (c_user, hashed_pass, name, client["notion_id"])
                )
            conn.close()
        except Exception as e:
            logger.error(f"Failed to create client user: {e}")

    EMP_NAMES = {
        "emp001": "Vidit", "emp002": "Nupur", "emp003": "Abhinav",
        "emp004": "Kshitij", "emp006": "Mohit",
        "emp007": "Tanaya", "emp008": "Happy",
    }
    SVC_TASKS = {
        "content":  [("Content Brief & Research", "emp006"), ("Write Copy / Content Draft", "emp006"), ("Content Review & Approval", "emp004")],
        "video":    [("Video Script Writing", "emp006"), ("Video Shoot / Production", "emp005"), ("Video Editing & Post", "emp005"), ("AI Video Enhancements", "emp008")],
        "design":   [("Design Brief & Moodboard", "emp002"), ("UI/UX Design — Wireframes", "emp002"), ("Final Design Handoff", "emp002")],
        "website":  [("Website Architecture Plan", "emp001"), ("Frontend Development", "emp003"), ("Backend / Integrations", "emp001"), ("QA & Launch", "emp003")],
        "accounts": [("Invoice & Payment Setup", "emp007"), ("Monthly Reporting", "emp007")],
        # "social" intentionally omitted — social posts are added via the Add Tasks calendar
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


@ops_bp.route("/api/notion/tasks", methods=["GET", "POST"])
def notion_list_or_create_tasks():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        title = body.get("title", "").strip()
        assigned_to = body.get("assigned_to", "")
        due_date = body.get("due_date", "")
        client_name = body.get("client_name", "Internal")
        client_notion_id = body.get("client_id", "")
        
        if not title:
            return jsonify({"error": "Title is required"}), 400
            
        result = notion_store.create_task(
            title=title, 
            client_name=client_name,
            client_notion_id=client_notion_id, 
            assigned_to=assigned_to,
            due_date=due_date,
            status="not_started"
        )
        if result:
            return jsonify({"success": True, "task": result})
        return jsonify({"error": "Failed to create task in Notion"}), 500

    # GET request
    tasks = notion_store.list_tasks(
        assigned_to=request.args.get("assigned_to", ""),
        client_notion_id=request.args.get("client_id", ""),
        status_filter=request.args.get("status", ""),
    )
    return jsonify({"tasks": tasks, "count": len(tasks)})



@ops_bp.route("/api/notion/tasks/<string:notion_id>", methods=["PATCH"])
def notion_update_task(notion_id: str):
    body   = request.get_json(silent=True) or {}
    
    EMP_NAMES = {"emp001":"Vidit","emp002":"Nupur","emp003":"Abhinav",
                 "emp004":"Kshitij","emp006":"Mohit",
                 "emp007":"Palak","emp008":"Happy"}
                 
    raw_assigned = body.get("assigned_to", "")
    mapped_assigned = EMP_NAMES.get(raw_assigned, raw_assigned)
    
    result = notion_store.update_task(
        notion_id=notion_id, status=body.get("status"),
        progress=body.get("progress"), submission_note=body.get("submission_note"),
        assigned_to=mapped_assigned, new_title=body.get("new_title"),
        due_date=body.get("due_date"), task_title=body.get("task_title", ""),
        assignee=body.get("assignee", ""), client_name=body.get("client_name", ""),
    )
    
    # ── Auto-sync assignment to standup_tasks ─────────────────────────────────
    if result:
        task_title = body.get("new_title") or body.get("task_title") or "Untitled Task"
        new_due = body.get("due_date")
        
        su_conn = _su_conn()
        
        # Sync title & due_date to any existing standup tasks tied to this notion_id
        with su_conn:
            su_conn.execute(
                "UPDATE standup_tasks SET title=COALESCE(?, title), due_date=COALESCE(?, due_date) WHERE notion_id=?",
                (task_title if task_title != "Untitled Task" else None, new_due, notion_id)
            )
            
        if raw_assigned:
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            cur2 = su_conn.cursor()
            cur2.execute(
                "SELECT id FROM standup_tasks WHERE user_id=? AND date=? AND notion_id=?",
                (raw_assigned, today_str, notion_id)
            )
            if not cur2.fetchone():
                with su_conn:
                    su_conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, notion_id, due_date) VALUES (?,?,?,'pending',?,?)",
                        (raw_assigned, today_str, task_title, notion_id, new_due)
                    )
        su_conn.close()
    # ─────────────────────────────────────────────────────────────────────────

    # The WhatsApp notification is handled internally by notion_store.update_task

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
    user_id = request.args.get("user_id", "")
    
    try:
        from utils import _is_admin
        if _is_admin(user_id):
            conn = _pt_conn()
            cur = conn.cursor()
            cur.execute("SELECT client_notion_id, username FROM client_users WHERE client_notion_id IS NOT NULL")
            users = {r[0]: {"username": r[1]} for r in cur.fetchall()}
            conn.close()
            
            if "clients" in data:
                for c in data["clients"]:
                    nid = c.get("notion_id") or c.get("id")
                    if nid in users:
                        c["client_username"] = users[nid]["username"]
        
        # Attach task feedback
        conn = _pt_conn()
        cur = conn.cursor()
        cur.execute("SELECT task_id, status, comments, audio_url, updated_at FROM client_task_feedback")
        feedback_map = {r[0]: {"status": r[1], "comments": r[2], "audio_url": r[3], "updated_at": r[4]} for r in cur.fetchall()}
        conn.close()

        # Attach standup subtasks & filter out tasks done in standup
        su_conn = _su_conn()
        su_cur = su_conn.cursor()
        su_cur.execute("SELECT notion_id, status, subtasks FROM standup_tasks WHERE notion_id IS NOT NULL ORDER BY date ASC, id ASC")
        standup_map = {r[0]: {"status": r[1], "subtasks": r[2]} for r in su_cur.fetchall()}
        su_conn.close()
        
        if "clients" in data:
            for c in data["clients"]:
                filtered_tasks = []
                for t in c.get("tasks", []):
                    t_id = t.get("notion_id") or t.get("id")
                    # Filter out if marked done in standup, and filter out random unassigned tasks
                    if t_id and t_id in standup_map:
                        if standup_map[t_id]["status"] == "done":
                            continue  # Skip adding this task to the list
                            
                        # Attach subtasks
                        su_subtasks = standup_map[t_id]["subtasks"]
                        if su_subtasks and su_subtasks != '[]':
                            try:
                                import json
                                t["subtasks"] = json.loads(su_subtasks)
                            except:
                                pass
                    else:
                        # If this is the Daily Standup Tasks block ("unassigned"), and it's NOT in standup_map, drop it!
                        if c.get("notion_id") == "unassigned":
                            continue
                            
                    if t_id and t_id in feedback_map:
                        t["feedback"] = feedback_map[t_id]
                        
                    filtered_tasks.append(t)
                c["tasks"] = filtered_tasks
                        
    except Exception as e:
        logger.error(f"Error attaching dashboard metadata: {e}")
        
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
                       ("due_date","due_date"),("status","status"),("progress","progress"),
                       ("submission_note","description")]:
        if field in body:
            updates.append(f"{col}=?"); vals.append(body[field])
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    vals.append(task_id)
    # ── Fetch old state for WhatsApp notifications ────────────────────────
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT title, assigned_to, status, client_id FROM tasks WHERE id=?", 
        (task_id,)
    )
    old_row = cur.fetchone()
    old_status = old_row[2] if old_row else None
    
    with conn:
        conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", vals)
        
    # ── Trigger WhatsApp Notification if status changed ─────────────────────
    new_status = body.get("status")
    if old_row and new_status and new_status != old_status:
        try:
            from notifications import notify_task_status_changed
            EMP_NAMES = {"emp001":"Vidit","emp002":"Nupur","emp003":"Abhinav",
                         "emp004":"Kshitij","emp006":"Mohit",
                         "emp007":"Palak","emp008":"Happy"}
            
            task_title = body.get("new_title", old_row[0])
            raw_assignee = body.get("assigned_to", old_row[1])
            assignee_name = EMP_NAMES.get(raw_assignee, raw_assignee)
            client_id = old_row[3]
            client_name = "Internal"
            
            if client_id:
                cur.execute("SELECT name FROM clients WHERE id=?", (client_id,))
                c_row = cur.fetchone()
                if c_row:
                    client_name = c_row[0]
                    
            notify_task_status_changed(
                task_title=task_title, 
                assignee=assignee_name, 
                client_name=client_name, 
                old_status=old_status, 
                new_status=new_status
            )
        except Exception as e:
            logger.error(f"Failed to send WhatsApp notification: {e}")
    # ─────────────────────────────────────────────────────────────────────────
    # ── Auto-sync assignment to standup_tasks ─────────────────────────────────
    new_assignee = body.get("assigned_to", "").strip()
    task_title = body.get("new_title") or (old_row[0] if old_row else f"Task #{task_id}")
    new_due = body.get("due_date")
    
    su_conn = _su_conn()
    
    # Sync title and due_date for existing standup tasks matching the old title
    if old_row and old_row[0]:
        with su_conn:
            su_conn.execute(
                "UPDATE standup_tasks SET title=COALESCE(?, title), due_date=COALESCE(?, due_date) WHERE title=?",
                (task_title if task_title != old_row[0] else None, new_due, old_row[0])
            )
            
    if new_assignee:
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        cur2 = su_conn.cursor()
        # Only insert if not already in today's standup for this user
        cur2.execute(
            "SELECT id FROM standup_tasks WHERE user_id=? AND date=? AND title=?",
            (new_assignee, today_str, task_title)
        )
        if not cur2.fetchone():
            with su_conn:
                su_conn.execute(
                    "INSERT INTO standup_tasks (user_id, date, title, status, due_date) VALUES (?,?,?,'pending',?)",
                    (new_assignee, today_str, task_title, new_due)
                )
    su_conn.close()
    # ─────────────────────────────────────────────────────────────────────────

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
    """Convert AI-generated markdown to a downloadableDOCX,PDF, orPPTX file."""
    from document_exporter import export_docx, export_pdf, export_pptx

    body    = request.get_json(silent=True) or {}
    content = body.get("content", "").strip()
    fmt     = body.get("format", "pdf").lower()
    title   = body.get("title", "System Export")[:120]

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


# ══════════════════════════════════════════════════════════════════════════════
# CLAUDE AI ROUTES — Task Intelligence
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/ai/breakdown", methods=["POST"])
def ai_task_breakdown():
    """
    Feature 1: Ask System to auto-generate sub-tasks for a given task title.
    Body: { task_title, client_name (optional) }
    Returns: { subtasks: ["step 1", "step 2", ...] }
    """
    body = request.get_json(silent=True) or {}
    task_title = body.get("task_title", "").strip()
    client_name = body.get("client_name", "").strip()

    if not task_title:
        return jsonify({"error": "task_title is required"}), 400

    system = (
        "You are a senior project manager helping an agency team member break down tasks into actionable sub-steps. "
        "Return ONLY a JSON array of strings. Each string is one clear, concise sub-task (max 10 words). "
        "No explanations, no markdown, no numbering — just a raw JSON array like: "
        '[\"Design wireframe\", \"Write copy\", \"Code component\"]'
    )
    client_ctx = f" for client '{client_name}'" if client_name else ""
    user = f"Break down this task into 4-7 practical sub-tasks: '{task_title}'{client_ctx}"

    try:
        raw = _claude_call(system, user, max_tokens=512)
        import re
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            raw = match.group(0)
        # Parse raw JSON array
        subtasks = json.loads(raw)
        if not isinstance(subtasks, list):
            raise ValueError("Not a list")
        return jsonify({"subtasks": subtasks})
    except (json.JSONDecodeError, ValueError):
        # Fallback: extract lines from raw text
        lines = [l.strip().lstrip("•-123456789. \",") for l in raw.split("\n") if l.strip() and l.strip() not in ("```json", "```", "[", "]")]
        return jsonify({"subtasks": lines[:8]})
    except Exception as e:
        logger.exception("ai_task_breakdown failed")
        return jsonify({"error": str(e)}), 500


@ops_bp.route("/api/ai/proof-of-work", methods=["POST"])
def ai_proof_of_work():
    """
    Feature 2: Auto-draft a professional submission note.
    Body: { task_title, client_name, subtasks: [{ text, done }] }
    Returns: { draft: "..." }
    """
    body = request.get_json(silent=True) or {}
    task_title = body.get("task_title", "").strip()
    client_name = body.get("client_name", "").strip()
    subtasks = body.get("subtasks", [])

    if not task_title:
        return jsonify({"error": "task_title is required"}), 400

    done_list = [st["text"] for st in subtasks if st.get("done")]
    pending_list = [st["text"] for st in subtasks if not st.get("done")]

    completed_str = "\n".join(f"- {t}" for t in done_list) if done_list else "- All work completed"
    pending_str = "\n".join(f"- {t}" for t in pending_list) if pending_list else "None"

    system = (
        "You are a professional agency employee writing a polished, concise progress update for your manager. "
        "Write 2-4 sentences in first person. Be specific, confident and professional. "
        "Do NOT use placeholders or generic filler. Return only the note text, no preamble."
    )
    user = (
        f"Task: '{task_title}'" + (f" (Client: {client_name})" if client_name else "") + "\n"
        f"Completed steps:\n{completed_str}\n"
        f"Remaining:\n{pending_str}\n\n"
        "Write a professional proof-of-work submission note."
    )

    try:
        draft = _claude_call(system, user, max_tokens=300)
        return jsonify({"draft": draft})
    except Exception as e:
        logger.exception("ai_proof_of_work failed")
        return jsonify({"error": str(e)}), 500


@ops_bp.route("/api/ai/parse-task", methods=["POST"])
def ai_parse_task():
    """
    Feature 3: Natural language task parsing — extract title, client, due_date from free text.
    Body: { text, assigned_name }
    Returns: { title, client_name, due_date (YYYY-MM-DD or null) }
    """
    body = request.get_json(silent=True) or {}
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400

    today = datetime.utcnow().strftime("%Y-%m-%d")
    tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    next_week = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d")

    system = (
        "You are a task parser. Extract structured data from the user's natural language task description. "
        f"Today's date is {today}. Tomorrow is {tomorrow}. Next week is {next_week}. "
        "Return ONLY a JSON object with exactly these keys: title (string), client_name (string or null), due_date (YYYY-MM-DD string or null). "
        'Example: {"title": "Fix responsive header bug", "client_name": "MetaZune", "due_date": "2026-05-20"}'
    )
    user = f"Parse this task: '{text}'"

    try:
        raw = _claude_call(system, user, max_tokens=200)
        parsed = json.loads(raw)
        return jsonify({
            "title": parsed.get("title", text),
            "client_name": parsed.get("client_name") or "",
            "due_date": parsed.get("due_date") or today,
        })
    except Exception as e:
        logger.exception("ai_parse_task failed")
        return jsonify({"title": text, "client_name": "", "due_date": today})


@ops_bp.route("/api/ai/coach", methods=["POST"])
def ai_coach():
    """
    Feature 4: Daily AI Coach — answer questions about the user's Notion task list.
    Body: { question, assigned_name, user_id }
    Returns: { reply: "..." }
    """
    body = request.get_json(silent=True) or {}
    question = body.get("question", "").strip()
    assigned_name = body.get("assigned_name", "")

    if not question:
        return jsonify({"error": "question is required"}), 400

    # Fetch the user's tasks from Notion
    tasks = notion_store.list_tasks(assigned_to=assigned_name) if assigned_name else []

    today = datetime.utcnow().strftime("%Y-%m-%d")
    task_summary = ""
    if tasks:
        lines = []
        for t in tasks:
            status = t.get("status", "not_started")
            due = t.get("due_date", "no date")
            title = t.get("title", "Untitled")
            client = t.get("client_name", "")
            overdue = " [OVERDUE]" if due and due < today and status not in ("approved", "submitted") else ""
            lines.append(f"- {title} | Status: {status} | Due: {due}{overdue}" + (f" | Client: {client}" if client else ""))
        task_summary = "\n".join(lines)
    else:
        task_summary = "No tasks found."

    system = (
        f"You are a friendly, concise AI productivity coach for {assigned_name or 'the user'} at a creative agency. "
        f"Today is {today}. Here is their current Notion task list:\n\n{task_summary}\n\n"
        "Based on this, answer their question helpfully and specifically. "
        "Be direct and action-oriented. Max 3-4 sentences. "
        "If they ask what to do next, suggest the most urgent/important task. "
        "If they're overwhelmed, offer practical relief (defer, focus on one thing). "
        "Never invent tasks not in the list."
    )

    try:
        reply = _claude_call(system, question, max_tokens=400)
        return jsonify({"reply": reply})
    except Exception as e:
        logger.exception("ai_coach failed")
        return jsonify({"error": str(e)}), 500


# ── Feature 1: Meeting Notes → Notion Tasks ──────────────────────────────────
@ops_bp.route("/api/ai/meeting-to-tasks", methods=["POST"])
def meeting_to_tasks():
    """
    Parse raw meeting notes into structured tasks and create them in Notion.
    Body: { notes: str, assigned_to: str, client_name: str }
    """
    body = request.get_json(silent=True) or {}
    notes = body.get("notes", "").strip()
    assigned_to = body.get("assigned_to", "")
    client_name = body.get("client_name", "Internal")

    if not notes:
        return jsonify({"error": "notes is required"}), 400

    today = datetime.utcnow().strftime("%Y-%m-%d")

    system = f"""You are an expert project manager assistant for a creative agency.
The user has pasted raw meeting notes. Extract every clear action item or task from the notes.
For each task, guess the best assignee from this team list: Vidit (Design/Website), Nupur (Design), Abhinav (Website/Dev), Kshitij (Review), Mohit (Content), Tanaya (Accounts), Happy (Video).
If no one is obvious, use "{assigned_to or 'Unassigned'}".
Also guess a due date (within 7 days of today {today} unless notes specify otherwise, format YYYY-MM-DD).
Respond ONLY with a valid JSON array:
[
  {{"title": "Task title", "assigned_to": "Name", "due_date": "YYYY-MM-DD"}},
  ...
]
Only include genuine action items. Ignore discussion/context sentences."""

    try:
        resp = _claude_call(system, notes, max_tokens=800)
        match = re.search(r'\[.*\]', resp, re.DOTALL)
        tasks_raw = json.loads(match.group(0)) if match else []
    except Exception as e:
        logger.error(f"meeting_to_tasks parse failed: {e}")
        return jsonify({"error": "Failed to parse tasks from notes"}), 500

    created = []
    if notion_store.is_configured():
        for t in tasks_raw:
            result = notion_store.create_task(
                title=t.get("title", "Untitled"),
                client_name=client_name,
                client_notion_id="",
                assigned_to=t.get("assigned_to", assigned_to),
                due_date=t.get("due_date", today),
                status="not_started"
            )
            created.append({
                "title": t.get("title"),
                "assigned_to": t.get("assigned_to"),
                "due_date": t.get("due_date"),
                "notion_id": result.get("id") if result else None
            })
    else:
        # Return tasks without creating in Notion
        created = tasks_raw

    return jsonify({"success": True, "tasks": created})


# ── Feature 3: Manager's End-of-Day Summary ───────────────────────────────────
@ops_bp.route("/api/ai/daily-summary", methods=["POST"])
def daily_summary():
    """
    Read today's standup tasks for all employees and produce a manager summary.
    Body: { user_id: str }
    """
    body = request.get_json(silent=True) or {}
    today = datetime.utcnow().strftime("%Y-%m-%d")

    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, title, status, blocker
        FROM standup_tasks
        WHERE date = ?
        ORDER BY user_id
    """, (today,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return jsonify({"summary": "No standup data found for today. Ask your team to log their tasks!"}), 200

    # Group by user_id
    by_user = {}
    for user_id, title, status, blocker in rows:
        if user_id not in by_user:
            by_user[user_id] = []
        by_user[user_id].append({"title": title, "status": status, "blocker": blocker})

    # Load employee name map
    try:
        import json as _json
        emp_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'employees.json')
        with open(emp_path) as f:
            data = _json.load(f)
            employees_list = data.get("employees", [])
        emp_map = {e.get("id", ""): e.get("name", e.get("id", "")) for e in employees_list}
    except Exception as e:
        logger.error(f"Failed to load employee names for daily report: {e}")
        emp_map = {}

    standup_text = ""
    for uid, tasks in by_user.items():
        name = emp_map.get(uid, uid)
        standup_text += f"\n {name}:\n"
        for t in tasks:
            status_icon = "" if t["status"] == "done" else "⏳"
            standup_text += f"  {status_icon} {t['title']}"
            if t.get("blocker"):
                standup_text += f" [BLOCKER: {t['blocker']}]"
            standup_text += "\n"

    system = f"""You are a senior project manager reviewing the team's daily standup for {today}.
Here is a summary of what everyone did today:
{standup_text}

Write a concise end-of-day manager report with exactly 3 sections:
1.  **Wins Today** – What was accomplished (2-3 bullet points max)
2. ⚠ **Watch List** – Who is behind or has blockers (name specific people)
3.  **Tomorrow's Priority** – The single most critical thing the team must focus on

Be direct, specific, and use names. Keep total response under 200 words."""

    try:
        summary = _claude_call(system, "Generate the daily report.", max_tokens=400)
        return jsonify({"summary": summary})
    except Exception as e:
        logger.exception("daily_summary failed")
        return jsonify({"error": str(e)}), 500


# ── Feature 5: AI Tone-Matched Client Update Draft ───────────────────────────
@ops_bp.route("/api/ai/client-update", methods=["POST"])
def client_update_draft():
    """
    Generate a professional client update message after a task is submitted.
    Body: { task_title: str, client_name: str, submission_note: str, channel: 'email'|'whatsapp' }
    """
    body = request.get_json(silent=True) or {}
    task_title = body.get("task_title", "").strip()
    client_name = body.get("client_name", "Client").strip()
    submission_note = body.get("submission_note", "").strip()
    channel = body.get("channel", "email").lower()

    if not task_title:
        return jsonify({"error": "task_title is required"}), 400

    if channel == "whatsapp":
        style = "casual, friendly WhatsApp message (2-3 sentences, use one relevant emoji, no formal greeting)"
    else:
        style = "professional email (subject line + 3-4 sentence body, polite and concise)"

    system = f"""You are writing on behalf of a creative agency team member.
They just completed a task and want to send a {channel} update to their client.

Task completed: {task_title}
Client: {client_name}
Work summary: {submission_note or "Work has been completed as discussed."}

Write a {style}.
Do NOT mention internal tools, Notion, or anything technical.
Sound warm, professional, and confident. Make the client feel well taken care of."""

    try:
        draft = _claude_call(system, "Write the client update now.", max_tokens=300)
        return jsonify({"draft": draft})
    except Exception as e:
        logger.exception("client_update_draft failed")
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# CLIENT PORTAL — task feed (called from client-dashboard.html)
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/client-portal/tasks", methods=["GET"])
def client_portal_tasks():
    """
    Return tasks for the logged-in client. Reads identity from the
    verified client_session_token cookie (via auth.client_verify logic).
    Query params:
      client_name      — name of the client (set by the session verify)
      client_notion_id — optional Notion DB id to filter by
    """
    # Re-verify the client session here so this endpoint is self-contained
    from routes.auth import _verify_client_session
    token = (
        request.cookies.get("client_session_token", "")
        or request.headers.get("X-Client-Token", "")
        or request.args.get("token", "")
    )
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    client_name = client.get("client_name", "")
    client_notion_id = client.get("client_notion_id", "")

    tasks = []
    source = "sqlite"

    # ── Try Notion first ──────────────────────────────────────────────────────
    try:
        if notion_store.is_configured():
            notion_tasks = notion_store.list_tasks(
                client_notion_id=client_notion_id if client_notion_id else None,
            )
            # If no notion_id, filter by client name
            if not client_notion_id:
                notion_tasks = [
                    t for t in notion_tasks
                    if (t.get("client_name") or "").lower() == client_name.lower()
                ]
            tasks = [_shape_client_task(t, "notion") for t in notion_tasks]
            source = "notion"
    except Exception as e:
        logger.warning(f"Notion client portal fetch failed: {e}")

    # ── SQLite fallback ───────────────────────────────────────────────────────
    if source == "sqlite" or not tasks:
        conn = _su_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT t.id, t.title, t.status, t.progress, t.due_date,
                   t.assigned_to, t.description, t.created_at, c.name AS client_name
            FROM tasks t
            JOIN clients c ON c.id = t.client_id
            WHERE LOWER(c.name) = LOWER(?)
            ORDER BY t.due_date ASC, t.created_at DESC
        """, (client_name,))
        rows = cur.fetchall()
        conn.close()
        tasks = [_shape_client_task({
            "id": r[0], "title": r[1], "status": r[2], "progress": r[3] or 0,
            "due_date": r[4], "assigned_to": r[5], "description": r[6],
            "created_at": r[7], "client_name": r[8],
        }, "sqlite") for r in rows]
        source = "sqlite"

    # Fetch feedback for these tasks
    try:
        conn = _pt_conn()
        cur = conn.cursor()
        task_ids = [str(t.get("id")) for t in tasks if t.get("id")]
        if task_ids:
            placeholders = ",".join("?" * len(task_ids))
            cur.execute(f"SELECT task_id, status, comments, audio_url, updated_at FROM client_task_feedback WHERE task_id IN ({placeholders})", task_ids)
            feedback_map = {r[0]: {"status": r[1], "comments": r[2], "audio_url": r[3], "updated_at": r[4]} for r in cur.fetchall()}
            for t in tasks:
                t_id = str(t.get("id"))
                if t_id in feedback_map:
                    t["feedback"] = feedback_map[t_id]
        conn.close()
    except Exception as e:
        logger.error(f"Error fetching task feedback: {e}")

    return jsonify({
        "tasks": tasks,
        "client_name": client_name,
        "source": source,
        "count": len(tasks),
    })


def _shape_client_task(t: dict, source: str) -> dict:
    """Normalise a task dict for the clientDashboard frontend."""
    status = (t.get("status") or "not_started").lower().replace(" ", "_").replace("-", "_")
    progress = int(t.get("progress") or 0)

    # Compute progress from status when not explicitly set
    if progress == 0:
        progress = {
            "done": 100, "approved": 100,
            "submitted": 80, "in_review": 80,
            "in_progress": 50,
            "not_started": 0, "blocked": 0,
        }.get(status, 0)

    # Parse dates
    start_raw = t.get("start_date") or t.get("created_at") or ""
    due_raw   = t.get("due_date") or ""

    def _parse_date(s):
        if not s:
            return None
        return s.split("T")[0] if "T" in s else s

    desc = t.get("description") or ""
    brief = t.get("brief") or ""
    idea = t.get("idea") or ""
    caption = t.get("caption") or ""
    file_link = t.get("file_link") or ""

    # Parsed pipe-separated values are now handled by notion_store.list_tasks

    return {
        "id":          t.get("notion_id") or t.get("id") or "",
        "title":       t.get("title") or "Untitled Task",
        "status":      status,
        "progress":    progress,
        "assigned_to": t.get("assigned_to") or "",
        "description": desc,
        "start_date":  _parse_date(start_raw),
        "due_date":    _parse_date(due_raw),
        "client_name": t.get("client_name") or "",
        "source":      source,
        "service":     t.get("service") or "",
        "type":        t.get("type") or "",
        "brief":       brief,
        "content":     t.get("content") or "",
        "idea":        idea,
        "scripts_copy": t.get("scripts_copy") or "",
        "caption":     caption,
        "file_link":   file_link,
    }


@ops_bp.route("/api/client-portal/tasks/<task_id>/feedback", methods=["POST"])
def submit_task_feedback(task_id):
    from routes.auth import _verify_client_session
    token = request.cookies.get("client_session_token", "") or request.headers.get("X-Client-Token", "")
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    status = data.get("status")
    comments = data.get("comments", "")
    audio_url = data.get("audio_url", None)
    source = data.get("source", "sqlite")
    
    # Store feedback locally
    try:
        conn = _pt_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO client_task_feedback (task_id, status, comments, audio_url, updated_at) 
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, comments=excluded.comments, audio_url=excluded.audio_url, updated_at=CURRENT_TIMESTAMP
        """, (str(task_id), status, comments, audio_url))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving task feedback: {e}")
        return jsonify({"error": "Failed to save feedback"}), 500

    # Update actual task status
    if status in ("approved", "changes_requested", ""):
        if status == "approved":
            new_task_status = "approved"
        else:
            new_task_status = "pending_review"  # changes requested or cleared feedback

        if source == "notion":
            try:
                notion_store.update_task(task_id, status=new_task_status, progress=100 if new_task_status == "approved" else 80)
            except Exception as e:
                logger.error(f"Failed to update Notion task: {e}")
        else:
            try:
                conn = _su_conn()
                conn.execute("UPDATE tasks SET status = ?, progress = ? WHERE id = ?", 
                             (new_task_status, 100 if new_task_status == "approved" else 80, task_id))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Failed to update SQLite task: {e}")

    return jsonify({"success": True})


# ══════════════════════════════════════════════════════════════════════════════
# CLIENT PORTAL DEPENDENCIES (Files, Notes, Links)
# ══════════════════════════════════════════════════════════════════════════════

import uuid
from werkzeug.utils import secure_filename

@ops_bp.route("/api/client-portal/dependencies/upload", methods=["POST"])
def upload_dependency():
    from routes.auth import _verify_client_session
    token = request.cookies.get("client_session_token", "") or request.headers.get("X-Client-Token", "")
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    dep_type = request.form.get("type", "docs")

    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    original_name = secure_filename(file.filename) or "upload.file"
    ext = os.path.splitext(original_name)[1]
    
    # generate a unique filename
    unique_filename = f"{uuid.uuid4().hex}_{original_name}"
    
    from app import UPLOAD_DIR
    file_path = os.path.join(UPLOAD_DIR, unique_filename)
    file.save(file_path)

    content = f"/uploads/{unique_filename}"

    try:
        conn = _pt_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO client_dependencies (client_id, type, content, original_name) 
            VALUES (?, ?, ?, ?)
        """, (str(client["id"]), dep_type, content, original_name))
        conn.commit()
        dep_id = cur.lastrowid
        conn.close()
    except Exception as e:
        logger.error(f"Error saving dependency: {e}")
        return jsonify({"error": "Database error"}), 500

    return jsonify({"success": True, "id": dep_id, "url": content, "original_name": original_name})


@ops_bp.route("/api/client-portal/dependencies/text", methods=["POST"])
def save_text_dependency():
    from routes.auth import _verify_client_session
    token = request.cookies.get("client_session_token", "") or request.headers.get("X-Client-Token", "")
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    dep_type = data.get("type")
    content = data.get("content")

    if not dep_type or not content:
        return jsonify({"error": "Missing required fields"}), 400

    try:
        conn = _pt_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO client_dependencies (client_id, type, content) 
            VALUES (?, ?, ?)
        """, (str(client["id"]), dep_type, content))
        conn.commit()
        dep_id = cur.lastrowid
        conn.close()
    except Exception as e:
        logger.error(f"Error saving dependency: {e}")
        return jsonify({"error": "Database error"}), 500

    return jsonify({"success": True, "id": dep_id})


@ops_bp.route("/api/client-portal/dependencies", methods=["GET"])
def get_dependencies():
    from routes.auth import _verify_client_session
    token = request.cookies.get("client_session_token", "") or request.headers.get("X-Client-Token", "")
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        conn = _pt_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, type, content, original_name, created_at FROM client_dependencies WHERE client_id = ? ORDER BY created_at ASC", (str(client["id"]),))
        rows = cur.fetchall()
        conn.close()
        
        deps = []
        for r in rows:
            deps.append({
                "id": r[0],
                "type": r[1],
                "content": r[2],
                "original_name": r[3],
                "created_at": r[4]
            })
        return jsonify({"dependencies": deps})
    except Exception as e:
        logger.error(f"Error fetching dependencies: {e}")
        return jsonify({"error": "Database error"}), 500


@ops_bp.route("/api/client-portal/dependencies/<int:dep_id>", methods=["DELETE"])
def delete_dependency(dep_id):
    from routes.auth import _verify_client_session
    token = request.cookies.get("client_session_token", "") or request.headers.get("X-Client-Token", "")
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        conn = _pt_conn()
        cur = conn.cursor()
        
        # Optionally verify it belongs to this client and get file path
        cur.execute("SELECT client_id, content FROM client_dependencies WHERE id = ?", (dep_id,))
        row = cur.fetchone()
        if not row or str(row[0]) != str(client["id"]):
            conn.close()
            return jsonify({"error": "Not found or unauthorized"}), 404
            
        content_url = row[1]
        
        cur.execute("DELETE FROM client_dependencies WHERE id = ?", (dep_id,))
        conn.commit()
        conn.close()
        
        # If it was an uploaded file, delete it from disk
        if content_url and content_url.startswith("/uploads/"):
            filename = content_url.replace("/uploads/", "")
            from app import UPLOAD_DIR
            file_path = os.path.join(UPLOAD_DIR, filename)
            if os.path.exists(file_path):
                os.remove(file_path)
                
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error deleting dependency: {e}")
        return jsonify({"error": "Database error"}), 500

@ops_bp.route("/api/bet", methods=["GET"])
def get_bet():
    try:
        from db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key='bet_question'")
        row = cur.fetchone()
        question = row[0] if row else "Enter bet question here..."
        
        cur.execute("SELECT user_id, vote FROM mohit_bets")
        votes = [{"user_id": r[0], "vote": r[1]} for r in cur.fetchall()]
        conn.close()
        return jsonify({"success": True, "votes": votes, "question": question})
    except Exception as e:
        logger.error(f"Error getting bets: {e}")
        return jsonify({"success": False, "error": str(e)})

@ops_bp.route("/api/bet", methods=["POST"])
def post_bet():
    try:
        data = request.json
        user_id = data.get("user_id")
        vote = data.get("vote")
        if user_id not in ["emp002", "emp003", "emp007", "emp008"]:
            return jsonify({"success": False, "error": "Not allowed"}), 403
        
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute("INSERT OR REPLACE INTO mohit_bets (user_id, vote) VALUES (?, ?)", (user_id, vote))
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error posting bet: {e}")
        return jsonify({"success": False, "error": str(e)})

@ops_bp.route("/api/bet/question", methods=["POST"])
def post_bet_question():
    try:
        data = request.json
        user_id = data.get("user_id")
        question = data.get("question")
        if user_id not in ["emp002", "emp003", "emp007", "emp008"]:
            return jsonify({"success": False, "error": "Not allowed"}), 403
        
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('bet_question', ?)", (question,))
            # Clear previous votes since question changed
            conn.execute("DELETE FROM mohit_bets")
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error posting bet question: {e}")
        return jsonify({"success": False, "error": str(e)})

# ══════════════════════════════════════════════════════════════════════════════
# DISCOVERY QUESTIONNAIRE
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/form-templates", methods=["GET"])
def get_all_form_templates():
    try:
        from db import get_connection
        conn = get_connection()
        cur = conn.execute("SELECT id FROM form_templates ORDER BY id ASC")
        rows = cur.fetchall()
        conn.close()
        templates = [r[0] for r in rows]
        return jsonify({"success": True, "templates": templates})
    except Exception as e:
        logger.error(f"Error fetching form templates list: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@ops_bp.route("/api/form-templates/<template_id>", methods=["GET"])
def get_form_template(template_id):
    try:
        from db import get_connection
        conn = get_connection()
        cur = conn.execute("SELECT schema_json FROM form_templates WHERE id=?", (template_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return jsonify({"success": True, "template": json.loads(row[0])})
        return jsonify({"success": False, "error": "Template not found"}), 404
    except Exception as e:
        logger.error(f"Error fetching form template: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@ops_bp.route("/api/form-templates/<template_id>", methods=["POST"])
def save_form_template(template_id):
    try:
        data = request.json
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute("INSERT OR REPLACE INTO form_templates (id, schema_json) VALUES (?, ?)", (template_id, json.dumps(data.get("template", []))))
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving form template: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@ops_bp.route("/api/clients/<client_id>/form-answers", methods=["GET"])
def get_client_form_answers(client_id):
    try:
        from db import get_connection
        conn = get_connection()
        cur = conn.execute("SELECT answers_json FROM client_form_answers WHERE client_id=?", (client_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return jsonify({"success": True, "answers": json.loads(row[0])})
        return jsonify({"success": True, "answers": {}})
    except Exception as e:
        logger.error(f"Error fetching client form answers: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@ops_bp.route("/api/clients/<client_id>/form-answers", methods=["POST"])
def save_client_form_answers(client_id):
    try:
        data = request.json
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute("INSERT OR REPLACE INTO client_form_answers (client_id, answers_json) VALUES (?, ?)", (client_id, json.dumps(data.get("answers", {}))))
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving client form answers: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@ops_bp.route("/api/discovery-submissions", methods=["GET"])
def get_discovery_submissions():
    try:
        from db import get_connection
        conn = get_connection()
        cur = conn.execute("SELECT id, form_id, company_name, email, answers_json, submitted_at FROM discovery_submissions ORDER BY submitted_at DESC")
        rows = cur.fetchall()
        conn.close()
        submissions = []
        for r in rows:
            submissions.append({
                "id": r[0],
                "form_id": r[1],
                "company_name": r[2],
                "email": r[3],
                "answers": json.loads(r[4]),
                "submitted_at": r[5]
            })
        return jsonify({"success": True, "submissions": submissions})
    except Exception as e:
        logger.error(f"Error fetching discovery submissions: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@ops_bp.route("/api/discovery-submissions", methods=["POST"])
def save_discovery_submission():
    try:
        data = request.json
        form_id = data.get("form_id", "discovery_global").strip()
        company_name = data.get("company_name", "").strip()
        email = data.get("email", "").strip()
        answers = data.get("answers", {})
        
        if not company_name:
            return jsonify({"success": False, "error": "Company Name is required"}), 400
            
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO discovery_submissions (form_id, company_name, email, answers_json) VALUES (?, ?, ?, ?)", 
                (form_id, company_name, email, json.dumps(answers))
            )
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving discovery submission: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
