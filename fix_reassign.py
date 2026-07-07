with open('backend/routes/ops.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    new_lines.append(line)
    if "for target_user_id in target_uids:" in line and "cur.execute" not in line and "local_tasks =" not in line:
        # Check if we already inserted it
        if "Robust cleanup:" not in "".join(new_lines[-10:]):
            insert_str = """
            # Robust cleanup: Ensure users who are no longer assigned do not keep this task
            if sync_all and nid:
                if insert_allowed and target_uids:
                    placeholders = ",".join("?" * len(target_uids))
                    query = f"DELETE FROM standup_tasks WHERE notion_id=? AND (date=? OR status NOT IN ('Completed', 'Archived')) AND user_id NOT IN ({placeholders})"
                    params = [nid, today_str] + target_uids
                    cur.execute(query, params)
                else:
                    cur.execute("DELETE FROM standup_tasks WHERE notion_id=? AND (date=? OR status NOT IN ('Completed', 'Archived'))", (nid, today_str))
"""
            new_lines.insert(-1, insert_str)

with open('backend/routes/ops.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
