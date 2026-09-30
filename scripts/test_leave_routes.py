# scripts/test_leave_routes.py
"""Scratch test for routes/leave.py via a real Flask test client,
temp sqlite DB, never logs/app.db.

Identity for every route in this file comes from the real session_token
cookie (routes.leave._session_user_id(), which calls
routes.auth._verify_session()) -- never a client-supplied user_id param.
This test bypasses the real cookie mechanics the same way
scripts/test_leave_export.py bypasses routes/attendance.py's
_verified_admin(): monkeypatch the module-level auth function itself, and
flip what it returns per test case. That is testing "does the route
correctly gate on whatever _session_user_id() says", which is the actual
unit under test here -- the cookie-parsing/session-lookup plumbing itself
belongs to routes/auth.py and is already covered elsewhere."""
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
import routes.leave as leave_routes  # noqa: E402
from routes.leave import leave_bp  # noqa: E402

app = Flask(__name__)
app.register_blueprint(leave_bp)
client = app.test_client()

# Controllable stand-in for "whoever the session_token cookie resolves
# to". "" means no valid session (matches a missing/expired cookie).
_session = {"uid": ""}
leave_routes._session_user_id = lambda: _session["uid"]


def as_user(uid):
    _session["uid"] = uid


# ── no session at all -> 401 on every route, self-service AND HR ───────
as_user("")
r = client.get("/api/leave/balance")
assert r.status_code == 401, (r.status_code, r.get_json())
r = client.get("/api/leave/calendar?year=2026&month=10")
assert r.status_code == 401, r.status_code
r = client.post("/api/leave/apply", json={"date": "2026-10-05"})
assert r.status_code == 401, r.status_code
r = client.post("/api/leave/1/cancel")
assert r.status_code == 401, r.status_code
r = client.get("/api/leave/pending")
assert r.status_code == 401, r.status_code
r = client.post("/api/leave/1/approve")
assert r.status_code == 401, r.status_code
r = client.post("/api/leave/1/reject")
assert r.status_code == 401, r.status_code

# ── a real session as a normal (non-HR) employee: self-service works ───
as_user("emp010")
r = client.post("/api/leave/apply", json={
    "date": "2026-10-05", "leave_type": "full", "reason": "sick",
})
assert r.status_code == 200, (r.status_code, r.get_json())
leave_id = r.get_json()["id"]
assert r.get_json()["user_id"] == "emp010"

r = client.get("/api/leave/balance")
assert r.status_code == 200, r.status_code

r = client.get("/api/leave/calendar?year=2026&month=10")
assert r.status_code == 200, r.status_code

# A body-supplied user_id must be completely ignored -- the row above was
# filed as emp010 (the session holder), not whatever a caller might claim.
r = client.post("/api/leave/apply", json={
    "date": "2026-10-09", "user_id": "emp003",
})
assert r.status_code == 200, (r.status_code, r.get_json())
assert r.get_json()["user_id"] == "emp010", r.get_json()

# ── non-HR session cannot see pending list, approve, or reject -> 403 ──
r = client.get("/api/leave/pending")
assert r.status_code == 403, r.status_code
r = client.post(f"/api/leave/{leave_id}/approve")
assert r.status_code == 403, r.status_code
r = client.post(f"/api/leave/{leave_id}/reject")
assert r.status_code == 403, r.status_code
# ...even if the body claims to be HR -- the session, not the body, decides.
r = client.post(f"/api/leave/{leave_id}/approve", json={"user_id": "emp009"})
assert r.status_code == 403, (r.status_code, r.get_json())

# ── HR's OWN session (emp009) can see it and approve it ────────────────
as_user("emp009")
r = client.get("/api/leave/pending")
assert r.status_code == 200
assert any(p["id"] == leave_id for p in r.get_json()["pending"])
r = client.post(f"/api/leave/{leave_id}/approve")
assert r.status_code == 200, r.get_json()

# balance now reflects it -- checked as the employee whose leave it is
as_user("emp010")
r = client.get("/api/leave/balance")
d = r.get_json()
assert d["used"] == 1.0, d
assert d["remaining"] == 14.0, d

# calendar returns that date as leave_approved
r = client.get("/api/leave/calendar?year=2026&month=10")
d = r.get_json()
assert d["days"]["2026-10-05"]["status"] == "leave_approved", d["days"]["2026-10-05"]

# bad leave_type on apply -> 400
r = client.post("/api/leave/apply", json={
    "date": "2026-11-01", "leave_type": "quarter",
})
assert r.status_code == 400, r.status_code

# M6: out-of-range year on calendar -> 400, not a 500 from an unhandled
# ValueError constructing an out-of-range date.
r = client.get("/api/leave/calendar?year=99999&month=1")
assert r.status_code == 400, (r.status_code, r.data)
r = client.get("/api/leave/calendar?year=1500&month=1")
assert r.status_code == 400, r.status_code

os.remove(tmp_path)
print("OK: test_leave_routes")
