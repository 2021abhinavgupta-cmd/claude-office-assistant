import sqlite3
conn = sqlite3.connect('logs/app.db')
cur = conn.cursor()
cur.execute("PRAGMA wal_checkpoint(FULL)")
cur.execute('SELECT id, date, user_id, title, due_date, notion_id FROM standup_tasks ORDER BY date DESC LIMIT 20')
for row in cur.fetchall():
    print(row)
