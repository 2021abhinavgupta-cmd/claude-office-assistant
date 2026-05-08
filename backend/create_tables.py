"""
Run once to create the project tracker tables.
Usage: python backend/create_tables.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from db import get_connection

conn = get_connection()
conn.executescript("""
CREATE TABLE IF NOT EXISTS clients (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  contact     TEXT,
  requirements TEXT,
  deadline    TEXT,
  status      TEXT DEFAULT 'active',
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id        INTEGER,
  title            TEXT NOT NULL,
  description      TEXT,
  assigned_to      TEXT,
  status           TEXT DEFAULT 'not_started',
  progress         INTEGER DEFAULT 0,
  due_date         TEXT,
  submission_note  TEXT,
  submission_file  TEXT,
  rejection_note   TEXT,
  submission_count INTEGER DEFAULT 0,
  opened_at        TEXT,
  created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (client_id) REFERENCES clients(id)
);

CREATE TABLE IF NOT EXISTS dependencies (
  task_id             INTEGER,
  depends_on_task_id  INTEGER,
  PRIMARY KEY (task_id, depends_on_task_id)
);
""")
conn.commit()
conn.close()
print("✅ Project tracker tables created successfully.")
