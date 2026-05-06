import sqlite3
import json
import os

# SQLite database file located in the persistent logs directory
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "app.db")

def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    # Enable Write-Ahead Logging for high concurrency
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_connection()
    with conn:
        # Budget tracking
        conn.execute("CREATE TABLE IF NOT EXISTS budget (period TEXT PRIMARY KEY, total_cost REAL DEFAULT 0)")
        
        # Conversations (storing the entire JSON dict to minimize refactoring)
        conn.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, data TEXT)")
        
        # User Memory
        conn.execute("CREATE TABLE IF NOT EXISTS memory (user_id TEXT PRIMARY KEY, data TEXT)")
        
        # Usage Logs
        conn.execute("CREATE TABLE IF NOT EXISTS usage_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)")
        
        # Attendance tracking
        conn.execute("CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, action TEXT, timestamp TEXT)")
    conn.close()

init_db()

# --- Migration Script from JSON to SQLite (Run Once) ---
def migrate_from_json():
    logs_dir = os.path.dirname(DB_PATH)
    conn = get_connection()
    
    # Migrate Conversations
    conv_file = os.path.join(logs_dir, "conversations.json")
    if os.path.exists(conv_file):
        try:
            with open(conv_file, "r") as f:
                data = json.load(f)
            with conn:
                for cid, cdata in data.items():
                    conn.execute("INSERT OR IGNORE INTO conversations (id, data) VALUES (?, ?)", (cid, json.dumps(cdata)))
            os.rename(conv_file, conv_file + ".bak")
        except Exception as e:
            print("Migration error conversations:", e)
            
    # Migrate Memory
    mem_file = os.path.join(logs_dir, "memory.json")
    if os.path.exists(mem_file):
        try:
            with open(mem_file, "r") as f:
                data = json.load(f)
            with conn:
                for uid, mdata in data.items():
                    conn.execute("INSERT OR IGNORE INTO memory (user_id, data) VALUES (?, ?)", (uid, json.dumps(mdata)))
            os.rename(mem_file, mem_file + ".bak")
        except Exception as e:
            print("Migration error memory:", e)
            
    # Migrate Budget & Usage
    usage_file = os.path.join(logs_dir, "usage.json")
    if os.path.exists(usage_file):
        try:
            with open(usage_file, "r") as f:
                data = json.load(f)
            with conn:
                budget = data.get("budget", {})
                for period, bdata in budget.items():
                    conn.execute("INSERT OR IGNORE INTO budget (period, total_cost) VALUES (?, ?)", (period, bdata.get("total_cost", 0.0)))
                logs = data.get("logs", [])
                for log in logs:
                    conn.execute("INSERT INTO usage_logs (data) VALUES (?)", (json.dumps(log),))
            os.rename(usage_file, usage_file + ".bak")
        except Exception as e:
            print("Migration error usage:", e)
            
    conn.close()

migrate_from_json()
