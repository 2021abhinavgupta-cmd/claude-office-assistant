"""
Project Store — Manages user projects with custom instructions and knowledge base.
Now uses SQLite via db.py for persistent storage across Railway deploys.
"""

import os
import json
import uuid
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from db import get_connection

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

logger = logging.getLogger(__name__)

def _now(): return datetime.utcnow().isoformat() + "Z"

def _migrate_projects_json_once():
    """Migrate legacy projects.json to SQLite if it exists."""
    log_dir = Path(__file__).parent.parent / "logs"
    json_file = log_dir / "projects.json"
    
    if not json_file.exists():
        return
        
    try:
        with open(json_file, 'r') as f:
            data = json.load(f)
            
        projects = data.get("projects", [])
        if not projects:
            os.rename(json_file, str(json_file) + ".bak")
            return
            
        conn = get_connection()
        with conn:
            for p in projects:
                conn.execute(
                    "INSERT OR IGNORE INTO projects (id, user_id, data) VALUES (?, ?, ?)",
                    (p["id"], p["user_id"], json.dumps(p))
                )
        conn.close()
        os.rename(json_file, str(json_file) + ".bak")
        logger.info(f"Migrated {len(projects)} projects from JSON to SQLite.")
    except Exception as e:
        logger.error(f"Failed to migrate projects.json: {e}")

# Run migration on import
_migrate_projects_json_once()

def get_projects(user_id: str) -> list:
    """Return all projects for a user."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM projects WHERE user_id=?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    
    return [json.loads(row[0]) for row in rows]

def get_project(project_id: str, user_id: str) -> dict:
    """Return a single project by ID for a user."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM projects WHERE id=? AND user_id=?", (project_id, user_id))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return json.loads(row[0])
    return None

def create_project(user_id: str, name: str, custom_instructions: str = "") -> dict:
    """Create a new project."""
    project_id = "proj_" + uuid.uuid4().hex[:12]
    project = {
        "id": project_id,
        "user_id": user_id,
        "name": name,
        "custom_instructions": custom_instructions,
        "knowledge_base": [],
        "created_at": _now()
    }
    
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO projects (id, user_id, data) VALUES (?, ?, ?)",
            (project_id, user_id, json.dumps(project))
        )
    conn.close()
    return project

def update_project(project_id: str, user_id: str, name: str = None, custom_instructions: str = None) -> dict:
    """Update project details."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM projects WHERE id=? AND user_id=?", (project_id, user_id))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return None
        
    project = json.loads(row[0])
    if name is not None:
        project["name"] = name
    if custom_instructions is not None:
        project["custom_instructions"] = custom_instructions
        
    with conn:
        conn.execute(
            "UPDATE projects SET data=? WHERE id=? AND user_id=?",
            (json.dumps(project), project_id, user_id)
        )
    conn.close()
    return project

def delete_project(project_id: str, user_id: str) -> bool:
    """Delete a project."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM projects WHERE id=? AND user_id=?", (project_id, user_id))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def add_knowledge_base_doc(project_id: str, user_id: str, filename: str, content: str) -> dict:
    """Add a document to the project knowledge base."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM projects WHERE id=? AND user_id=?", (project_id, user_id))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return None
        
    project = json.loads(row[0])
    doc = {
        "id": "kb_" + uuid.uuid4().hex[:10],
        "filename": filename,
        "content": content,
        "added_at": _now()
    }
    project.setdefault("knowledge_base", []).append(doc)
    
    with conn:
        conn.execute(
            "UPDATE projects SET data=? WHERE id=? AND user_id=?",
            (json.dumps(project), project_id, user_id)
        )
    conn.close()
    return doc

def delete_knowledge_base_doc(project_id: str, user_id: str, doc_id: str) -> bool:
    """Delete a document from the project knowledge base."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM projects WHERE id=? AND user_id=?", (project_id, user_id))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return False
        
    project = json.loads(row[0])
    docs = project.get("knowledge_base", [])
    new_docs = [d for d in docs if d.get("id") != doc_id]
    
    if len(new_docs) == len(docs):
        conn.close()
        return False
        
    project["knowledge_base"] = new_docs
    with conn:
        conn.execute(
            "UPDATE projects SET data=? WHERE id=? AND user_id=?",
            (json.dumps(project), project_id, user_id)
        )
    conn.close()
    return True

