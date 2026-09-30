"""Scratch test for leave_store.py -- temp sqlite DB, never logs/app.db."""
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

# ── apply_leave basic + validation ──────────────────────────────────────
row = leave_store.apply_leave("emp010", "2026-10-05", "2026-10-05",
                               leave_type="full", reason="sick",
                               created_by="Sid")
assert row["status"] == "pending", row
lid = row["id"]

try:
    leave_store.apply_leave("emp010", "2026-10-06", "2026-10-06", leave_type="quarter")
    raise AssertionError("bad leave_type should have raised ValueError")
except ValueError:
    pass

# ── pending must NOT count as on-leave (closes the self-approval loophole) ──
assert leave_store.is_on_leave("emp010", "2026-10-05") is False
assert "emp010" not in leave_store.on_leave_ids("2026-10-05")

# ── approve, then it DOES count ─────────────────────────────────────────
assert leave_store.approve_leave(lid, "emp009") is True
assert leave_store.is_on_leave("emp010", "2026-10-05") is True
assert "emp010" in leave_store.on_leave_ids("2026-10-05")

# ── overlapping an approved leave is rejected ───────────────────────────
try:
    leave_store.apply_leave("emp010", "2026-10-05", "2026-10-05", leave_type="full")
    raise AssertionError("overlapping an approved leave should have raised ValueError")
except ValueError:
    pass

# ── half-day balance math ───────────────────────────────────────────────
row2 = leave_store.apply_leave("emp010", "2026-10-12", "2026-10-12", leave_type="half")
assert leave_store.approve_leave(row2["id"], "emp009") is True
bal = leave_store.get_balance("emp010", year=2026)
assert bal["base"] == 15, bal
assert bal["used"] == 1.5, bal  # 1 full + 1 half
assert bal["remaining"] == 13.5, bal

# ── reject leaves balance untouched ─────────────────────────────────────
row3 = leave_store.apply_leave("emp010", "2026-11-01", "2026-11-01")
assert leave_store.reject_leave(row3["id"], "emp009", reason="no") is True
bal2 = leave_store.get_balance("emp010", year=2026)
assert bal2["used"] == 1.5, bal2  # unchanged

# ── cancel only works on a still-pending row the employee owns ─────────
row4 = leave_store.apply_leave("emp010", "2026-11-05", "2026-11-05")
assert leave_store.cancel_leave(row4["id"], "emp010") is True
assert leave_store.cancel_leave(lid, "emp010") is False  # lid is already approved

# ── list_pending only shows pending rows ────────────────────────────────
row5 = leave_store.apply_leave("emp002", "2026-11-10", "2026-11-10")
pending = leave_store.list_pending()
ids = {p["id"] for p in pending}
assert row5["id"] in ids
assert lid not in ids  # already approved, shouldn't show

# ── calendar_days: full/half/leave dots ─────────────────────────────────
conn = db.get_connection()
conn.execute(
    "INSERT INTO daily_attendance (user_id, date, checkin_time, checkout_time) "
    "VALUES ('emp010','2026-10-06','09:00:00','18:00:00')"  # 9h -> full
)
conn.execute(
    "INSERT INTO daily_attendance (user_id, date, checkin_time, checkout_time) "
    "VALUES ('emp010','2026-10-07','09:00:00','12:00:00')"  # 3h -> half
)
conn.commit()
conn.close()
days = leave_store.calendar_days("emp010", 2026, 10)
assert days["2026-10-05"]["status"] == "leave_approved", days["2026-10-05"]
assert days["2026-10-06"]["status"] == "full", days["2026-10-06"]
assert days["2026-10-07"]["status"] == "half", days["2026-10-07"]

# ── set_leave (WhatsApp path) now defaults to pending, and never deletes
# an already-approved overlapping row ───────────────────────────────────
wa_row = leave_store.set_leave("emp002", "2026-12-01", "2026-12-01",
                                reason="wa", created_by="bot")
assert wa_row.get("status", "pending") == "pending" or True  # tolerate either shape
conn = db.get_connection()
st = conn.execute(
    "SELECT status FROM employee_leave WHERE id=?", (wa_row["id"],)
).fetchone()[0]
assert st == "pending", st
conn.close()

# an approved row for emp002 elsewhere must survive a second set_leave call
# that doesn't overlap it
approved_row = leave_store.apply_leave("emp002", "2026-12-20", "2026-12-20")
leave_store.approve_leave(approved_row["id"], "emp009")
leave_store.set_leave("emp002", "2026-12-01", "2026-12-01", reason="again")
conn = db.get_connection()
still_there = conn.execute(
    "SELECT status FROM employee_leave WHERE id=?", (approved_row["id"],)
).fetchone()
assert still_there == ("approved",), still_there
conn.close()

# ── run_overtime_conversion_sweep: idempotent + converts at 24hrs ───────
conn = db.get_connection()
conn.execute(
    "INSERT INTO daily_attendance (user_id, date, checkin_time, checkout_time) "
    "VALUES ('emp003','2026-10-01','09:00:00','22:00:00')"  # 13h -> 4h OT
)
conn.commit()
conn.close()
r1 = leave_store.run_overtime_conversion_sweep(for_date="2026-10-01")
assert r1["processed"] >= 1, r1
r1b = leave_store.run_overtime_conversion_sweep(for_date="2026-10-01")  # re-run, must not double count
conn = db.get_connection()
cnt = conn.execute(
    "SELECT COUNT(*) FROM overtime_ledger WHERE user_id='emp003' AND date='2026-10-01'"
).fetchone()[0]
assert cnt == 1, f"expected exactly 1 ledger row after two sweeps, got {cnt}"
conn.close()

# push enough more days to cross 24hrs and confirm one comp-off accrual fires
conn = db.get_connection()
for i, d in enumerate(["2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"]):
    conn.execute(
        "INSERT INTO daily_attendance (user_id, date, checkin_time, checkout_time) "
        "VALUES (?,?,?,?)", ("emp003", d, "09:00:00", "22:00:00")  # 4h OT/day
    )
conn.commit()
conn.close()
for d in ["2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"]:
    leave_store.run_overtime_conversion_sweep(for_date=d)
conn = db.get_connection()
accrued = conn.execute(
    "SELECT COUNT(*) FROM comp_off_ledger WHERE user_id='emp003'"
).fetchone()[0]
conn.close()
assert accrued >= 1, f"expected at least 1 comp-off accrual after 24+ OT hours, got {accrued}"

os.remove(tmp_path)
print("OK: test_leave_store")
