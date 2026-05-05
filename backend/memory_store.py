"""
User Memory Store — Cross-conversation persistent memory per employee.
Memories are injected into every Claude system prompt automatically.
Stored in SQLite via db.py
"""
import json, uuid, logging
from datetime import datetime

from backend.db import get_connection

MAX_PER_USER = 50

logger = logging.getLogger(__name__)

def _now(): return datetime.utcnow().isoformat() + "Z"

def get_memories(user_id: str) -> list:
    """Return all memories for a user, newest first."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM memory WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return list(reversed(json.loads(row[0])))
    return []

def add_memory(user_id: str, content: str, source: str = "manual") -> dict:
    """Add a memory. Returns the new memory dict."""
    content = content.strip()[:500]
    if not content: return {}
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM memory WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    
    mems = json.loads(row[0]) if row else []
    mem = {"id": uuid.uuid4().hex[:10], "content": content,
           "source": source, "created_at": _now()}
    mems.append(mem)
    if len(mems) > MAX_PER_USER:
        mems = mems[-MAX_PER_USER:]
        
    with conn:
        conn.execute("INSERT OR REPLACE INTO memory (user_id, data) VALUES (?, ?)", (user_id, json.dumps(mems)))
    conn.close()
    return mem

def delete_memory(user_id: str, memory_id: str) -> bool:
    """Delete a memory by ID."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM memory WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
        
    mems = json.loads(row[0])
    new = [m for m in mems if m["id"] != memory_id]
    if len(new) == len(mems):
        conn.close()
        return False
        
    with conn:
        conn.execute("INSERT OR REPLACE INTO memory (user_id, data) VALUES (?, ?)", (user_id, json.dumps(new)))
    conn.close()
    return True

def format_for_prompt(user_id: str) -> str:
    """Format memories for injection into Claude's system prompt."""
    mems = get_memories(user_id)
    if not mems: return ""
    lines = "\n".join(f"• {m['content']}" for m in mems[-20:])
    return f"\n\n## What you remember about this user:\n{lines}"
