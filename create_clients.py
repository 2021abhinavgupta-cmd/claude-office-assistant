import sqlite3
import os

db_path = r"c:\Users\abhin\OneDrive\Desktop\claude-office-assistant\logs\app.db"

clients = [
    ("topgreen", "password123", "TopGreen", ""),
    ("metazune", "password123", "METAZUNE", ""),
    ("evault", "password123", "Evault", "")
]

conn = sqlite3.connect(db_path)
cur = conn.cursor()

print("Creating client users...")
for username, password, name, notion_id in clients:
    try:
        cur.execute(
            "INSERT INTO client_users (username, password, client_name, client_notion_id) VALUES (?,?,?,?)",
            (username, password, name, notion_id)
        )
        print(f"Created: {username} for {name}")
    except sqlite3.IntegrityError:
        print(f"User {username} already exists, skipping.")
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            print("Table 'client_users' does not exist yet. Please start the app once to initialize the database.")
            break
        raise e

conn.commit()
conn.close()

print("Done!")
