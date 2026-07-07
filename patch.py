import json
with open('backend/routes/ops.py', 'r', encoding='utf-8') as f:
    content = f.read()

replacement = """# ══════════════════════════════════════════════════════════════════════════════
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
"""

target = """# ══════════════════════════════════════════════════════════════════════════════
# PERSONAL DAILY TASK TRACKER (separate from project tasks)
# ══════════════════════════════════════════════════════════════════════════════"""

content = content.replace(target, replacement)

with open('backend/routes/ops.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("done")
