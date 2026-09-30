# scripts/test_leave_routes.py
"""Scratch test for routes/leave.py via a real Flask test client,
temp sqlite DB, never logs/app.db."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

import db  # noqa: E402
db.DB_PATH = tmp_path
db.init_db()

from flask import Flask  # noqa: E402
from routes.leave import leave_bp  # noqa: E402

app = Flask(__name__)
app.register_blueprint(leave_bp)
client = app.test_client()

# missing user_id -> 400/401 style rejection on balance
r = client.get("/api/leave/balance")
assert r.status_code == 400, r.status_code

# apply as a normal employee
r = client.post("/api/leave/apply", json={
    "user_id": "emp010", "date": "2026-10-05", "leave_type": "full", "reason": "sick",
})
assert r.status_code == 200, (r.status_code, r.get_json())
leave_id = r.get_json()["id"]

# non-HR cannot see pending list or approve
r = client.get("/api/leave/pending?user_id=emp010")
assert r.status_code == 403, r.status_code
r = client.post(f"/api/leave/{leave_id}/approve", json={"user_id": "emp010"})
assert r.status_code == 403, r.status_code

# HR (emp009) can see it and approve it
r = client.get("/api/leave/pending?user_id=emp009")
assert r.status_code == 200
assert any(p["id"] == leave_id for p in r.get_json()["pending"])
r = client.post(f"/api/leave/{leave_id}/approve", json={"user_id": "emp009"})
assert r.status_code == 200, r.get_json()

# balance now reflects it
r = client.get("/api/leave/balance?user_id=emp010")
d = r.get_json()
assert d["used"] == 1.0, d
assert d["remaining"] == 14.0, d

# calendar returns that date as leave_approved
r = client.get("/api/leave/calendar?user_id=emp010&year=2026&month=10")
d = r.get_json()
assert d["days"]["2026-10-05"]["status"] == "leave_approved", d["days"]["2026-10-05"]

# bad leave_type on apply -> 400
r = client.post("/api/leave/apply", json={
    "user_id": "emp010", "date": "2026-11-01", "leave_type": "quarter",
})
assert r.status_code == 400, r.status_code

os.remove(tmp_path)
print("OK: test_leave_routes")
