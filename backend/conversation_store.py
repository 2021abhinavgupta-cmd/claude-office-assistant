"""
Conversation Store — Manages multi-turn chat history for all employees.
Stored in SQLite via db.py
"""

import json
import uuid
import logging
from datetime import datetime
from typing import Optional

from db import get_connection

MAX_CONTEXT_MESSAGES = 40
MAX_TITLE_WORDS      = 7
MAX_CONTEXT_CHARS    = 24_000  # rough budget for message contents (excludes system prompt)

logger = logging.getLogger(__name__)

def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"

def _auto_title(message: str) -> str:
    """First MAX_TITLE_WORDS words of message, capped at 55 chars."""
    words = message.strip().split()
    title = " ".join(words[:MAX_TITLE_WORDS])
    if len(words) > MAX_TITLE_WORDS:
        title += "…"
    return title[:55] or "New conversation"

def _strip_messages(conv: dict) -> dict:
    return {k: v for k, v in conv.items() if k != "messages"}

# ── Public API ───────────────────────────────────────────────────────────────

def create_conversation(user_id: str, user_name: str,
                        task_type: Optional[str] = None,
                        project_id: Optional[str] = None) -> dict:
    conv_id = "conv_" + uuid.uuid4().hex[:12]
    conv = {
        "id":         conv_id,
        "user_id":    user_id,
        "user_name":  user_name,
        "title":      "New conversation",
        "task_type":  task_type,
        "project_id": project_id,
        "created_at": _now(),
        "updated_at": _now(),
        "messages":   [],
    }
    conn = get_connection()
    with conn:
        conn.execute("INSERT INTO conversations (id, data) VALUES (?, ?)", (conv_id, json.dumps(conv)))
    conn.close()
    return conv

def get_conversation(conv_id: str) -> Optional[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM conversations WHERE id=?", (conv_id,))
    row = cursor.fetchone()
    conn.close()
    if row: return json.loads(row[0])
    return None

def list_conversations(user_id: str) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT data FROM conversations WHERE json_extract(data, '$.user_id') = ?",
        (user_id,)
    )
    convs = []
    for (row,) in cursor.fetchall():
        c = json.loads(row)
        convs.append(_strip_messages(c))
    conn.close()
    convs.sort(key=lambda c: c["updated_at"], reverse=True)
    return convs

def add_message(conv_id: str, role: str, content: str,
                metadata: Optional[dict] = None) -> Optional[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM conversations WHERE id=?", (conv_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None

    conv = json.loads(row[0])
    msg = {
        "role":      role,
        "content":   content,
        "timestamp": _now(),
        **(metadata or {}),
    }
    conv["messages"].append(msg)
    conv["updated_at"] = _now()

    if role == "user" and conv["title"] == "New conversation":
        conv["title"] = _auto_title(content)

    with conn:
        conn.execute("UPDATE conversations SET data=? WHERE id=?", (json.dumps(conv), conv_id))
    conn.close()
    return msg

def update_title(conv_id: str, title: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM conversations WHERE id=?", (conv_id,))
    row = cursor.fetchone()
    if row:
        conv = json.loads(row[0])
        conv["title"] = title.strip()[:60]
        with conn:
            conn.execute("UPDATE conversations SET data=? WHERE id=?", (json.dumps(conv), conv_id))
    conn.close()

def truncate_messages(conv_id: str, index: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM conversations WHERE id=?", (conv_id,))
    row = cursor.fetchone()
    if row:
        conv = json.loads(row[0])
        conv["messages"] = conv["messages"][:index]
        conv["updated_at"] = _now()
        with conn:
            conn.execute("UPDATE conversations SET data=? WHERE id=?", (json.dumps(conv), conv_id))
    conn.close()


def amend_last_user_content(conv_id: str, new_content: str) -> bool:
    """Replace the text of the most recent user message (used for regenerate-with-note)."""
    if not (new_content or "").strip():
        return False
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM conversations WHERE id=?", (conv_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    conv = json.loads(row[0])
    msgs = conv.get("messages") or []
    if not msgs or msgs[-1].get("role") != "user":
        conn.close()
        return False
    msgs[-1]["content"] = new_content.strip()
    conv["updated_at"] = _now()
    with conn:
        conn.execute("UPDATE conversations SET data=? WHERE id=?", (json.dumps(conv), conv_id))
    conn.close()
    return True


def update_task_type(conv_id: str, task_type: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM conversations WHERE id=?", (conv_id,))
    row = cursor.fetchone()
    if row:
        conv = json.loads(row[0])
        conv["task_type"] = task_type
        with conn:
            conn.execute("UPDATE conversations SET data=? WHERE id=?", (json.dumps(conv), conv_id))
    conn.close()

def delete_conversation(conv_id: str) -> bool:
    conn = get_connection()
    with conn:
        cursor = conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        deleted = cursor.rowcount > 0
    conn.close()
    return deleted

def get_context_messages(conv_id: str, limit: int = MAX_CONTEXT_MESSAGES) -> list:
    conv = get_conversation(conv_id)
    if not conv: return []
    messages = []
    for m in conv.get("messages", []):
        messages.append({"role": m["role"], "content": m["content"]})
        
    messages = messages[-limit:]

    # Rough character budget: keep newest messages until budget is reached.
    total = 0
    kept = []
    for m in reversed(messages):
        t = m.get("content", "")
        if isinstance(t, list):
            t = " ".join([b.get("text", "") for b in t if b.get("type") == "text"])
        t = str(t or "")
        if total + len(t) > MAX_CONTEXT_CHARS and kept:
            continue
        kept.append(m)
        total += len(t)
        if total >= MAX_CONTEXT_CHARS:
            break
    return list(reversed(kept))
