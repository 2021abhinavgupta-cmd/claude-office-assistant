"""
User Memory Store — Cross-conversation persistent memory per employee.
Memories are injected into every Claude system prompt automatically.
"""
import os, json, uuid, logging, tempfile, threading
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

LOG_DIR  = Path(__file__).parent.parent / "logs"
MEM_FILE = LOG_DIR / "memories.json"
MAX_PER_USER = 50

logger = logging.getLogger(__name__)
_lock  = threading.Lock()

def _now(): return datetime.utcnow().isoformat() + "Z"

def _load() -> dict:
    LOG_DIR.mkdir(exist_ok=True)
    if not MEM_FILE.exists(): return {}
    try:
        with open(MEM_FILE) as f: return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"memories.json corrupt: {e}"); return {}

def _save(data: dict):
    LOG_DIR.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=LOG_DIR, suffix=".json.tmp")
    try:
        with os.fdopen(fd, 'w') as f: json.dump(data, f, indent=2)
        os.replace(tmp, MEM_FILE)
    except Exception as e:
        logger.error(f"Failed to save memories: {e}")
        try: os.unlink(tmp)
        except OSError: pass

def get_memories(user_id: str) -> list:
    """Return all memories for a user, newest first."""
    with _lock:
        return list(reversed(_load().get(user_id, [])))

def add_memory(user_id: str, content: str, source: str = "manual") -> dict:
    """Add a memory. Returns the new memory dict."""
    content = content.strip()[:500]
    if not content: return {}
    with _lock:
        data = _load()
        data.setdefault(user_id, [])
        mem = {"id": uuid.uuid4().hex[:10], "content": content,
               "source": source, "created_at": _now()}
        data[user_id].append(mem)
        if len(data[user_id]) > MAX_PER_USER:
            data[user_id] = data[user_id][-MAX_PER_USER:]
        _save(data)
        return mem

def delete_memory(user_id: str, memory_id: str) -> bool:
    """Delete a memory by ID."""
    with _lock:
        data  = _load()
        mems  = data.get(user_id, [])
        new   = [m for m in mems if m["id"] != memory_id]
        if len(new) == len(mems): return False
        data[user_id] = new
        _save(data)
        return True

def format_for_prompt(user_id: str) -> str:
    """Format memories for injection into Claude's system prompt."""
    mems = get_memories(user_id)
    if not mems: return ""
    lines = "\n".join(f"• {m['content']}" for m in mems[-20:])
    return f"\n\n## What you remember about this user:\n{lines}"
