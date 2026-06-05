import uuid
import json
import logging
from db import get_connection

logger = logging.getLogger(__name__)

def create_skill(user_id: str, name: str, model: str, prompt: str, is_shared: bool = False):
    skill_id = "sk_" + uuid.uuid4().hex[:10]
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO custom_skills (id, user_id, name, model, prompt, is_shared) VALUES (?, ?, ?, ?, ?, ?)",
                (skill_id, user_id, name, model, prompt, 1 if is_shared else 0)
            )
        return skill_id
    except Exception as e:
        logger.error(f"Error creating custom skill: {e}")
        return None

def get_skills_for_user(user_id: str):
    try:
        conn = get_connection()
        conn.row_factory = dict_factory
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, user_id, name, model, task_type, prompt, is_shared, created_at FROM custom_skills WHERE user_id = ? OR is_shared = 1",
            (user_id,)
        )
        return cursor.fetchall()
    except Exception as e:
        logger.error(f"Error fetching skills for user {user_id}: {e}")
        return []

def delete_skill(skill_id: str, user_id: str):
    try:
        conn = get_connection()
        with conn:
            cursor = conn.execute(
                "DELETE FROM custom_skills WHERE id = ? AND user_id = ?",
                (skill_id, user_id)
            )
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Error deleting custom skill {skill_id}: {e}")
        return False

def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d
