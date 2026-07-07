import re

with open('backend/notion_store.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_multi = """def _get_multi_select(prop: dict) -> str:
    items = prop.get("multi_select") or []
    return ", ".join(item.get("name", "") for item in items)"""

new_multi = """def _get_multi_select(prop: dict) -> str:
    if "multi_select" in prop:
        items = prop.get("multi_select") or []
        return ", ".join(item.get("name", "") for item in items if item.get("name"))
    elif "people" in prop:
        items = prop.get("people") or []
        return ", ".join(item.get("name", "") for item in items if item.get("name"))
    return \"\""""

content = content.replace(old_multi, new_multi)

with open('backend/notion_store.py', 'w', encoding='utf-8') as f:
    f.write(content)
