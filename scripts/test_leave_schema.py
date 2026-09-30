"""Scratch test: employee_leave gets its new columns, and the two new
ledger tables exist, after db.init_db() runs. Uses a temp sqlite file,
never touches logs/app.db."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["LUMINA_DB_PATH"] = tmp_path  # picked up by db.py below if it honours it

import db  # noqa: E402

# db.py's DB_PATH is normally a fixed constant -- override it directly so
# this test never touches the real logs/app.db regardless of env var support.
db.DB_PATH = tmp_path
db.init_db()

conn = db.get_connection()
cols = {r[1] for r in conn.execute("PRAGMA table_info(employee_leave)").fetchall()}
assert {"status", "leave_type", "approved_by", "approved_at"} <= cols, \
    f"employee_leave missing new columns, has: {cols}"

tables = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
assert "overtime_ledger" in tables, "overtime_ledger table not created"
assert "comp_off_ledger" in tables, "comp_off_ledger table not created"

ot_cols = {r[1] for r in conn.execute("PRAGMA table_info(overtime_ledger)").fetchall()}
assert {"user_id", "date", "worked_hours", "ot_hours", "converted_at"} <= ot_cols

co_cols = {r[1] for r in conn.execute("PRAGMA table_info(comp_off_ledger)").fetchall()}
assert {"user_id", "days", "source_ot_hours", "created_at"} <= co_cols

# Old-shape insert (no status/leave_type given) must still work and default
# to 'approved'/'full' -- existing WhatsApp-set rows must not become invalid.
conn.execute(
    "INSERT INTO employee_leave (user_id, start_date, end_date, reason, created_by) "
    "VALUES ('emp003','2026-01-01','2026-01-01','test','test')"
)
conn.commit()
row = conn.execute(
    "SELECT status, leave_type FROM employee_leave WHERE user_id='emp003'"
).fetchone()
assert row == ("approved", "full"), f"expected default ('approved','full'), got {row}"

conn.close()
os.remove(tmp_path)
print("OK: test_leave_schema")
