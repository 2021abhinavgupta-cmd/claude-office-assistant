"""
Project Store — Manages user projects with custom instructions, memory, and knowledge base.
Uses SQLite for persistent storage.
"""

import os
import uuid
import logging
from datetime import datetime
from db import get_connection

logger = logging.getLogger(__name__)

def _now(): return datetime.utcnow().isoformat() + "Z"

def get_projects(user_id: str) -> list:
    """Return all projects for a user."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, description, instructions, memory, created_at FROM projects WHERE user_id=? ORDER BY created_at DESC", 
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    
    projects = []
    for r in rows:
        projects.append({
            "id": r[0],
            "name": r[1],
            "description": r[2],
            "instructions": r[3] or "",
            "memory": r[4] or "",
            "created_at": r[5]
        })
    return projects

def get_project(project_id: str) -> dict:
    """Return a single project by ID with its files."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, name, description, instructions, memory, created_at FROM projects WHERE id=?", 
        (project_id,)
    )
    p_row = cursor.fetchone()
    
    if not p_row:
        conn.close()
        return None
        
    project = {
        "id": p_row[0],
        "user_id": p_row[1],
        "name": p_row[2],
        "description": p_row[3],
        "instructions": p_row[4] or "",
        "memory": p_row[5] or "",
        "created_at": p_row[6],
        "knowledge_base": []
    }
    
    cursor.execute(
        "SELECT id, filename, content, added_at FROM project_files WHERE project_id=? ORDER BY added_at ASC", 
        (project_id,)
    )
    f_rows = cursor.fetchall()
    conn.close()
    
    for f in f_rows:
        project["knowledge_base"].append({
            "id": f[0],
            "filename": f[1],
            "content": f[2],
            "added_at": f[3]
        })
        
    return project

def create_project(user_id: str, name: str, description: str = "") -> dict:
    """Create a new project."""
    project_id = "proj_" + uuid.uuid4().hex[:12]
    
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO projects (id, user_id, name, description, instructions, memory, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, user_id, name, description, "", "", _now())
        )
    conn.close()
    
    return get_project(project_id)

def update_project_instructions(project_id: str, instructions: str) -> bool:
    """Update project instructions."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE projects SET instructions=? WHERE id=?", (instructions, project_id))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated

def update_project_memory(project_id: str, memory: str) -> bool:
    """Update project memory."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE projects SET memory=? WHERE id=?", (memory, project_id))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated

def add_knowledge_base_doc(project_id: str, filename: str, content: str) -> dict:
    """Add a document to the project knowledge base."""
    file_id = "kb_" + uuid.uuid4().hex[:10]
    added_at = _now()
    
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO project_files (id, project_id, filename, content, added_at) VALUES (?, ?, ?, ?, ?)",
            (file_id, project_id, filename, content, added_at)
        )
    conn.close()
    
    return {
        "id": file_id,
        "filename": filename,
        "content": content,
        "added_at": added_at
    }

def delete_knowledge_base_doc(project_id: str, doc_id: str) -> bool:
    """Delete a document from the project knowledge base."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM project_files WHERE id=? AND project_id=?", (doc_id, project_id))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted
