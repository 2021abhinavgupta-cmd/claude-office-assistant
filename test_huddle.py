import json
import sqlite3
import os

DB_PATH = "backend/office.db"

def test_huddle():
    if not os.path.exists(DB_PATH):
        print("DB not found")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Find the conversation 'yoo'
    cursor.execute("SELECT data FROM conversations")
    rows = cursor.fetchall()
    
    found = None
    for (row,) in rows:
        data = json.loads(row)
        if data.get("title") == "yoo" or data.get("participant_ids"):
            print("Found conv:", data.get("title"), data.get("participant_ids"))
            found = data
            
    if not found:
        print("No huddle convs found.")

    user_id = "emp002"
    cursor.execute(
        "SELECT data FROM conversations WHERE json_extract(data, '$.user_id') = ? "
        "OR data LIKE ?",
        (user_id, f'%"{user_id}"%')
    )
    res = cursor.fetchall()
    print(f"\nQuerying for {user_id} returned {len(res)} results.")
    for (r,) in res:
        c = json.loads(r)
        print(" ->", c.get("title"), c.get("participant_ids"))

test_huddle()
