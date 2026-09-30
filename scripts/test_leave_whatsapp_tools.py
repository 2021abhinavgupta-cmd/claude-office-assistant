"""Scratch test: whatsapp_agent's set_leave/clear_leave/get_leave_balance
tool handlers, via _run_tool directly (no real Anthropic call needed --
this only exercises the tool-execution branch, not the model loop)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

import db  # noqa: E402
db.DB_PATH = tmp_path
db.init_db()

import whatsapp_agent as wa  # noqa: E402
import leave_store  # noqa: E402

identity = {"kind": "employee", "id": "emp010", "name": "Sid"}

out = wa._run_tool("set_leave", {"start_date": "2026-10-05", "end_date": "2026-10-05"},
                    identity)
assert "approv" in out.lower(), out  # must mention it's pending approval, not granted
assert "on leave" not in out.lower() or "not" in out.lower(), out

conn = db.get_connection()
row = conn.execute(
    "SELECT status FROM employee_leave WHERE user_id='emp010'"
).fetchone()
conn.close()
assert row == ("pending",), row

out2 = wa._run_tool("get_leave_balance", {}, identity)
assert "15" in out2, out2  # base pool visible somewhere in the reply

# clear_leave on the still-pending request
out3 = wa._run_tool("clear_leave", {}, identity)
assert "cleared" in out3.lower() or "removed" in out3.lower(), out3

os.remove(tmp_path)
print("OK: test_leave_whatsapp_tools")
