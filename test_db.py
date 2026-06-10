import os, sys, json, sqlite3
conn = sqlite3.connect('logs/app.db')
cur = conn.cursor()
cur.execute("SELECT id, user_id, date, title, notion_id, status FROM standup_tasks WHERE user_id='emp008' AND date='2026-06-10'")
rows = cur.fetchall()
for r in rows:
    print(r)
