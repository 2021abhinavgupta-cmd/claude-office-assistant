"""
Conversation Store — Manages multi-turn chat history for all employees.
Each conversation maintains full Claude message history for context.
Stored in logs/conversations.json
"""

import os
import json
import uuid
import logging
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

LOG_DIR   = Path(__file__).parent.parent / "logs"
CONV_FILE = LOG_DIR / "conversations.json"

MAX_CONTEXT_MESSAGES = 40  # messages sent to Claude (≈ 80 exchanges before oldest drops off)
MAX_TITLE_WORDS      = 7   # words used to auto-title from first message

logger = logging.getLogger(__name__)
_lock  = threading.Lock()


# ── Internal helpers ─────────────────────────────────────────────────────────

def _load() -> dict:
    LOG_DIR.mkdir(exist_ok=True)
    if not CONV_FILE.exists():
        return {}
    try:
        with open(CONV_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"conversations.json corrupt ({e}) — resetting to empty")
        return {}


def _save(data: dict):
    LOG_DIR.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=LOG_DIR, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, CONV_FILE)
    except Exception as e:
        logger.error(f"Failed to save conversations: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


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
    """Return a copy of conv without the messages list (for list views)."""
    return {k: v for k, v in conv.items() if k != "messages"}


# ── Public API ───────────────────────────────────────────────────────────────

def create_conversation(user_id: str, user_name: str,
                        task_type: Optional[str] = None) -> dict:
    """Create a new conversation and return its full dict."""
    with _lock:
        data    = _load()
        conv_id = "conv_" + uuid.uuid4().hex[:12]
        conv    = {
            "id":         conv_id,
            "user_id":    user_id,
            "user_name":  user_name,
            "title":      "New conversation",
            "task_type":  task_type,
            "created_at": _now(),
            "updated_at": _now(),
            "messages":   [],
        }
        data[conv_id] = conv
        _save(data)
        return conv


def get_conversation(conv_id: str) -> Optional[dict]:
    """Return a conversation (with messages) or None."""
    with _lock:
        return _load().get(conv_id)


def list_conversations(user_id: str) -> list:
    """Return all conversations for a user (no messages), newest first."""
    with _lock:
        data  = _load()
        convs = [
            _strip_messages(c)
            for c in data.values()
            if c.get("user_id") == user_id
        ]
        convs.sort(key=lambda c: c["updated_at"], reverse=True)
        return convs


def add_message(conv_id: str, role: str, content: str,
                metadata: Optional[dict] = None) -> Optional[dict]:
    """Append a message; auto-title on first user message. Returns message dict."""
    with _lock:
        data = _load()
        conv = data.get(conv_id)
        if not conv:
            return None

        msg = {
            "role":      role,
            "content":   content,
            "timestamp": _now(),
            **(metadata or {}),
        }
        conv["messages"].append(msg)
        conv["updated_at"] = _now()

        # Auto-title from first user message
        if role == "user" and conv["title"] == "New conversation":
            conv["title"] = _auto_title(content)

        _save(data)
        return msg


def update_title(conv_id: str, title: str):
    """Manually rename a conversation."""
    with _lock:
        data = _load()
        if conv_id in data:
            data[conv_id]["title"] = title.strip()[:60]
            _save(data)


def update_task_type(conv_id: str, task_type: str):
    """Set/update the task type for a conversation."""
    with _lock:
        data = _load()
        if conv_id in data:
            data[conv_id]["task_type"] = task_type
            _save(data)


def delete_conversation(conv_id: str) -> bool:
    """Delete a conversation. Returns True if it existed."""
    with _lock:
        data = _load()
        if conv_id in data:
            del data[conv_id]
            _save(data)
            return True
        return False


def get_context_messages(conv_id: str,
                         max_messages: int = MAX_CONTEXT_MESSAGES) -> list:
    """
    Return the last N messages formatted for the Claude API.
    Only includes role + content (no metadata).
    """
    with _lock:
        data = _load()
        conv = data.get(conv_id)
        if not conv:
            return []
        recent = conv["messages"][-max_messages:]
        return [{"role": m["role"], "content": m["content"]} for m in recent]


def list_all_users() -> list:
    """Return unique users with conversation counts (for admin view)."""
    with _lock:
        data  = _load()
        users = {}
        for conv in data.values():
            uid = conv["user_id"]
            if uid not in users:
                users[uid] = {
                    "user_id":    uid,
                    "user_name":  conv["user_name"],
                    "conv_count": 0,
                }
            users[uid]["conv_count"] += 1
        return list(users.values())
