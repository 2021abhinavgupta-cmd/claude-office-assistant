# WhatsApp Daily Standup — Design

## Goal

Let employees manage their daily standup entirely from WhatsApp, alongside the existing `standup.html` UI (both stay usable, same underlying data): see today's tasks each morning, mark them done or blocked by replying, and get nudged if work isn't done by end of day. Founder gets a weekly team completion digest and an escalation ping if someone repeatedly ignores reminders.

## Non-goals

- Replacing `standup.html` — it stays the primary UI; WhatsApp is an additional channel.
- Free-text/AI-parsed task mutation. Every WhatsApp action that changes data uses an exact command the bot can parse deterministically. This codebase has a long history of AI-guessed edits silently corrupting data (see CLAUDE.md gotchas #45, #51, #69, #73) — a WhatsApp command marking the wrong task "done" would be exactly that failure mode, so mutating commands are never AI-classified.
- New employees/onboarding via WhatsApp. Identification is a lookup against the existing `employees.json` `whatsapp` field only — someone not in that file gets the existing generic-chat behavior, unchanged.

## Architecture

Four additions, all inside the existing WhatsApp bot (`app.py`) and scheduler (`task_scheduler.py`):

1. **Employee identification** in `whatsapp_webhook()` — before routing to generic Claude chat, match the inbound `sender` phone number against every employee's `whatsapp` field (`utils._load_employees()`, same live-load pattern used everywhere else per gotcha #63 — never a hardcoded map). Phone number comparison normalizes both sides to digits-only, comparing the last 10 digits (handles a stored number with/without `+`, country code, `whatsapp:` prefix). If matched, route to the new standup command parser instead of generic chat. If not matched, existing behavior is untouched.

2. **Command parser** — deterministic regex-based grammar, checked in order:
   - `^(\d+(?:,\d+)*|all)\s+done$` (case-insensitive) → mark done
   - `^add\s+(.+)$` (case-insensitive) → add task
   - `^blocked\s*:?\s*(\d+)\s+(.+)$` (case-insensitive) → set blocker
   - No match → falls through to the existing generic Claude chat reply, unchanged. The bot does not need a special "sorry I didn't understand" message — an unrecognized message from a known employee still gets a normal, possibly-helpful Claude reply, same as today. (Considered replying with a fixed usage hint on non-match; rejected — indistinguishable from "you typed something wrong" for an employee who just wants to chat with the bot normally, and adds a special case with no real benefit.)

3. **New table `whatsapp_standup_context`** — the numbered list in a "done"/"blocked" reply only means something in relation to the specific list the bot most recently sent that employee. Written every time the bot sends a numbered task list (the 10am morning prompt, and also the on-demand case below); read when a numbered reply arrives.
   ```sql
   CREATE TABLE IF NOT EXISTS whatsapp_standup_context (
       user_id    TEXT NOT NULL,
       date       TEXT NOT NULL,
       task_order TEXT NOT NULL,  -- JSON array of standup_tasks.id, index 0 = "1"
       sent_at    TEXT DEFAULT CURRENT_TIMESTAMP,
       PRIMARY KEY (user_id, date)
   )
   ```
   One row per employee per day — a later resend (e.g. employee texts `add buy milk` mid-afternoon, bot's confirmation re-lists the updated set) overwrites the same row via `INSERT OR REPLACE`, so `N done` always resolves against the most recently shown list, not a stale morning one.

4. **New table `whatsapp_reminder_state`** — tracks consecutive missed-EOD count per employee, for the escalation feature.
   ```sql
   CREATE TABLE IF NOT EXISTS whatsapp_reminder_state (
       user_id                TEXT PRIMARY KEY,
       consecutive_incomplete INTEGER DEFAULT 0,
       last_checked_date      TEXT
   )
   ```

Both tables added via the standard `CREATE TABLE IF NOT EXISTS` pattern in `db.py::init_db()`.

## Scheduler jobs (`task_scheduler.py`)

All new jobs are `cron` triggers registered in `init_scheduler(app)` alongside the existing 08:00 `check_overdue_tasks` job and the 3-minute `attendance_presence_sweep` interval job (gotcha #89). All times IST (matches `today_ist()`/`now_ist()` convention used throughout this codebase — no UTC math needed since Railway's container timezone doesn't matter, these helpers already normalize).

### 10:00 — morning prompt (`send_morning_standup_prompts`)
For every employee with a non-empty `whatsapp` field:
1. Fetch today's tasks the same way `GET /api/standup/my-tasks` does, **including that endpoint's existing auto-carry-over** (gotcha #8) and Creation-Date self-heal (gotcha #50) — call the same underlying function rather than re-implementing the query, so WhatsApp and the web UI can never see a different task list for the same employee/day.
2. If zero tasks, skip (no message sent — avoids a daily "you have nothing to do" ping to someone genuinely idle that day).
3. Otherwise send a numbered list:
   ```
   Good morning! Today's tasks:
   1. Fix header bug (Mellow)
   2. Client call prep
   3. [carried over] Draft brand brief

   Reply "1 done", "1,3 done", or "all done" to mark complete.
   Reply "add <task>" to add something.
   Reply "blocked: 2 waiting on assets" to flag a blocker.
   ```
4. Write the numbered list to `whatsapp_standup_context`.

### 19:00 — EOD nudge (`send_eod_standup_reminders`)
For every employee with a non-empty `whatsapp` field and at least one non-`done` task today:
1. Send a nudge listing only the incomplete tasks (re-numbered 1..N against just that subset — the message is self-contained, doesn't require remembering this morning's numbers).
   ```
   End of day check-in — still open:
   1. Client call prep
   2. Draft brand brief

   Reply "1 done" etc. to update, or it'll carry over to tomorrow.
   ```
2. Overwrite `whatsapp_standup_context` with this re-numbered subset (so a reply to *this* message resolves correctly, not against the stale morning numbering).
3. Update `whatsapp_reminder_state`: increment `consecutive_incomplete` by 1 for this employee; for anyone with zero incomplete tasks today, reset their `consecutive_incomplete` to 0 (do this reset pass for ALL employees with a `whatsapp` field, not just ones who got a nudge, so a genuinely all-caught-up day always clears the streak).
4. **Escalation:** for anyone whose `consecutive_incomplete` just reached exactly 2, send a WhatsApp message to every employee whose `employees.json` `role` contains "founder" (case-insensitive substring match — matches both "Founder" and the literal "1Founder" role string used for Sid/emp010, see CLAUDE.md gotcha #80) naming the employee and their open task titles, then reset that employee's counter to 0 (so it's a one-time ping per two-day streak, not a repeating daily spam once triggered).

Reminders never repeat same-day — the 10am/7pm cadence itself is the "reminds them to do it and repeat next morning" behavior the user asked for; there's no separate retry loop.

### Monday 09:00 — founder weekly digest (`send_weekly_standup_digest`)
Sent to the same "role contains founder" recipient set. Aggregates the prior 7 days (Mon–Sun) using the existing month-to-date-style counting logic from `get_velocity_summary()` (gotcha #77) — reused for a 7-day window instead of month-to-date — per employee: tasks completed, tasks still open/overdue. Format:
```
Weekly Standup Digest (Aug 18–24)
✓ Abhinav: 12 done, 0 open
✓ Palak: 9 done, 1 open
⚠ Anagha: 4 done, 3 open
```

## Marking done / adding / blocking — implementation

All three commands resolve to the **same DB writes the existing web UI already makes**, not new logic:
- `N done` → for each referenced number, look up `standup_tasks.id` via `whatsapp_standup_context.task_order[N-1]`, then call the same internal function `update_my_task()` (`routes/ops.py`) uses — `status='done'`, `progress=100` — including its existing Notion social-task status override (gotcha #64) and Notion sync. Reused directly, not reimplemented, so this inherits that fix rather than risking a second, divergent copy of the same logic (this codebase has been bitten repeatedly by exactly that kind of drift — see gotcha #69's "keep two copies in sync by hand" caveat, and the whole `EMP_NAMES`-duplicated-5x saga in gotcha #63).
- `add <text>` → calls the same code path as `POST /api/standup/smart-add`, `user_id` = the matched employee, `assigned_to` = the matched employee. Bot replies confirming the task was added and re-sends the updated numbered list (refreshing `whatsapp_standup_context`).
- `blocked: N <reason>` → look up the task id the same way, write to `standup_tasks.blocker` (same column `PATCH /api/standup/my-tasks/<id>` already writes) via the same internal function. This automatically surfaces in the existing `/api/blockers` admin dashboard — no new plumbing needed there.

Every command reply confirms what happened in plain text (`"✓ Marked done: Fix header bug"` / `"Added: buy milk"` / `"⚠ Flagged blocked: Client call prep"`) — mirrors this codebase's established rule (CLAUDE.md gotcha #53/#54) of never letting a mutating action complete silently.

**Invalid number handling:** if `N` is out of range for today's stored list (e.g. replying "5 done" when only 3 tasks were sent), reply with an error naming the valid range instead of silently no-op'ing — same "never fail silently" rule.

## Out of scope / deferred

- Voice note standup updates — this app already has a native voice recorder for a different feature (client dependencies); reusing WhatsApp voice notes would need transcription and wasn't asked for.
- Delegating/reassigning tasks via WhatsApp — `standup.html` already has this, not requested for WhatsApp.
- Editing an already-`add`ed task's title via WhatsApp — only create/complete/block are supported; corrections go through `standup.html`.

## Testing approach

No live WhatsApp send in dev (Meta Cloud API requires real env vars per gotcha #59's original bug about the wrong env var name — worth double-checking `WHATSAPP_ACCESS_TOKEN` is what's actually set, not `META_WHATSAPP_TOKEN`, before assuming sends work). Plan:
- Unit-test the command regex parser and phone-number-matching logic in isolation (pure functions, no Flask/DB needed).
- Scratch-test the scheduler job bodies against a temp SQLite DB with mocked `send_whatsapp_message`, asserting on the DB writes and the exact message text built, the same verification pattern used throughout this codebase's Sheets-sync work (CLAUDE.md gotcha #87).
- `pyflakes` + `import app` boot check before deploying, per this codebase's standing convention.
- Live verification only after deploying, by texting the real bot number from a real employee's WhatsApp — no way to test the actual Meta API round-trip locally.
