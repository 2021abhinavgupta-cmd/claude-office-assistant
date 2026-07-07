import re

with open('backend/notion_store.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_date = """def _get_date(prop: dict) -> str:
    d = prop.get("date") or {}
    return d.get("start", "")"""

new_date = """def _get_date(prop: dict) -> str:
    if "date" in prop:
        d = prop.get("date") or {}
        return d.get("start", "")
    elif "created_time" in prop:
        return prop.get("created_time", "")
    elif "last_edited_time" in prop:
        return prop.get("last_edited_time", "")
    return \"\""""

content = content.replace(old_date, new_date)

with open('backend/notion_store.py', 'w', encoding='utf-8') as f:
    f.write(content)
