import re

with open('backend/routes/ops.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_str = """                else:
                if not insert_allowed:
                    cur.execute("DELETE FROM standup_tasks WHERE id=?", (matched_id,))
                else:
                    cur.execute(
                        "UPDATE standup_tasks SET title=?, due_date=?, notion_id=? WHERE id=?",
                        (search_title, d, nid, matched_id)
                    )"""

new_str = """                else:
                    if not insert_allowed:
                        cur.execute("DELETE FROM standup_tasks WHERE id=?", (matched_id,))
                    else:
                        cur.execute(
                            "UPDATE standup_tasks SET title=?, due_date=?, notion_id=? WHERE id=?",
                            (search_title, d, nid, matched_id)
                        )"""

content = content.replace(old_str, new_str)

with open('backend/routes/ops.py', 'w', encoding='utf-8') as f:
    f.write(content)
