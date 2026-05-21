import sys
sys.path.append('backend')
import notion_store

print("Configured:", notion_store.is_configured())

if len(sys.argv) > 1:
    page_id = sys.argv[1]
    import requests
    
    headers = notion_store._headers()
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=headers,
        json={"archived": True},
        timeout=10,
    )
    print("Status code:", r.status_code)
    try:
        print("Response:", r.json())
    except:
        print("Text:", r.text)
