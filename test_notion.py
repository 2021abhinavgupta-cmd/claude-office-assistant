import sys, json
sys.stdout.reconfigure(encoding='utf-8')
from backend.notion_store import list_tasks

tasks = list_tasks()
print(f'Total tasks: {len(tasks)}')
for t in tasks:
    if t.get('assigned_to') == 'Abhinav':
        print(t.get('title'), '||', t.get('status'), '||', t.get('due_date'))
