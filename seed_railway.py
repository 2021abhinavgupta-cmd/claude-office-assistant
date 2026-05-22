import urllib.request
import json
import sys

print("=== Railway Client Account Seeder ===")
print("This script will create the default client accounts on your live Railway app.")
print("It uses the secure HTTP API we built.")
print("")

base_url = input("Enter your Railway App URL (e.g., https://claude-office-assistant-xxxx.up.railway.app): ").strip()
if not base_url:
    print("Error: URL cannot be empty.")
    sys.exit(1)

# Ensure no trailing slash
if base_url.endswith("/"):
    base_url = base_url[:-1]

clients = [
    {"username": "topgreen",  "password": "password123", "client_name": "TopGreen",  "client_notion_id": ""},
    {"username": "metazune",  "password": "password123", "client_name": "METAZUNE",  "client_notion_id": ""},
    {"username": "evault",    "password": "password123", "client_name": "Evault",    "client_notion_id": ""},
]

print(f"\nSeeding clients to: {base_url}\n")

for client in clients:
    payload = json.dumps(client).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/auth/clients",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            print(f"[SUCCESS] Created: @{client['username']} for {client['client_name']}")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        if "already exists" in body.get("error", "").lower() or e.code == 409:
            print(f"[SKIPPED] @{client['username']} already exists.")
        else:
            print(f"[ERROR] Failed for @{client['username']}: {body}")
    except Exception as e:
        print(f"[NETWORK ERROR] Failed to connect for @{client['username']}: {e}")

print("\nDone! You should now be able to log into your Railway deployment.")
