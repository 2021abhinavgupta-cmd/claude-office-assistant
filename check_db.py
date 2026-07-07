import sqlite3
import re

def _normalize_title(t):
    if not t: return ''
    t = t.replace('“', '"').replace('”', '"').replace('‘', "'").replace('’', "'")
    t = re.sub(r'^\[.*?\]\s*', '', t)
    return t.lower().strip()

conn = sqlite3.connect('logs/app.db')
cur = conn.cursor()
cur.execute("SELECT id, title, notion_id, due_date FROM standup_tasks WHERE title LIKE '%expensive%' OR title LIKE '%Post 4%'")
rows = cur.fetchall()
for r in rows:
    print(r, _normalize_title(r[1]))
