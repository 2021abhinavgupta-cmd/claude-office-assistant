import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
conn = sqlite3.connect('logs/app.db')
cur = conn.cursor()
cur.execute('SELECT id, title, status, user_id FROM standup_tasks')
rows = cur.fetchall()
for r in rows:
    print(r)
