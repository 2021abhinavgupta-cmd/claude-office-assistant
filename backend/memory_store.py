"""
User Memory Store — Cross-conversation persistent memory per employee.
Memories are injected into every Claude system prompt automatically.
Stored in SQLite via db.py

Data format (always a JSON list of objects):
  [{"id": "abc123", "content": "...", "source": "manual", "created_at": "..."}, ...]
"""
import json, uuid, logging
from datetime import datetime

from db import get_connection

MAX_PER_USER = 50

logger = logging.getLogger(__name__)

def _now(): return datetime.utcnow().isoformat() + "Z"


def _normalize(raw_data) -> list:
    """
    Normalize whatever is stored in DB into a consistent list of dicts.
    Handles:
      - list of {"id", "content", ...} objects  → returned as-is
      - list of strings                          → wrapped into objects
      - dict with "legacy_notes" key             → extracted and wrapped
      - any other dict                           → each key-value pair becomes a note
    """
    if isinstance(raw_data, list):
        result = []
        for item in raw_data:
            if isinstance(item, dict) and "content" in item:
                # Already well-formed
                result.append(item)
            elif isinstance(item, str):
                result.append({
                    "id": uuid.uuid4().hex[:10],
                    "content": item,
                    "source": "legacy",
                    "created_at": _now()
                })
        return result

    if isinstance(raw_data, dict):
        result = []
        # Handle legacy_notes list
        notes = raw_data.get("legacy_notes", [])
        for note in notes:
            if isinstance(note, str):
                result.append({
                    "id": uuid.uuid4().hex[:10],
                    "content": note,
                    "source": "legacy",
                    "created_at": _now()
                })
            elif isinstance(note, dict) and "content" in note:
                result.append(note)

        # Handle any other keys as structured profile data
        for key, value in raw_data.items():
            if key == "legacy_notes":
                continue
            if isinstance(value, (str, int, float, bool)):
                result.append({
                    "id": uuid.uuid4().hex[:10],
                    "content": f"{key}: {value}",
                    "source": "profile",
                    "created_at": _now()
                })
            elif isinstance(value, list):
                for v in value:
                    result.append({
                        "id": uuid.uuid4().hex[:10],
                        "content": f"{key}: {v}",
                        "source": "profile",
                        "created_at": _now()
                    })
            elif isinstance(value, dict):
                result.append({
                    "id": uuid.uuid4().hex[:10],
                    "content": f"{key}: {json.dumps(value)}",
                    "source": "profile",
                    "created_at": _now()
                })
        return result

    return []


def _load(user_id: str):
    """Load and normalize memories from DB. Returns (conn, list_of_mems)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM memory WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return conn, []
    try:
        raw = json.loads(row[0])
        return conn, _normalize(raw)
    except Exception:
        return conn, []


def _save(conn, user_id: str, mems: list):
    """Save normalized list back to DB."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO memory (user_id, data) VALUES (?, ?)",
            (user_id, json.dumps(mems))
        )


def get_memories(user_id: str) -> list:
    """Return all memories for a user, newest first."""
    conn, mems = _load(user_id)
    conn.close()
    return list(reversed(mems))


def add_memory(user_id: str, content: str, source: str = "manual") -> dict:
    """Add a memory. Returns the new memory object."""
    content = content.strip()[:500]
    if not content:
        return {}

    conn, mems = _load(user_id)

    mem = {
        "id": uuid.uuid4().hex[:10],
        "content": content,
        "source": source,
        "created_at": _now()
    }
    mems.append(mem)

    # Trim to max
    if len(mems) > MAX_PER_USER:
        mems = mems[-MAX_PER_USER:]

    # Migrate: overwrite whatever was in DB with clean list format
    _save(conn, user_id, mems)
    conn.close()
    return mem


def delete_memory(user_id: str, memory_id: str) -> bool:
    """Delete a memory by ID."""
    conn, mems = _load(user_id)
    new_mems = [m for m in mems if m.get("id") != memory_id]
    if len(new_mems) == len(mems):
        conn.close()
        return False
    _save(conn, user_id, new_mems)
    conn.close()
    return True


def update_profile(user_id: str, profile_json: str):
    """
    Updates the user's structured JSON profile by merging new data.
    New profile keys are converted to individual memory items.
    """
    try:
        new_data = json.loads(profile_json)
        conn, mems = _load(user_id)

        # Remove old profile entries so we don't duplicate
        mems = [m for m in mems if m.get("source") != "profile"]

        # Add new profile entries
        for key, value in new_data.items():
            if isinstance(value, (str, int, float, bool)):
                mems.append({
                    "id": uuid.uuid4().hex[:10],
                    "content": f"{key}: {value}",
                    "source": "profile",
                    "created_at": _now()
                })
            elif isinstance(value, list):
                for v in value:
                    mems.append({
                        "id": uuid.uuid4().hex[:10],
                        "content": f"{key}: {v}",
                        "source": "profile",
                        "created_at": _now()
                    })
            elif isinstance(value, dict):
                mems.append({
                    "id": uuid.uuid4().hex[:10],
                    "content": f"{key}: {json.dumps(value)}",
                    "source": "profile",
                    "created_at": _now()
                })

        _save(conn, user_id, mems)
        conn.close()
    except Exception as e:
        logger.error(f"Failed to update profile for {user_id}: {e}")


def format_for_prompt(user_id: str) -> str:
    """Format memories for injection into Claude's system prompt."""
    conn, mems = _load(user_id)
    conn.close()
    if not mems:
        return ""
    lines = "\n".join(f"• {m['content']}" for m in mems[-20:])
    return f"\n\n## What you remember about this user:\n{lines}"


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
            raw = json.loads(data)
            mems = _normalize(raw)
            if mems:
                lines = "\n".join(f"  • {m['content']}" for m in mems[-10:])
                res.append(f"[{name}'s Preferences]:\n{lines}")
        except Exception:
            pass

    if not res:
        return ""
    return "\n\n## SHARED TEAM MEMORY (Use these if asked to write in another employee's style):\n" + "\n\n".join(res)
