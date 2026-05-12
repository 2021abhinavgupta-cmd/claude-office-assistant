import uuid, json
from db import get_connection

def create_skill(user_id, name, emoji, model, prompt, is_shared=False):
    skill_id = "sk_" + uuid.uuid4().hex[:10]
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO custom_skills (id,user_id,name,emoji,model,task_type,prompt,is_shared) VALUES (?,?,?,?,?,?,?,?)",
            (skill_id, user_id, name, emoji, model, "general", prompt, 1 if is_shared else 0)
        )
    conn.close()
    return skill_id

def get_skills_for_user(user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id,user_id,name,emoji,model,prompt,is_shared FROM custom_skills WHERE user_id=? OR is_shared=1",
        (user_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return [{"id":r[0],"user_id":r[1],"name":r[2],"emoji":r[3],
             "model":r[4],"prompt":r[5],"is_shared":bool(r[6])} for r in rows]

def delete_skill(skill_id, user_id):
    conn = get_connection()
    with conn:
        conn.execute(
            "DELETE FROM custom_skills WHERE id=? AND user_id=?",
            (skill_id, user_id)
        )
    conn.close()
