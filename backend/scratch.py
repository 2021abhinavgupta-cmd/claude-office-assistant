import sqlite3
import os

db_path = os.path.join(os.path.dirname(__file__), "..", "logs", "app.db")
if not os.path.exists(db_path):
    print(f"Database not found at {db_path}")
else:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        SELECT id, user_id, title, status, created_at, date
        FROM standup_tasks
        ORDER BY created_at DESC
        LIMIT 20
    """)

    rows = cur.fetchall()
    if not rows:
        print("No standup tasks found in the entire database.")
    else:
        print(f"{'ID':<5} {'User':<10} {'Status':<10} {'Created At (UTC)':<22} {'Date':<12} {'Title'}")
        print("-" * 100)
        for row in rows:
            print(f"{row[0]:<5} {row[1]:<10} {row[3]:<10} {str(row[4]):<22} {str(row[5]):<12} {row[2]}")

    conn.close()
