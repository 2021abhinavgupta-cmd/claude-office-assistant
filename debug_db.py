import sqlite3
conn = sqlite3.connect('logs/app.db')
cur = conn.cursor()
cur.execute('PRAGMA table_info(standup_tasks)')
for r in cur.fetchall():
    print(r)
