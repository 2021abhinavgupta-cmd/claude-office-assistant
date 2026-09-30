# scripts/test_leave_export.py
"""Scratch test: attendance_export_sheets() distinguishes an approved
leave day from a plain unexplained absence, and the Summary sheet carries
the formal leave balance columns. Reads the generated xlsx back with
openpyxl to check actual cell values, not just that the route 200s.

NOTE on the two fixture dates below: the per-employee sheet's date range
is ALWAYS [start, today_ist()] (attendance_export_sheets() caps `end` at
today by design, unmodified by this feature) -- so both fixture dates
must be real weekdays that fall on or before whatever "today" actually is
when this test runs, not hardcoded future dates. joined_date is
monkeypatched on emp003 (same bypass pattern as _verified_admin below) so
the per-employee sheet's start date reaches back far enough to include
them, without touching the real config/employees.json."""
import io
import json
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

import db  # noqa: E402
db.DB_PATH = tmp_path
db.init_db()

import leave_store  # noqa: E402
import routes.attendance as attn  # noqa: E402
from utils import today_ist  # noqa: E402
from flask import Flask  # noqa: E402

# This test relies on config/employees.json's CURRENT real roster: emp003
# (Abhinav) must be active. If that ever changes, update this test's id.
UID = "emp003"

today_d = date.fromisoformat(today_ist())
# Two real weekdays inside [start, today] -- walk backward from today to
# find two weekdays (Mon-Fri) at least a few days apart, so this test
# never breaks depending on what day of the week "today" happens to be.
weekdays = []
d = today_d - timedelta(days=1)
while len(weekdays) < 2:
    if d.weekday() < 5:
        weekdays.append(d)
    d -= timedelta(days=1)
approved_date, plain_date = weekdays[0].isoformat(), weekdays[1].isoformat()

# Push emp003's per-employee-sheet start date back far enough to cover
# both fixture dates, WITHOUT touching the real config/employees.json --
# same monkeypatch-for-this-test-only pattern as _verified_admin below.
config_path = os.path.join(os.path.dirname(__file__), "..", "config", "employees.json")
with open(config_path) as f:
    real_employees = json.load(f)
patched_employees = json.loads(json.dumps(real_employees))  # deep copy
for e in patched_employees["employees"]:
    if e["id"] == UID:
        e["joined_date"] = (weekdays[-1] - timedelta(days=3)).isoformat()
attn._load_employees = lambda: patched_employees

# One approved leave day (no checkin) + one plain unexplained-absence
# weekday (also no checkin, no leave row) -- both weekdays, both within
# the per-employee sheet's real [start, today] date range.
row = leave_store.apply_leave(UID, approved_date, approved_date, reason="approved test")
leave_store.approve_leave(row["id"], "emp009")
# plain_date has no attendance row and no leave row -> plain "Leave"

# Bypass the real session-cookie auth for this worker-logic test --
# _verified_admin() is a real security gate (routes/attendance.py) and is
# not being re-tested here; this test is about the workbook content.
attn._verified_admin = lambda: True

app = Flask(__name__)
app.register_blueprint(attn.attendance_bp)
client = app.test_client()

r = client.get("/api/attendance/export-sheets")
assert r.status_code == 200, r.status_code

from openpyxl import load_workbook  # noqa: E402
wb = load_workbook(io.BytesIO(r.data))

assert UID.lower() != "abhinav"  # sanity: UID is an id, not the sheet title
ws = wb["Abhinav"]  # per-employee sheet is titled by name, not id

# Find the two rows by date in column A and check column G (Day Type)
found_approved = found_plain = False
for r_idx in range(5, ws.max_row + 1):
    cell_a = ws.cell(row=r_idx, column=1).value
    if cell_a is None:
        continue
    # openpyxl reads a date-formatted cell back as datetime.datetime (not
    # datetime.date), so .isoformat() alone yields "2026-09-29T00:00:00" --
    # slice to the date portion so it compares equal to a plain YYYY-MM-DD.
    dstr = cell_a.isoformat()[:10] if hasattr(cell_a, "isoformat") else str(cell_a)
    if dstr == approved_date:
        assert ws.cell(row=r_idx, column=7).value == "Leave (Approved)", \
            ws.cell(row=r_idx, column=7).value
        found_approved = True
    if dstr == plain_date:
        assert ws.cell(row=r_idx, column=7).value == "Leave", \
            ws.cell(row=r_idx, column=7).value
        found_plain = True
assert found_approved, "approved-leave row not found in per-employee sheet"
assert found_plain, "plain-absence row not found in per-employee sheet"

# Summary sheet carries the formal leave balance columns
ws_sum = wb["Summary"]
headers = [ws_sum.cell(row=6, column=c).value for c in range(1, ws_sum.max_column + 1)]
assert "Leave Used (Formal)" in headers, headers
assert "Comp-Off Earned" in headers, headers
assert "Leave Remaining" in headers, headers

col_remaining = headers.index("Leave Remaining") + 1
# find Abhinav's row in the Summary sheet
for r_idx in range(7, ws_sum.max_row + 1):
    if ws_sum.cell(row=r_idx, column=1).value == "Abhinav":
        bal = leave_store.get_balance(UID)
        assert ws_sum.cell(row=r_idx, column=col_remaining).value == bal["remaining"], \
            (ws_sum.cell(row=r_idx, column=col_remaining).value, bal)
        break
else:
    raise AssertionError("Abhinav row not found in Summary sheet")

os.remove(tmp_path)
print("OK: test_leave_export")
