import re

with open('backend/routes/ops.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix 1: t.get("id")
old_nid = r'nid = t.get\("id"\)'
new_nid = r'nid = t.get("notion_id") or t.get("id")'
content = re.sub(old_nid, new_nid, content)

# Fix 2: Unassign deletion
old_cleanup = r'# Clean up any tasks that were in the local DB but no longer exist in Notion \(deleted/archived\)\n\s*# We only do this if sync_all is True, because otherwise we only fetched tasks for one user!\n\s*if sync_all:\n\s*fetched_notion_ids = set\(t\.get\("notion_id"\) for t in all_tasks if t\.get\("notion_id"\)\)\n\s*for nid in existing_notion_ids:\n\s*if nid not in fetched_notion_ids:\n\s*try:\n\s*cur\.execute\("DELETE FROM standup_tasks WHERE notion_id=\?", \(nid,\)\)\n\s*except: pass'

new_cleanup = """        # Clean up any tasks that were in the local DB but no longer exist in fetched list
        fetched_notion_ids = set(t.get("notion_id") or t.get("id") for t in all_tasks if t.get("notion_id") or t.get("id"))
        
        if sync_all:
            for nid in existing_notion_ids:
                if nid not in fetched_notion_ids:
                    try:
                        cur.execute("DELETE FROM standup_tasks WHERE notion_id=?", (nid,))
                    except: pass
        else:
            cur.execute("SELECT notion_id FROM standup_tasks WHERE user_id=? AND (date=? OR status NOT IN ('Completed', 'Archived'))", (user_id, today_str))
            my_existing_nids = set(r[0] for r in cur.fetchall() if r[0])
            for nid in my_existing_nids:
                if nid not in fetched_notion_ids:
                    try:
                        cur.execute("DELETE FROM standup_tasks WHERE notion_id=? AND user_id=?", (nid, user_id))
                    except: pass"""

content = re.sub(old_cleanup, new_cleanup, content)

with open('backend/routes/ops.py', 'w', encoding='utf-8') as f:
    f.write(content)
