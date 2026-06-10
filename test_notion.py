import os, sys, json
with open('backend/.env') as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ[k] = v.strip('"\'')
sys.path.append(os.path.join(os.getcwd(), 'backend'))
import notion_store

tasks = notion_store.list_tasks(assigned_to='Happy')
print('Found tasks:', len(tasks))
for t in tasks:
    if '[Carousel]' in t.get('title', ''):
        print(json.dumps(t, indent=2))
