import re

with open('backend/notion_store.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_filter_logic = """    filters = []
    if assigned_to:
        filters.append({"property": "Assigned To", "multi_select": {"contains": assigned_to}})
    if client_notion_id:
        filters.append({"property": "Client ID", "rich_text": {"equals": client_notion_id}})
    if status_filter:
        if status_filter == "EMPTY":
            filters.append({"property": "Status", "select": {"is_empty": True}})
        else:
            filters.append({"property": "Status", "select": {"equals": status_filter}})

    payload: dict = {
        "page_size": 200,
        "sorts": [{"timestamp": "created_time", "direction": "descending"}]
    }
    if len(filters) == 1:
        payload["filter"] = filters[0]
    elif len(filters) > 1:
        payload["filter"] = {"and": filters}"""

new_filter_logic = """    filters = []
    if client_notion_id:
        filters.append({"property": "Client ID", "rich_text": {"equals": client_notion_id}})
    if status_filter:
        if status_filter == "EMPTY":
            filters.append({"property": "Status", "select": {"is_empty": True}})
        else:
            filters.append({"property": "Status", "select": {"equals": status_filter}})

    payload: dict = {
        "page_size": 200,
        "sorts": [{"timestamp": "created_time", "direction": "descending"}]
    }
    if len(filters) == 1:
        payload["filter"] = filters[0]
    elif len(filters) > 1:
        payload["filter"] = {"and": filters}"""

content = content.replace(old_filter_logic, new_filter_logic)

with open('backend/notion_store.py', 'w', encoding='utf-8') as f:
    f.write(content)
