import os, requests, sys

sys.stdout.reconfigure(encoding='utf-8')
env_vars = {}
try:
    with open('config/.env', 'r') as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                env_vars[k] = v.strip('"\'')
except: pass

token = env_vars.get('NOTION_TOKEN', '')
db_id = env_vars.get('NOTION_TASKS_DB_ID', '')
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
                
        if "[Carousel]" in title:
            print("---")
            print("Task Title:", title)
            
            def get_text(prop):
                if not prop: return ""
                arr = prop.get("title") or prop.get("rich_text") or []
                if isinstance(arr, list):
                    return "".join(t.get("plain_text", "") for t in arr)
                return ""

            print("Customer Name (raw):", props.get("Customer Name"))
            print("Customer Name (text):", get_text(props.get("Customer Name")))
            print("Notes:", get_text(props.get("Notes")))
