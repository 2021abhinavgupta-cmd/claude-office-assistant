import json
import urllib.request
import urllib.error

data = {
    "posts": [
        {
            "postDay": "2024-05-10",
            "creationDate": "",
            "post": "Test post",
            "type": "Story",
            "content": "",
            "idea": "",
            "scripts": "",
            "caption": "",
            "assignee": "",
            "status": "Scheduled",
            "link": ""
        }
    ]
}

req = urllib.request.Request(
    'http://127.0.0.1:5000/api/social-media/auto-fill',
    data=json.dumps(data).encode('utf-8'),
    headers={'Content-Type': 'application/json'}
)

try:
    with urllib.request.urlopen(req) as response:
        print("Success:")
        print(response.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print(f"HTTP Error {e.code}:")
    print(e.read().decode('utf-8'))
except Exception as e:
    print(f"Error: {e}")
