"""Confirms task_scheduler exposes _run_overtime_conversion and that it
doesn't raise when leave_store's real sweep function runs against a temp
DB with no attendance data (the common case: nothing to convert)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

fd, tmp_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

import db  # noqa: E402
db.DB_PATH = tmp_path
db.init_db()

import task_scheduler  # noqa: E402

assert hasattr(task_scheduler, "_run_overtime_conversion"), \
    "task_scheduler._run_overtime_conversion not defined"

task_scheduler._run_overtime_conversion()  # must not raise on an empty DB

os.remove(tmp_path)
print("OK: test_overtime_job")
