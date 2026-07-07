with open('backend/routes/ops.py', 'r', encoding='utf-8') as f:
    content = f.read()

debug_endpoint = """
@ops_bp.route("/debug-notion", methods=["GET"])
def debug_notion():
    from backend.notion_store import list_tasks
    import json
    return json.dumps(list_tasks(), indent=2)
"""

if "def debug_notion()" not in content:
    content = content.replace('ops_bp = Blueprint("ops_bp", __name__)', 'ops_bp = Blueprint("ops_bp", __name__)\n' + debug_endpoint)
    with open('backend/routes/ops.py', 'w', encoding='utf-8') as f:
        f.write(content)
