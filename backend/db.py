import sqlite3
import json
import os
import time
import functools
import logging

logger = logging.getLogger(__name__)

# DB_PATH env var lets you point to a Railway volume (e.g. DB_PATH=/logs/app.db)
# Falls back to the local logs/ directory for development.
_default_db = os.path.join(os.path.dirname(__file__), "..", "logs", "app.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)

def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    # Enable Write-Ahead Logging for high concurrency
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL is the recommended synchronous setting for WAL
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def with_retry(max_retries=5, initial_delay=0.1):
    """Decorator to retry SQLite operations if the database is locked."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e) and attempt < max_retries - 1:
                        logger.warning(f"Database locked, retrying {func.__name__} in {delay}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(delay)
                        delay *= 2  # Exponential backoff
                    else:
                        raise
        return wrapper
    return decorator

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
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            date TEXT NOT NULL,
            checkin_time TEXT,
            checkout_time TEXT,
            UNIQUE(user_id, date)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_attendance_date ON daily_attendance (date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_attendance_user_date ON daily_attendance (user_id, date)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_user_date ON daily_attendance(user_id, date)"
        )

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
        
        # Mohit bet
        conn.execute("""CREATE TABLE IF NOT EXISTS mohit_bets (
            user_id TEXT PRIMARY KEY,
            vote TEXT NOT NULL
        )""")

        # Personal daily task tracker (separate from project tasks)
        conn.execute("""CREATE TABLE IF NOT EXISTS standup_tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT NOT NULL,
            date         TEXT NOT NULL,
            title        TEXT NOT NULL,
            status       TEXT DEFAULT 'pending',
            carried_from TEXT DEFAULT NULL,
            blocker      TEXT DEFAULT NULL,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_standup_tasks_user_date ON standup_tasks (user_id, date)")
        
        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN blocker TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN notion_id TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists



        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN subtasks TEXT DEFAULT '[]'")
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN delegated_to TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN delegated_from TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN due_date TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        # Task risk escalation log (tracks alert level per task)
        conn.execute("""CREATE TABLE IF NOT EXISTS task_risk (
            task_id     TEXT PRIMARY KEY,
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

        # Client portal users (separate from employees)
        conn.execute("""CREATE TABLE IF NOT EXISTS client_users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password      TEXT NOT NULL,
            client_name   TEXT NOT NULL,
            client_notion_id TEXT DEFAULT '',
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        # Client portal dependencies (files, notes, links)
        conn.execute("""CREATE TABLE IF NOT EXISTS client_dependencies (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id     TEXT NOT NULL,
            type          TEXT NOT NULL,
            content       TEXT NOT NULL,
            original_name TEXT,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        # Client portal sessions (separate from employee sessions)
        conn.execute("""CREATE TABLE IF NOT EXISTS client_sessions (
            token       TEXT PRIMARY KEY,
            client_id   INTEGER NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at  TEXT NOT NULL
        )""")

        # Client task feedback
        conn.execute("""CREATE TABLE IF NOT EXISTS client_task_feedback (
            task_id     TEXT PRIMARY KEY,
            status      TEXT,
            comments    TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        try:
            conn.execute("ALTER TABLE client_task_feedback ADD COLUMN audio_url TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        # Projects migration
        try:
            # Check if old schema exists (has 'data' column)
            cursor = conn.execute("PRAGMA table_info(projects)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'data' in columns and 'name' not in columns:
                conn.execute("DROP TABLE projects")
        except Exception:
            pass

        # Projects
        conn.execute("""CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            instructions TEXT,
            memory TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        # Discovery Questionnaire
        conn.execute("""CREATE TABLE IF NOT EXISTS form_templates (
            id TEXT PRIMARY KEY,
            schema_json TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS client_form_answers (
            client_id TEXT PRIMARY KEY,
            answers_json TEXT NOT NULL
        )""")
        
        # Seed default questionnaire
        cur = conn.execute("SELECT id FROM form_templates WHERE id='discovery_global'")
        if not cur.fetchone():
            default_questions = [
                {"id": "q1", "section": "1. Company & Project Overview", "label": "Company Name", "type": "text"},
                {"id": "q2", "section": "1. Company & Project Overview", "label": "What does your company do and how does this platform support your operations?", "type": "textarea"},
                {"id": "q3", "section": "1. Company & Project Overview", "label": "What are the top three business goals?", "type": "textarea"},
                {"id": "q4", "section": "1. Company & Project Overview", "label": "What would define a successful redesign?", "type": "textarea"},
                {"id": "q5", "section": "2. Users & Personas", "label": "Primary user groups", "type": "textarea"},
                {"id": "q6", "section": "2. Users & Personas", "label": "Most active users", "type": "textarea"},
                {"id": "q7", "section": "2. Users & Personas", "label": "Devices used", "type": "text"},
                {"id": "q8", "section": "2. Users & Personas", "label": "Biggest pain points", "type": "textarea"},
                {"id": "q9", "section": "3. Workflow Analysis", "label": "Describe the most common workflow", "type": "textarea"},
                {"id": "q10", "section": "3. Workflow Analysis", "label": "Which tasks take longest?", "type": "textarea"},
                {"id": "q11", "section": "3. Workflow Analysis", "label": "Where do users get confused?", "type": "textarea"},
                {"id": "q12", "section": "3. Workflow Analysis", "label": "Which steps can be simplified?", "type": "textarea"},
                {"id": "q13", "section": "4. Design Preferences", "label": "Preferred style (Minimal, Corporate, Industrial, Premium, Modern SaaS)", "type": "text"},
                {"id": "q14", "section": "4. Design Preferences", "label": "Preferred theme (Light/Dark/Both)", "type": "text"},
                {"id": "q15", "section": "4. Design Preferences", "label": "Link 3-5 reference products and why", "type": "textarea"},
                {"id": "q16", "section": "4. Design Preferences", "label": "Designs to avoid", "type": "textarea"},
                {"id": "q17", "section": "4. Design Preferences", "label": "Animation or interaction preferences", "type": "textarea"},
                {"id": "q18", "section": "5. Branding", "label": "Are their any Brand guidelines", "type": "textarea"},
                {"id": "q19", "section": "5. Branding", "label": "Preferred colors & why?", "type": "textarea"},
                {"id": "q20", "section": "5. Branding", "label": "Preferred typography & why?", "type": "textarea"},
                {"id": "q21", "section": "6. Dashboard & Navigation", "label": "Information shown after login", "type": "textarea"},
                {"id": "q22", "section": "6. Dashboard & Navigation", "label": "Important KPIs", "type": "textarea"},
                {"id": "q23", "section": "6. Dashboard & Navigation", "label": "Navigation preference", "type": "textarea"},
                {"id": "q24", "section": "6. Dashboard & Navigation", "label": "Simple vs data-rich dashboard", "type": "textarea"},
                {"id": "q25", "section": "6. Dashboard & Navigation", "label": "Preferred charts and visualizations", "type": "textarea"},
                {"id": "q26", "section": "7. Screen Prioritization", "label": "Dashboard, Projects, Tasks", "type": "textarea"},
                {"id": "q27", "section": "8. Mobile Experience", "label": "Desktop", "type": "textarea"},
                {"id": "q28", "section": "8. Mobile Experience", "label": "Tablet", "type": "textarea"},
                {"id": "q29", "section": "8. Mobile Experience", "label": "Mobile usage", "type": "textarea"},
                {"id": "q30", "section": "9. Accessibility & Technical Constraints", "label": "Components that cannot change", "type": "textarea"},
                {"id": "q31", "section": "10. Success & Delivery", "label": "Success metrics", "type": "textarea"},
                {"id": "q32", "section": "10. Success & Delivery", "label": "Additional comments", "type": "textarea"}
            ]
            conn.execute("INSERT INTO form_templates (id, schema_json) VALUES (?, ?)", ("discovery_global", json.dumps(default_questions)))

        conn.execute("""CREATE TABLE IF NOT EXISTS project_files (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            filename TEXT,
            content TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        # Project Knowledge Base search index (FTS5)
        # Stores chunked text for fast, System-Projects-like retrieval.
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

        # Add task_type column to tasks if not already present
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN task_type TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        # ── Startup cleanup: remove orphaned client_users ──────────────
        # Removes portal credentials for clients that no longer exist in the DB.
        # This fixes stuck "username already taken" errors after a client is deleted.
        try:
            conn.execute("""
                DELETE FROM client_users
                WHERE client_notion_id != ''
                AND client_notion_id NOT IN (
                    SELECT CAST(id AS TEXT) FROM clients
                )
            """)
        except Exception:
            pass  # Silently skip if schema not ready yet

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
            
    # Migrate Memory mem_file prep
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
