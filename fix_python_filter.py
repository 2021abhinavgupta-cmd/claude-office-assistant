import re

with open('backend/notion_store.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_return = """    return tasks"""

new_return = """    # If assigned_to was requested, we filter in Python because Notion API fails
    # when attempting to filter a 'people' property with a 'multi_select' condition.
    if assigned_to:
        filtered_tasks = []
        for t in tasks:
            n_assignees = t.get("assigned_to", "")
            if assigned_to.lower() in n_assignees.lower():
                filtered_tasks.append(t)
        return filtered_tasks
    return tasks"""

content = content.replace(old_return, new_return)

with open('backend/notion_store.py', 'w', encoding='utf-8') as f:
    f.write(content)
