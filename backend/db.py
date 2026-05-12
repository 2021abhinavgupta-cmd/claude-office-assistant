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
    # NORMAL is the recommended synchronous setting for WAL
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    conn = get_connection()
    with conn:
        # Budget tracking
        conn.execute("CREATE TABLE IF NOT EXISTS budget (period TEXT PRIMARY KEY, total_cost REAL DEFAULT 0)")
        
        # Conversations (storing the entire JSON dict to minimize refactoring)
        conn.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, data TEXT)")
        # Index to prevent full table scans when querying by user_id
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations (json_extract(data, '$.user_id'))")
        
        # User Memory
        conn.execute("CREATE TABLE IF NOT EXISTS memory (user_id TEXT PRIMARY KEY, data TEXT)")
        
        # Custom Skills
        conn.execute("""CREATE TABLE IF NOT EXISTS custom_skills (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            emoji TEXT DEFAULT '⚡',
            model TEXT DEFAULT 'haiku',
            task_type TEXT DEFAULT 'general',
            prompt TEXT NOT NULL,
            is_shared INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        
        # Usage Logs
        conn.execute("CREATE TABLE IF NOT EXISTS usage_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)")
        
        # Attendance tracking
        conn.execute("CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, action TEXT, timestamp TEXT)")

        # Daily standups
        conn.execute("""CREATE TABLE IF NOT EXISTS standups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            date        TEXT NOT NULL,
            yesterday   TEXT,
            today       TEXT,
            blockers    TEXT,
            submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, date)
        )""")

        # Task risk escalation log (tracks alert level per task)
        conn.execute("""CREATE TABLE IF NOT EXISTS task_risk (
            task_id     INTEGER PRIMARY KEY,
            risk_level  TEXT DEFAULT 'normal',
            alerted_day1 INTEGER DEFAULT 0,
            alerted_day2 INTEGER DEFAULT 0,
            alerted_day3 INTEGER DEFAULT 0,
            alerted_day5 INTEGER DEFAULT 0,
            last_checked TEXT,
            updated_at  TEXT
        )""")

        # Server-side auth sessions (#1)
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token       TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at  TEXT NOT NULL
        )""")

        # Projects
        conn.execute("""CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            data TEXT
        )""")

        # Project Knowledge Base search index (FTS5)
        # Stores chunked text for fast, Claude-Projects-like retrieval.
        try:
            conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
                project_id UNINDEXED,
                user_id    UNINDEXED,
                doc_id     UNINDEXED,
                filename   UNINDEXED,
                chunk,
                tokenize='porter'
            )""")
        except Exception:
            # FTS5 might be unavailable in some SQLite builds; app will gracefully fall back.
            pass

        # NOTE: Semantic KB retrieval (embeddings) intentionally not enabled by default
        # to avoid requiring an additional embeddings API key.

        # Add category column to tasks if not already present
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN category TEXT DEFAULT 'general'")
        except Exception:
            pass  # Column already exists

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
