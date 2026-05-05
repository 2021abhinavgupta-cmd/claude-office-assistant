"""
Project Store — Manages user projects with custom instructions and knowledge base.
"""

import os
import json
import uuid
import logging
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

LOG_DIR = Path(__file__).parent.parent / "logs"
PROJECTS_FILE = LOG_DIR / "projects.json"

logger = logging.getLogger(__name__)
_lock = threading.Lock()

def _now(): return datetime.utcnow().isoformat() + "Z"

def _load() -> dict:
    LOG_DIR.mkdir(exist_ok=True)
    if not PROJECTS_FILE.exists(): return {"projects": []}
    try:
        with open(PROJECTS_FILE) as f: return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"projects.json corrupt: {e}")
        return {"projects": []}

def _save(data: dict):
    LOG_DIR.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=LOG_DIR, suffix=".json.tmp")
    try:
        with os.fdopen(fd, 'w') as f: json.dump(data, f, indent=2)
        os.replace(tmp, PROJECTS_FILE)
    except Exception as e:
        logger.error(f"Failed to save projects: {e}")
        try: os.unlink(tmp)
        except OSError: pass

def get_projects(user_id: str) -> list:
    """Return all projects for a user."""
    with _lock:
        data = _load()
        return [p for p in data.get("projects", []) if p.get("user_id") == user_id]

def get_project(project_id: str, user_id: str) -> dict:
    """Return a single project by ID for a user."""
    with _lock:
        data = _load()
        for p in data.get("projects", []):
            if p.get("id") == project_id and p.get("user_id") == user_id:
                return p
    return None

def create_project(user_id: str, name: str, custom_instructions: str = "") -> dict:
    """Create a new project."""
    with _lock:
        data = _load()
        project = {
            "id": "proj_" + uuid.uuid4().hex[:12],
            "user_id": user_id,
            "name": name,
            "custom_instructions": custom_instructions,
            "knowledge_base": [],
            "created_at": _now()
        }
        data.setdefault("projects", []).append(project)
        _save(data)
        return project

def update_project(project_id: str, user_id: str, name: str = None, custom_instructions: str = None) -> dict:
    """Update project details."""
    with _lock:
        data = _load()
        for p in data.get("projects", []):
            if p.get("id") == project_id and p.get("user_id") == user_id:
                if name is not None:
                    p["name"] = name
                if custom_instructions is not None:
                    p["custom_instructions"] = custom_instructions
                _save(data)
                return p
    return None

def delete_project(project_id: str, user_id: str) -> bool:
    """Delete a project."""
    with _lock:
        data = _load()
        projects = data.get("projects", [])
        new_projects = [p for p in projects if not (p.get("id") == project_id and p.get("user_id") == user_id)]
        if len(new_projects) == len(projects):
            return False
        data["projects"] = new_projects
        _save(data)
        return True

def add_knowledge_base_doc(project_id: str, user_id: str, filename: str, content: str) -> dict:
    """Add a document to the project knowledge base."""
    with _lock:
        data = _load()
        for p in data.get("projects", []):
            if p.get("id") == project_id and p.get("user_id") == user_id:
                doc = {
                    "id": "kb_" + uuid.uuid4().hex[:10],
                    "filename": filename,
                    "content": content,
                    "added_at": _now()
                }
                p.setdefault("knowledge_base", []).append(doc)
                _save(data)
                return doc
    return None

def delete_knowledge_base_doc(project_id: str, user_id: str, doc_id: str) -> bool:
    """Delete a document from the project knowledge base."""
    with _lock:
        data = _load()
        for p in data.get("projects", []):
            if p.get("id") == project_id and p.get("user_id") == user_id:
                docs = p.get("knowledge_base", [])
                new_docs = [d for d in docs if d.get("id") != doc_id]
                if len(new_docs) == len(docs):
                    return False
                p["knowledge_base"] = new_docs
                _save(data)
                return True
    return False
