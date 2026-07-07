import sqlite3
from datetime import datetime

# Setup fake DB
conn = sqlite3.connect(':memory:')
cur = conn.cursor()
cur.execute('''CREATE TABLE standup_tasks (id INTEGER PRIMARY KEY, user_id TEXT, date TEXT, title TEXT, notion_id TEXT, status TEXT)''')
today_str = datetime.utcnow().strftime("%Y-%m-%d")

# Insert fake tasks
cur.execute("INSERT INTO standup_tasks (user_id, date, notion_id, status) VALUES ('emp008', ?, 'task_1', 'pending')", (today_str,))
cur.execute("INSERT INTO standup_tasks (user_id, date, notion_id, status) VALUES ('emp003', ?, 'task_1', 'pending')", (today_str,))
cur.execute("INSERT INTO standup_tasks (user_id, date, notion_id, status) VALUES ('emp008', ?, 'task_2', 'pending')", (today_str,))
conn.commit()

print("Before:")
for row in cur.execute("SELECT * FROM standup_tasks"):
    print(row)

# Simulate sync_all = True for task_1 assigned ONLY to emp003
sync_all = True
nid = 'task_1'
insert_allowed = True
target_uids = ['emp003']

if sync_all and nid:
    if insert_allowed and target_uids:
        placeholders = ",".join("?" * len(target_uids))
        query = f"DELETE FROM standup_tasks WHERE notion_id=? AND (date=? OR status NOT IN ('Completed', 'Archived')) AND user_id NOT IN ({placeholders})"
        params = [nid, today_str] + target_uids
        cur.execute(query, params)
    else:
        cur.execute("DELETE FROM standup_tasks WHERE notion_id=? AND (date=? OR status NOT IN ('Completed', 'Archived'))", (nid, today_str))

# Simulate sync_all = True for task_2 unassigned
nid2 = 'task_2'
insert_allowed2 = False
target_uids2 = ['emp001', 'emp002', 'emp003', 'emp004', 'emp006', 'emp007', 'emp008', 'emp009', 'emp010'] # fallback

if sync_all and nid2:
    if insert_allowed2 and target_uids2:
        placeholders = ",".join("?" * len(target_uids2))
        query = f"DELETE FROM standup_tasks WHERE notion_id=? AND (date=? OR status NOT IN ('Completed', 'Archived')) AND user_id NOT IN ({placeholders})"
        params = [nid2, today_str] + target_uids2
        cur.execute(query, params)
    else:
        cur.execute("DELETE FROM standup_tasks WHERE notion_id=? AND (date=? OR status NOT IN ('Completed', 'Archived'))", (nid2, today_str))


print("\nAfter:")
for row in cur.execute("SELECT * FROM standup_tasks"):
    print(row)

