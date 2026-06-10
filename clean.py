import sqlite3
import os

db_path = os.path.join(os.getcwd(), 'logs', 'app.db')
if os.path.exists(db_path):
    print('Found db at', db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM standup_tasks WHERE date >= date('now', '-1 day') AND (title LIKE '%[Carousel]%' OR title LIKE '%[Reel]%')")
    print(f'Deleted {cur.rowcount} social tasks.')
    conn.commit()
else:
    print('db not found at', db_path)
