"""
User Memory Store — Cross-conversation persistent memory per employee.
Memories are injected into every Claude system prompt automatically.
Stored in SQLite via db.py
"""
import json, uuid, logging
from datetime import datetime

from db import get_connection

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

def update_profile(user_id: str, profile_json: str):
    """Updates the user's structured JSON profile."""
    try:
        new_data = json.loads(profile_json)
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM memory WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        
        mems = json.loads(row[0]) if row else {}
        
        # If the existing memory is a list (legacy), convert it to dict
        if isinstance(mems, list):
            mems = {"legacy_notes": [m.get("content") for m in mems]}
            
        # Deep merge new data into mems
        for k, v in new_data.items():
            if isinstance(v, dict) and isinstance(mems.get(k), dict):
                mems[k].update(v)
            elif isinstance(v, list) and isinstance(mems.get(k), list):
                # Ensure unique items without breaking order, or just extend
                mems[k] = list({str(item): item for item in (mems[k] + v)}.values())
            else:
                mems[k] = v
                
        with conn:
            conn.execute("INSERT OR REPLACE INTO memory (user_id, data) VALUES (?, ?)", (user_id, json.dumps(mems)))
        conn.close()
    except Exception as e:
        logger.error(f"Failed to update profile for {user_id}: {e}")

def add_memory(user_id: str, content: str, source: str = "manual") -> dict:
    """Add a memory manually."""
    content = content.strip()[:500]
    if not content: return {}
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM memory WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    
    mems = json.loads(row[0]) if row else {}
    if isinstance(mems, list):
        mems = {"legacy_notes": [m.get("content") for m in mems]}
        
    if "legacy_notes" not in mems:
        mems["legacy_notes"] = []
    
    mem = {"id": uuid.uuid4().hex[:10], "content": content,
           "source": source, "created_at": _now()}
    mems["legacy_notes"].append(content)
    
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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM memory WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row: return ""
    try:
        data = json.loads(row[0])
        if isinstance(data, list):
            lines = "\n".join(f"• {m['content']}" for m in data[-20:])
            return f"\n\n## What you remember about this user:\n{lines}"
        else:
            return f"\n\n## What you remember about this user:\n```json\n{json.dumps(data, indent=2)}\n```"
    except:
        return ""

def format_team_memories() -> str:
    """Format all team memories to allow cross-pollination of writing styles."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, data FROM memory")
    rows = cursor.fetchall()
    conn.close()
    
    import os
    from pathlib import Path
    emp_map = {}
    try:
        with open(Path(__file__).parent.parent / "config" / "employees.json", "r") as f:
            for e in json.load(f).get("employees", []):
                emp_map[e["id"]] = e["name"]
    except Exception:
        pass

    res = []
    for uid, data in rows:
        name = emp_map.get(uid, uid)
        try:
            mems = list(reversed(json.loads(data)))
            if mems:
                lines = "\n".join(f"  • {m['content']}" for m in mems[-10:])
                res.append(f"[{name}'s Preferences]:\n{lines}")
        except:
            pass
            
    if not res: return ""
    return "\n\n## SHARED TEAM MEMORY (Use these if asked to write in another employee's style):\n" + "\n\n".join(res)
