import sys

with open('backend/routes/ops.py', 'r', encoding='utf-8') as f:
    code = f.read()

target1 = '''        if not assigned_name:
            return jsonify({"error": "assigned_name required"}), 400
        all_tasks = notion_store.list_tasks(assigned_to=assigned_name)

    # Filter active tasks
    valid_tasks = []
    for t in all_tasks:'''

replacement1 = '''        if not assigned_name:
            return jsonify({"error": "assigned_name required"}), 400
        all_tasks = notion_store.list_tasks(assigned_to=assigned_name)

    # Pre-fetch existing notion_ids for today to ensure we update them even if they are future tasks
    existing_notion_ids = set()
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("SELECT notion_id FROM standup_tasks WHERE date=?", (today_str,))
    for r in cur.fetchall():
        if r[0]: existing_notion_ids.add(r[0])
    conn.close()

    # Filter active tasks
    valid_tasks = []
    for t in all_tasks:'''

if target1 in code:
    code = code.replace(target1, replacement1)
    print('Replaced chunk 1')
else:
    print('Failed to find chunk 1')


target2 = '''        # Also, if body explicitly requested "upcoming", we can pull all not_started tasks
        pull_all_upcoming = body.get("pull_upcoming", False)
        
        if is_active or is_due or is_upcoming or is_creation_today or (pull_all_upcoming and s == "not_started"):
            valid_tasks.append(t)'''

replacement2 = '''        # Also, if body explicitly requested "upcoming", we can pull all not_started tasks
        pull_all_upcoming = body.get("pull_upcoming", False)
        
        if is_active or is_due or is_upcoming or is_creation_today or (pull_all_upcoming and s == "not_started") or (t.get("notion_id") in existing_notion_ids):
            valid_tasks.append(t)'''

if target2 in code:
    code = code.replace(target2, replacement2)
    print('Replaced chunk 2')
else:
    print('Failed to find chunk 2')

with open('backend/routes/ops.py', 'w', encoding='utf-8') as f:
    f.write(code)

