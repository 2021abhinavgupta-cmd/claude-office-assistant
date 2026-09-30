"""Regression test: a PENDING leave request must NOT exempt someone from
the standup lock (routes/ops.py::standup_lock_status) or the login-nudge
suppression (routes/companion.py::companion_not_logged_in) -- both read
leave_store.is_on_leave/on_leave_ids directly. Only an APPROVED leave may
exempt either. This is the one deliberate behavior change to gotcha #119's
original WhatsApp-instant-leave feature."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

import db  # noqa: E402
db.DB_PATH = tmp_path
db.init_db()

import leave_store  # noqa: E402

row = leave_store.apply_leave("emp010", "2026-10-05", "2026-10-05")

# PENDING: must not exempt
assert leave_store.is_on_leave("emp010", "2026-10-05") is False, \
    "a PENDING leave request must not count as on_leave"
assert "emp010" not in leave_store.on_leave_ids("2026-10-05"), \
    "a PENDING leave request must not appear in on_leave_ids"

# APPROVED: must exempt
leave_store.approve_leave(row["id"], "emp009")
assert leave_store.is_on_leave("emp010", "2026-10-05") is True, \
    "an APPROVED leave request must count as on_leave"
assert "emp010" in leave_store.on_leave_ids("2026-10-05"), \
    "an APPROVED leave request must appear in on_leave_ids"

os.remove(tmp_path)
print("OK: test_leave_lock_regression")
