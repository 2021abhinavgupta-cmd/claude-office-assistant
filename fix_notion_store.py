import re

with open('backend/notion_store.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_title = '"title":        _get_text(props.get("Task", {})) or _get_text(props.get("Post Title", {})),'
new_title = '"title":        _get_text(props.get("Task", {})) or _get_text(props.get("Post Title", {})) or _get_text(props.get("Post", {})),'
content = content.replace(old_title, new_title)

old_due = '"due_date":     _get_date(props.get("Due Date", {})) or _get_date(props.get("Post Day", {})),'
new_due = '"due_date":     _get_date(props.get("Due Date", {})) or _get_date(props.get("Post Day", {})),\n            "creation_date":  _get_date(props.get("Creation Date", {})),'
content = content.replace(old_due, new_due)

with open('backend/notion_store.py', 'w', encoding='utf-8') as f:
    f.write(content)
