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

def get_text(prop):
    if not prop: return ""
    arr = prop.get("title") or prop.get("rich_text") or []
    if isinstance(arr, list):
        return "".join(t.get("plain_text", "") for t in arr)
    return ""

r = requests.post(f"https://api.notion.com/v1/databases/{db_id}/query", headers=headers, json={"page_size": 100})
if r.status_code == 200:
    for p in r.json().get('results', []):
        props = p.get('properties', {})
        
        title = "Untitled"
        for k, v in props.items():
            if v.get('type') == 'title':
                title = get_text(v)
                break
                
        if "[Carousel] Post 3" in title or "[Reel] TESTIMONIAL COMBINED" in title:
            print("---")
            print("Task Title:", title)
            client = get_text(props.get("Customer Name")).strip()
            
            desc = get_text(props.get("Notes", {}))
            brief = ""
            content = ""
            if desc and "|" in desc:
                parts = [pt.strip() for pt in desc.split("|")]
                for pt in parts:
                    pt_lower = pt.lower()
                    if pt_lower.startswith("brief:"): brief = pt[6:].strip()
                    elif pt_lower.startswith("content:"): content = pt[8:].strip()
            
            content = content.strip() or desc.strip() or brief.strip()
            
            if client and not title.startswith(client):
                title = f"{client} — {title}"
            if content:
                preview = content[:40] + "..." if len(content) > 40 else content
                title = f"{title} ({preview})"
                
            print("FINAL COMPUTED TITLE:", title)
