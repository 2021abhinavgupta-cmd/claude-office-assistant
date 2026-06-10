import os, requests

env_vars = {}
try:
    with open('config/.env', 'r') as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                env_vars[k] = v.strip('"\'')
except Exception as e:
    print("No .env:", e)

token = env_vars.get('NOTION_TOKEN', '')
db_id = env_vars.get('NOTION_TASKS_DB_ID', '')

if not token:
    print("NO TOKEN")
    exit()

headers = {
    "Authorization": f"Bearer {token}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

r = requests.post(f"https://api.notion.com/v1/databases/{db_id}/query", headers=headers, json={"page_size": 100})
if r.status_code == 200:
    for p in r.json().get('results', []):
        props = p.get('properties', {})
        
        title = "Untitled"
        for k, v in props.items():
            if v.get('type') == 'title':
                arr = v.get('title', [])
                title = "".join(t.get('plain_text', '') for t in arr)
                break
                
        if "[Carousel]" in title or "[Reel]" in title:
            print("---")
            print("Task Title:", title)
            print("Customer Name:", props.get("Customer Name"))
            print("Client Name:", props.get("Client Name"))
            print("Client:", props.get("Client"))
            print("Content:", props.get("Content"))
            print("Brief:", props.get("Brief"))
            print("Description:", props.get("Description"))
            print("Notes:", props.get("Notes"))
