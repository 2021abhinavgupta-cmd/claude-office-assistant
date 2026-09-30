# Leave Management System — Design Spec

**Date:** 2026-09-30
**Status:** Approved for planning

## Problem

Lumina has no formal leave tracking. Attendance CSV export (gotcha #88) has no
leave column. There is no leave balance, no leave application flow, no
approval workflow, and no way to see who's on leave at a glance. Overtime is
not tracked at all, so there's no way to convert it into extra leave days.

A prior session (gotcha #119) added `backend/leave_store.py` and an
`employee_leave` table, but only as a narrow side-effect: the WhatsApp bot
can instantly mark someone on leave, purely to exempt them from the daily
standup lock (gotcha #108). No approval, no balance, no UI, no half-day
concept, no overtime integration. This spec extends that table rather than
building a second, parallel one.

## Requirements (from the user)

1. Every employee gets 15 leave days.
2. If someone's cumulative overtime exceeds 24 hours, that converts into
   +1 leave day, added to their pool. Repeats every time another 24 hours
   accrues.
3. Attendance/leave summary Excel export should show a leave column.
4. New Leave Management page with a leave-application calendar:
   - Colored dots per date: green = worked a full day, yellow = worked a
     half day, red = on leave. Sat/Sun are always leave (existing weekend
     convention, gotcha #77/#89).
   - Click a date to apply for leave. Deducts from the 15-day pool **only
     once approved by HR**.

## Decisions made during brainstorming

- **Approver:** Noorish (`emp009`, HR) only. Hardcoded check, same pattern
  as the bet feature's 4-person allowlist (gotcha #102) — not the generic
  `_is_admin()`/`bool(user_id)` pattern (gotcha #60), since this is a real
  authority gate, not general app access.
- **Overtime baseline:** 9 hours/day. Hours worked beyond 9 on a given day
  count as that day's overtime.
- **WhatsApp leave (`set_leave`/`clear_leave`, gotcha #119):** changes from
  instant-apply to **request-and-wait**. Calling it from WhatsApp creates the
  same `pending` row the web calendar creates — one source of truth, no
  bypass of HR approval.
  - **Consequence, explicitly accepted:** the standup-lock exemption
    (`routes/ops.py::lock-status`, `on_leave` check) now requires
    `status='approved'`, not just any row. A same-day WhatsApp leave request
    does **not** unlock standup until Noorish approves it. This is a
    deliberate tightening versus gotcha #119's original "instant self-serve"
    behavior, accepted as the cost of closing the self-approval loophole.
- **Leave year:** calendar year (Jan 1 – Dec 31). No carryover — unused days
  are lost at year end. Comp-off earned via overtime conversion **also**
  expires with the same year-end reset (not banked indefinitely) — kept
  simple, one pool, one reset rule.
- **Backdated leave:** allowed. An employee can apply for a past date (e.g.
  filing sick leave after the fact). No restriction beyond normal approval.

## Data model

All changes are additive (`ALTER TABLE` in the existing try/except pattern,
`db.py`) — nothing existing breaks.

### `employee_leave` (existing table, gotcha #119 — extended)

New columns, all nullable/defaulted so existing rows keep working exactly as
before:

| column | type | notes |
|---|---|---|
| `status` | TEXT | `pending` / `approved` / `rejected` / `cancelled`. Default `'approved'` — every row written before this change was an instant WhatsApp grant, and is real leave that was actually taken, so it correctly still counts toward balance usage. |
| `leave_type` | TEXT | `full` / `half`. Default `'full'`. |
| `approved_by` | TEXT | employee id who approved/rejected, null while pending |
| `approved_at` | TEXT | IST timestamp of the decision |

Existing columns unchanged: `id`, `user_id`, `start_date`, `end_date`,
`reason`, `created_by`, `created_at`.

A single row still represents an inclusive `[start_date, end_date]` window,
same as today. The web calendar's "click one date" flow creates a
single-day window (`start_date == end_date`); a future multi-day apply flow
(not built now) could reuse the same row shape.

### `overtime_ledger` (new table)

One row per employee per day that has a computed overtime figure.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | TEXT | |
| `date` | TEXT | `YYYY-MM-DD`, IST |
| `worked_hours` | REAL | from that day's `daily_attendance` checkin/checkout |
| `ot_hours` | REAL | `max(0, worked_hours - 9)` |
| `converted_at` | TEXT | null until this row's hours have been folded into a comp-off accrual |
| `created_at` | TEXT | |

Populated by a new daily job (`task_scheduler.py`, analogous to the existing
08:00/03:30 jobs) that reads yesterday's `daily_attendance` for every active
employee and inserts one row (skips rows with no checkout, i.e. incomplete
days — nothing to compute yet).

### `comp_off_ledger` (new table)

One row per overtime-to-leave conversion event.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | TEXT | |
| `days` | REAL | always `1` for now (24hrs -> 1 day, fixed rate) |
| `source_ot_hours` | REAL | `24`, kept for audit/debug clarity |
| `created_at` | TEXT | IST timestamp, also used for "which calendar year did this accrue in" |

Same daily job, after inserting the day's `overtime_ledger` row, checks that
employee's **unconverted** `ot_hours` sum (`converted_at IS NULL`, current
calendar year only). If it's `>= 24`, marks the oldest rows that sum to 24
converted (`converted_at = now`), inserts one `comp_off_ledger` row, and
leaves any remainder hours unconverted for the next cycle.

### Balance (computed, never stored)

```
leave_used_this_year   = SUM(1.0 if leave_type='full' else 0.5
                          for employee_leave rows
                          where user_id=X and status='approved'
                          and start_date in current calendar year)

comp_earned_this_year  = SUM(days) from comp_off_ledger
                          where user_id=X and created_at in current calendar year

remaining               = 15 + comp_earned_this_year - leave_used_this_year
```

No stored balance column anywhere — always derived from the two ledgers at
read time, same "recompute, don't cache" philosophy the rest of this
codebase already uses for budget/usage numbers.

## Backend

### `backend/leave_store.py` (existing file, extended)

- `apply_leave(user_id, start_date, end_date, leave_type, reason, created_by)`
  → inserts a `pending` row (replaces the old instant-apply `set_leave`
  behavior at the storage layer; the WhatsApp tool and the new HTTP route
  both call this).
- `approve_leave(leave_id, approved_by)` / `reject_leave(leave_id, approved_by)`
  → updates `status`/`approved_by`/`approved_at`. No balance side effects
  needed (computed live).
- `cancel_leave(leave_id, user_id)` → employee cancels their own still-pending
  request.
- `get_balance(user_id, year=None)` → the computed dict above.
- `list_pending()` → org-wide pending requests, for the HR panel.
- `calendar_days(user_id, year, month)` → per-day dict for the month:
  merges `daily_attendance` (worked hours -> green/yellow) with
  `employee_leave` (approved -> red, pending -> orange) and the existing
  Sat/Sun rule. This is the one function the calendar UI actually calls.
- Existing `set_leave`/`clear_leave`/`is_on_leave`/`on_leave_ids`/
  `active_leave` functions **stay**, but `is_on_leave`/`active_leave`'s
  internal query gains `AND status='approved'` (the standup-lock tightening
  from the Decisions section). `set_leave` itself is refactored to call the
  new `apply_leave()` internally instead of writing an already-approved row.

### `backend/routes/leave.py` (new blueprint)

All routes require `user_id` (standard `bool(user_id)` gate for
self-service actions; HR-only routes additionally hard-check
`user_id == "emp009"`, matching the bet-feature convention).

- `GET /api/leave/balance?user_id=` → `{base: 15, comp_earned, used, remaining, year}`
- `GET /api/leave/calendar?user_id=&year=&month=` → per-day dot data for the
  month grid (delegates to `calendar_days`)
- `POST /api/leave/apply` `{user_id, date, end_date?, leave_type, reason}`
  → creates a pending request
- `POST /api/leave/<id>/cancel` `{user_id}` → employee cancels their own
  pending request
- `GET /api/leave/pending` (HR only) → org-wide pending list
- `POST /api/leave/<id>/approve` `{user_id}` (HR only)
- `POST /api/leave/<id>/reject` `{user_id, reason?}` (HR only)
- `GET /api/leave/export` (HR only, mirrors the existing attendance-export
  auth level) → CSV, one row per active employee: Employee, Leaves Used,
  Comp-Off Earned, Remaining, Unconverted OT Hours (current partial cycle).

Registered in `app.py` next to the other blueprints.

### `backend/task_scheduler.py` (extended)

New daily job, `compute_daily_overtime()`, registered alongside the
existing 08:00 (`check_overdue_tasks`) and 03:30 (`data_retention`) crons —
picks an off-peak time (proposing 02:00 IST). Computes yesterday's
`overtime_ledger` rows + runs the conversion check described above, for
every active employee (reuses the existing active/inactive employee filter
convention, gotcha #90).

### `backend/whatsapp_agent.py` (existing tools, behavior change)

- `set_leave` — now calls `leave_store.apply_leave(...)` (pending, not
  instant). Reply text changes to confirm submission, not grant
  ("Sent to Noorish for approval — you'll stay locked out of standup until
  it's approved").
- `clear_leave` — now cancels the caller's own pending/future-approved
  request via `cancel_leave` rather than deleting an instant window.
- New tool: `get_leave_balance` — employee-only, returns the same dict
  `GET /api/leave/balance` does, phrased conversationally.
- No new HR-approval-via-WhatsApp tool in this pass — Noorish approves via
  the web panel only. (Could be added later; out of scope now, flagged as a
  natural follow-up, not built speculatively per YAGNI.)

## Frontend

### `frontend/leave.html` (new page)

- Includes `auth.js`, `shared-config.js` — same convention as every other
  protected page.
- Top strip: "`{remaining} / 15 remaining · {comp_earned} comp-off earned
  this year`", for the logged-in user. If `user_id === "emp009"`, an
  additional "Pending Approvals ({count})" panel is shown with
  approve/reject buttons per request.
- Month-grid calendar (reuse the nav pattern already proven in
  `client-dashboard.html`'s calendar, gotcha #18 — `calPrev()`/`calNext()`
  shape), rendering dots per `GET /api/leave/calendar`:
  - green = full day worked (`worked_hours >= 6`)
  - yellow = partial day worked (`0 < worked_hours < 6`)
  - red = approved leave
  - orange = pending leave request
  - no dot = weekend, or no data (not marking unexplained absence in this
    pass)
- Clicking any date opens a small modal: Full Day / Half Day toggle +
  reason text field + Submit → `POST /api/leave/apply`. Works for past or
  future dates (backdated leave allowed per the Decisions section).
- Toast-based feedback (`toast()`, matching every other page's convention,
  gotcha #54) — no native `alert()`.

### Nav wiring

- `dashboard.html` Quick Actions gains a "Leave" link.
- `standup.html` header nav gains a "Leave" link, same slot pattern as the
  existing "Daily Standup" link placement (gotcha #46).
- Dashboard's existing "Export Attendance" button area gains a second
  "Export Leave Summary" button (HR-visible only, `user.user_id === "emp009"`
  — matches the existing `.admin-only`-style visibility gating already used
  for other founder-only buttons, e.g. Backup DB).

## Explicitly out of scope for this pass

- Multi-day leave application via a date-range picker (data model supports
  it; UI only supports single-date-click for now).
- An "absent, unexplained" calendar dot state.
- HR approving/rejecting via WhatsApp.
- Leave carryover, proration for new joiners, or per-employee custom leave
  allowances (everyone gets a flat 15).
- Editing/backdating the overtime baseline (9hrs) or conversion rate (24hrs
  = 1 day) from the UI — both are constants in code, change by editing them
  directly if the policy changes.

## Testing approach

Matches this codebase's established verification convention (no live test
suite; `py_compile`/`pyflakes` + `node --check` + scratch functional tests
against a temp SQLite DB, never `logs/app.db`):

- `leave_store.py`: balance computation across full/half days, year
  boundaries, comp-off accrual math (including partial/remainder OT hours
  carrying to the next cycle), approve/reject/cancel state transitions.
- `overtime_ledger`/`comp_off_ledger` conversion job: a scratch test
  simulating several days of >9hr attendance rows, confirming the
  conversion fires exactly once per 24hr crossing and leaves the correct
  remainder.
- `routes/leave.py`: auth gate (missing `user_id` → 403/401 consistent with
  the rest of the app), HR-only routes rejecting a non-`emp009` caller,
  full apply → approve → balance-reflects-it round trip.
- `standup-lock` regression: confirm a `pending` leave row does **not**
  exempt from the lock, and an `approved` one does — this is the one
  behavior change to an existing feature, so it gets its own explicit test.
- Frontend: `node --check` on the extracted script block. No live-browser
  verification unless a browser session is available at implementation
  time (same honest-caveat convention as every other frontend feature in
  this codebase's history).
