# WhatsApp Daily Standup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let employees run their daily standup from WhatsApp (see today's tasks, mark done/blocked, add tasks) alongside the existing `standup.html` UI, with automatic 10am/7pm reminders and a founder weekly digest + missed-reminder escalation.

**Architecture:** Extend the existing Meta Cloud API WhatsApp bot (`app.py`) with employee identification + a deterministic command parser (new `backend/whatsapp_standup.py`), reusing the existing standup CRUD logic (refactored out of `routes/ops.py`'s route handlers into plain functions so both the web UI and WhatsApp call the same code, never two copies). Three new APScheduler cron jobs in `task_scheduler.py` drive the reminders/digest.

**Tech Stack:** Flask, SQLite (`db.py`), APScheduler (already a dependency), Meta Cloud API (already wired via `send_whatsapp_message()` in `app.py`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-25-whatsapp-standup-design.md`

## Global Constraints

- No AI/free-text parsing for any WhatsApp command that mutates data (`done`, `add`, `blocked`) — regex-only, per the spec's Non-goals section and this codebase's established burn history with AI-guessed edits (CLAUDE.md gotchas #45, #51, #69, #73).
- Every mutating command replies with an explicit confirmation or an explicit error — never a silent no-op (CLAUDE.md gotcha #53/#54 convention).
- Employee/name/phone lookups always read `employees.json` live via `utils._load_employees()` — never a hardcoded id/name/phone map (CLAUDE.md gotcha #63 — this exact mistake was made 5 times in this codebase already).
- All new scheduler cron jobs pass `timezone=IST` explicitly (`from utils import IST`) — do not rely on server-local time like the pre-existing 08:00 job does.
- No new third-party dependencies (no pytest, no scheduling library beyond the already-installed `apscheduler`). Verification uses this project's actual established convention: `pyflakes`, `import app` boot check, and standalone `python` scratch scripts with plain `assert` statements (see CLAUDE.md's dozens of gotcha entries — this codebase has no pytest suite; introducing one is out of scope here).
- Money/notification correctness matters here (missed reminders = a real accountability tool) — every DB write in this feature goes through the connection helper (`db.get_connection()`), never a bare `sqlite3.connect()`.

---

### Task 1: New tables — `whatsapp_standup_context`, `whatsapp_reminder_state`

**Files:**
- Modify: `backend/db.py` (inside `init_db()`, right after the `standup_tasks` ALTER TABLE block, before the `task_risk` table — i.e. right after line 163's `due_date` ALTER, before line 165's `# Task risk escalation log` comment)

**Interfaces:**
- Produces: two tables other tasks read/write via plain `sqlite3` calls through `db.get_connection()`.

- [ ] **Step 1: Add the two `CREATE TABLE IF NOT EXISTS` blocks**

Insert immediately after the existing `due_date` ALTER TABLE try/except block (currently ending at line 163) and before the `# Task risk escalation log` comment:

```python
        # WhatsApp standup: maps a numbered list sent to an employee (morning
        # prompt or EOD nudge) back to real standup_tasks ids, so a reply like
        # "1,3 done" resolves correctly. One row per employee per day --
        # INSERT OR REPLACE so a later resend (e.g. after "add <task>") always
        # overwrites with the freshest numbering.
        conn.execute("""CREATE TABLE IF NOT EXISTS whatsapp_standup_context (
            user_id    TEXT NOT NULL,
            date       TEXT NOT NULL,
            task_order TEXT NOT NULL,
            sent_at    TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, date)
        )""")

        # WhatsApp standup: tracks consecutive evenings an employee had
        # incomplete tasks at the 19:00 nudge, for the 2-in-a-row founder
        # escalation. Reset to 0 whenever an employee has zero incomplete
        # tasks at nudge time, or right after an escalation fires.
        conn.execute("""CREATE TABLE IF NOT EXISTS whatsapp_reminder_state (
            user_id                TEXT PRIMARY KEY,
            consecutive_incomplete INTEGER DEFAULT 0,
            last_checked_date      TEXT
        )""")

```

- [ ] **Step 2: Verify the migration boots clean**

Run: `cd backend && ../.venv/Scripts/python.exe -c "import db; db.init_db(); print('OK')"`
Expected: `OK`, no exception. Then confirm both tables exist:

Run: `../.venv/Scripts/python.exe -c "import db; c = db.get_connection(); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'whatsapp_%'\").fetchall()])"`
Expected: `['whatsapp_standup_context', 'whatsapp_reminder_state']`

- [ ] **Step 3: Commit**

```bash
git add backend/db.py
git commit -m "Add whatsapp_standup_context and whatsapp_reminder_state tables for WhatsApp standup feature"
```

---

### Task 2: Extract reusable standup logic in `routes/ops.py`

**Files:**
- Modify: `backend/routes/ops.py` (`update_my_task()` ~line 1068-1156, `standup_smart_add()` ~line 954-1020, `get_my_tasks()` ~line 410-506)

**Interfaces:**
- Produces:
  - `_apply_standup_task_update(task_id: int, status: str = None, blocker: str = None, title: str = None, progress: int = None, subtasks: list = None) -> dict` — returns `{"success": True}` or `{"error": "..."}, <status_code>`-shaped dict (caller decides how to surface it); does the exact same DB write + Notion sync `update_my_task()` currently does inline, including the social-task status override (gotcha #64).
  - `_smart_add_standup_task_impl(user_id: str, assigned_to: str, title: str, due_date: str = "") -> dict` — returns `{"success": True, "task_id": ..., "title": ..., "notion_id": ..., "is_project": ...}` or `{"error": "..."}`.
  - `_fetch_standup_tasks_for_user(user_id: str, date_str: str = None) -> tuple[list, str]` — returns `(tasks, date_str)`, same shape `GET /api/standup/my-tasks` returns, including auto-carry-over and the Creation-Date self-heal.
- Consumes: nothing new — pure refactor, same imports already present in `ops.py` (`notion_store`, `_su_conn`, `today_ist`, `json`, `re`).

This is a pure extraction — no behavior change. The existing route handlers become thin wrappers that parse the Flask request and call these functions.

- [ ] **Step 1: Extract `_apply_standup_task_update()`**

Replace the body of `update_my_task()` (lines 1068-1156) with:

```python
def _apply_standup_task_update(task_id: int, status: str = None, blocker: str = None,
                                title: str = None, progress: int = None, subtasks: list = None) -> dict:
    """Shared by the PATCH /api/standup/my-tasks/<id> route and the WhatsApp
    'done'/'blocked' commands -- do not duplicate this logic a second time,
    see CLAUDE.md gotcha #63/#69 for why this codebase keeps getting bitten
    by exactly that."""
    if status is not None and status not in ("done", "pending"):
        return {"error": "status must be 'done' or 'pending'"}

    updates = []
    params = []
    if status is not None:
        updates.append("status=?")
        params.append(status)
    if blocker is not None:
        updates.append("blocker=?")
        params.append(blocker)
    if title is not None:
        updates.append("title=?")
        params.append(title.strip())
    if subtasks is not None:
        updates.append("subtasks=?")
        params.append(json.dumps(subtasks))

    if not updates:
        return {"error": "no updates provided"}

    params.append(task_id)

    conn = _su_conn()
    with conn:
        conn.execute(f"UPDATE standup_tasks SET {', '.join(updates)} WHERE id=?", params)

    cur = conn.cursor()
    cur.execute("SELECT notion_id, subtasks, status FROM standup_tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    conn.close()

    notion_id = row[0] if row else None
    current_subtasks_json = row[1] if row else '[]'
    current_status = row[2] if row else 'pending'

    if notion_id:
        try:
            notion_status = None
            notion_progress = None

            if progress is not None and current_status == "done":
                notion_progress = int(progress)
                if notion_progress == 100: notion_status = "Done"
                elif notion_progress > 0: notion_status = "In Progress"

            elif subtasks is not None:
                st = json.loads(current_subtasks_json) if current_subtasks_json else []
                if st:
                    done_count = sum(1 for s in st if s.get("done"))
                    notion_progress = int((done_count / len(st)) * 100)
                    if notion_progress == 100: notion_status = "Done"
                    elif notion_progress > 0: notion_status = "In Progress"

            if notion_progress is not None:
                if notion_status == "Done":
                    task_type = notion_store.get_task_type(notion_id)
                    if task_type and task_type.lower() == "social media":
                        notion_status = "need_for_approval"
                        conn = _su_conn()
                        conn.execute("UPDATE standup_tasks SET status='need_for_approval' WHERE id=?", (task_id,))
                        conn.commit()
                        conn.close()

                notion_store.update_task(notion_id, progress=notion_progress, status=notion_status)
        except Exception as e:
            logger.warning(f"Notion sync failed for task {notion_id}: {e}")

    return {"success": True}


@ops_bp.route("/api/standup/my-tasks/<int:task_id>", methods=["PATCH"])
def update_my_task(task_id: int):
    """Update a task's status or blocker. Body: { status?, blocker?, title?, progress?, subtasks? }"""
    body = request.get_json(silent=True) or {}
    result = _apply_standup_task_update(
        task_id,
        status=body.get("status"),
        blocker=body.get("blocker"),
        title=body.get("title"),
        progress=body.get("progress"),
        subtasks=body.get("subtasks"),
    )
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)
```

- [ ] **Step 2: Extract `_smart_add_standup_task_impl()`**

Replace the body of `standup_smart_add()` (lines 954-1020) with:

```python
def _smart_add_standup_task_impl(user_id: str, assigned_to: str, title: str, due_date: str = "") -> dict:
    """Shared by POST /api/standup/smart-add and the WhatsApp 'add <task>' command."""
    if not user_id or not title:
        return {"error": "user_id and title required"}

    system_prompt = """You are an AI task router.
The user just typed a new task into their daily standup list.
Is this a "Project Task" that should be tracked in a main project board (e.g. creating a feature, designing a page, writing a proposal), or is it a "Quick Chore" (e.g. check email, call client, meeting, lunch)?
If it's a Project Task, guess the Client Name from the title if possible (otherwise use "Internal").
Respond ONLY in valid JSON format:
{
  "is_project_task": true/false,
  "client_name": "Name or Internal"
}"""

    try:
        resp = _claude_call(system_prompt, title, 200)
        match = re.search(r'\{.*\}', resp, re.DOTALL)
        resp_json = json.loads(match.group(0)) if match else json.loads(resp)
        is_project = resp_json.get("is_project_task", False)
        client = resp_json.get("client_name", "Internal")
    except Exception as e:
        logger.error(f"Auto-Router failed: {e}")
        is_project = False
        client = "Internal"

    notion_id = None

    if is_project and notion_store.is_configured():
        if not due_date:
            due_date = today_ist()
        created = notion_store.create_task(
            title=title, client_name=client, client_notion_id="",
            assigned_to=assigned_to, due_date=due_date, status="in_progress",
            creation_date=today_ist()
        )
        if created and "id" in created:
            notion_id = created["id"]

    conn = _su_conn()
    with conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO standup_tasks (user_id, title, status, date, notion_id, due_date) VALUES (?, ?, 'pending', date('now'), ?, ?)",
            (user_id, title, notion_id, due_date)
        )
        task_id = cur.lastrowid

    return {"success": True, "task_id": task_id, "title": title, "notion_id": notion_id, "is_project": is_project}


@ops_bp.route("/api/standup/smart-add", methods=["POST"])
def standup_smart_add():
    body = request.get_json(silent=True) or {}
    result = _smart_add_standup_task_impl(
        user_id=body.get("user_id", ""),
        assigned_to=body.get("assigned_to", ""),
        title=body.get("title", "").strip(),
        due_date=body.get("due_date", "").strip(),
    )
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)
```

- [ ] **Step 3: Extract `_fetch_standup_tasks_for_user()`**

In `get_my_tasks()` (lines 410-506), wrap the entire body (everything currently between the `user_id`/`date_str` validation and the final `return jsonify(...)`) into a new function, keeping the route as a thin wrapper:

```python
def _fetch_standup_tasks_for_user(user_id: str, date_str: str = None) -> tuple:
    """Shared by GET /api/standup/my-tasks and the WhatsApp 10am morning
    prompt -- includes the existing auto-carry-over (gotcha #8) and
    Creation-Date self-heal (gotcha #50) so WhatsApp and the web UI can
    never show a different task list for the same employee/day."""
    date_str = date_str or today_ist()

    conn = _su_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM standup_tasks WHERE user_id=? AND date=?", (user_id, date_str))
    existing = cur.fetchall()
    if not existing and date_str == today_ist():
        cur.execute("SELECT MAX(date) FROM standup_tasks WHERE user_id=? AND date<?", (user_id, date_str))
        last_date_row = cur.fetchone()
        if last_date_row and last_date_row[0]:
            last_date = last_date_row[0]
            cur.execute(
                "SELECT title, blocker, carried_from, notion_id, due_date, subtasks, delegated_to, delegated_from FROM standup_tasks WHERE user_id=? AND date=? AND status='pending'",
                (user_id, last_date),
            )
            pending = cur.fetchall()
            if pending:
                with conn:
                    for title, blocker, carried_from, nid, dd, sub, d_to, d_from in pending:
                        orig_carry_from = carried_from if carried_from else last_date
                        conn.execute(
                            "INSERT INTO standup_tasks (user_id, date, title, status, carried_from, blocker, notion_id, due_date, subtasks, delegated_to, delegated_from) VALUES (?,?,?,'pending',?,?,?,?,?,?,?)",
                            (user_id, date_str, title, orig_carry_from, blocker, nid, dd, sub, d_to, d_from),
                        )

    def parse_date_for_sort(d):
        if not d:
            return "9999-12-31"
        if re.match(r"^\d{2}-\d{2}-\d{4}$", d.strip()):
            return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
        if re.match(r"^\d{2}/\d{2}/\d{4}$", d.strip()):
            return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
        return d.strip()

    cur.execute(
        "SELECT id, title, status, carried_from, created_at, blocker, notion_id, subtasks, delegated_to, delegated_from, due_date FROM standup_tasks WHERE user_id=? AND date=?",
        (user_id, date_str),
    )
    rows = cur.fetchall()

    if date_str == today_ist() and notion_store.is_configured():
        future_ids = []
        for r in rows:
            nid, row_status = r[6], r[2]
            if nid and row_status not in ("done", "deleted", "delegated"):
                try:
                    if _task_creation_is_future(nid, date_str):
                        future_ids.append(r[0])
                except Exception:
                    pass
        if future_ids:
            with conn:
                conn.executemany("DELETE FROM standup_tasks WHERE id=?", [(i,) for i in future_ids])
            rows = [r for r in rows if r[0] not in future_ids]

    conn.close()

    tasks = []
    for r in rows:
        if r[2] in ("deleted", "delegated"): continue
        st = []
        try:
            st = json.loads(r[7]) if r[7] else []
        except: pass
        tasks.append({
            "id": r[0], "title": r[1], "status": r[2],
            "carried_from": r[3], "created_at": r[4],
            "blocker": r[5], "notion_id": r[6], "subtasks": st,
            "delegated_to": r[8], "delegated_from": r[9], "due_date": r[10]
        })

    tasks.sort(key=lambda x: (parse_date_for_sort(x["due_date"]), x["id"]))
    return tasks, date_str


@ops_bp.route("/api/standup/my-tasks", methods=["GET"])
def get_my_tasks():
    """Get an employee's task list for a given date. Query: user_id, date (optional)."""
    user_id  = request.args.get("user_id", "").strip()
    date_str = request.args.get("date", "") or None
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    tasks, resolved_date = _fetch_standup_tasks_for_user(user_id, date_str)
    return jsonify({"tasks": tasks, "date": resolved_date})
```

- [ ] **Step 4: Verify no regression**

Run: `cd backend && ../.venv/Scripts/python.exe -m pyflakes routes/ops.py`
Expected: same warning count as before this change (no new undefined-name errors — this is the exact bug class CLAUDE.md's Debugging section warns about for this file).

Run: `cd backend && ../.venv/Scripts/python.exe -c "import app; print('OK')"`
Expected: `OK`

Start the dev server (`python -m backend.app` from repo root) and manually verify with curl against a real (or throwaway) `user_id`:
- `GET /api/standup/my-tasks?user_id=emp003` still returns the same shape as before.
- `POST /api/standup/smart-add` with `{"user_id":"emp003","title":"test task xyz","assigned_to":"emp003"}` still creates a row.
- `PATCH /api/standup/my-tasks/<id>` with `{"status":"pending"}` (revert whatever the smart-add test created) still works.
Then delete the test row created above via `DELETE /api/standup/my-tasks/<id>` to leave no test data behind.

- [ ] **Step 5: Commit**

```bash
git add backend/routes/ops.py
git commit -m "Extract standup task update/create/fetch logic into plain functions for reuse by WhatsApp standup"
```

---

### Task 3: Phone matching + command parser (`whatsapp_standup.py`)

**Files:**
- Create: `backend/whatsapp_standup.py`

**Interfaces:**
- Consumes: `utils._load_employees()` (existing).
- Produces:
  - `find_employee_by_whatsapp(sender: str) -> dict | None` — returns the matching employee dict from `employees.json`, or `None`.
  - `parse_standup_command(text: str) -> dict` — returns one of:
    - `{"type": "done", "numbers": [1, 3]}` (or `{"type": "done", "numbers": "all"}`)
    - `{"type": "add", "title": "buy milk"}`
    - `{"type": "blocked", "number": 2, "reason": "waiting on assets"}`
    - `{"type": "none"}` (no command matched — caller falls through to generic chat)

- [ ] **Step 1: Write the module with both functions**

```python
"""
whatsapp_standup.py -- WhatsApp Daily Standup
==============================================
Lets employees run their daily standup from WhatsApp: see today's tasks,
mark them done/blocked, add new ones. Deterministic command parsing only --
no AI-guessed edits (see docs/superpowers/specs/2026-08-25-whatsapp-standup-design.md
"Non-goals" and CLAUDE.md gotchas #45/#51/#69/#73 for why).
"""
import re
import logging
from typing import Optional

from utils import _load_employees

logger = logging.getLogger(__name__)

_DONE_RE = re.compile(r"^(all|\d+(?:\s*,\s*\d+)*)\s+done$", re.IGNORECASE)
_ADD_RE = re.compile(r"^add\s+(.+)$", re.IGNORECASE | re.DOTALL)
_BLOCKED_RE = re.compile(r"^blocked\s*:?\s*(\d+)\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _normalize_phone(raw: str) -> str:
    """Digits-only, last 10 digits -- handles a stored number with/without
    '+', country code, or a 'whatsapp:' prefix from either side."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def find_employee_by_whatsapp(sender: str) -> Optional[dict]:
    """Match an inbound WhatsApp sender number against employees.json's
    'whatsapp' field. Read live every call (utils._load_employees()) --
    never cache/hardcode this, see CLAUDE.md gotcha #63."""
    sender_norm = _normalize_phone(sender)
    if not sender_norm:
        return None
    try:
        data = _load_employees()
    except Exception:
        logger.exception("Failed to load employees.json for WhatsApp identification")
        return None
    for emp in data.get("employees", []):
        wa = emp.get("whatsapp", "")
        if wa and _normalize_phone(wa) == sender_norm:
            return emp
    return None


def parse_standup_command(text: str) -> dict:
    """Deterministic command grammar -- see module docstring for why this
    is never AI-classified. Checked in order; first match wins."""
    stripped = (text or "").strip()

    m = _DONE_RE.match(stripped)
    if m:
        raw = m.group(1).strip()
        if raw.lower() == "all":
            return {"type": "done", "numbers": "all"}
        numbers = [int(n.strip()) for n in raw.split(",") if n.strip()]
        return {"type": "done", "numbers": numbers}

    m = _BLOCKED_RE.match(stripped)
    if m:
        return {"type": "blocked", "number": int(m.group(1)), "reason": m.group(2).strip()}

    m = _ADD_RE.match(stripped)
    if m:
        return {"type": "add", "title": m.group(1).strip()}

    return {"type": "none"}
```

Note: `_BLOCKED_RE` is checked before `_ADD_RE` deliberately -- `"blocked: 2 ..."` would otherwise never match `_ADD_RE` anyway (doesn't start with "add"), but ordering blocked before add keeps the list in the same priority order as the spec lists the commands, for readability.

- [ ] **Step 2: Write and run a standalone verification script**

Create a throwaway script (not committed) at the scratchpad path and run it directly:

```python
import sys
sys.path.insert(0, r"c:\Users\abhin\OneDrive\Desktop\claude-office-assistant\backend")
from whatsapp_standup import parse_standup_command, _normalize_phone

# Command parsing
assert parse_standup_command("1 done") == {"type": "done", "numbers": [1]}
assert parse_standup_command("1,3 done") == {"type": "done", "numbers": [1, 3]}
assert parse_standup_command("1, 3 done") == {"type": "done", "numbers": [1, 3]}
assert parse_standup_command("ALL DONE") == {"type": "done", "numbers": "all"}
assert parse_standup_command("add buy milk") == {"type": "add", "title": "buy milk"}
assert parse_standup_command("Add Buy Milk") == {"type": "add", "title": "Buy Milk"}
assert parse_standup_command("blocked: 2 waiting on assets") == {"type": "blocked", "number": 2, "reason": "waiting on assets"}
assert parse_standup_command("blocked 2 waiting on assets") == {"type": "blocked", "number": 2, "reason": "waiting on assets"}
assert parse_standup_command("hey what's the weather") == {"type": "none"}
assert parse_standup_command("") == {"type": "none"}
assert parse_standup_command("addvantage of this") == {"type": "none"}  # must NOT false-match "add"

# Phone normalization
assert _normalize_phone("+919702908716") == "9702908716"
assert _normalize_phone("919702908716") == "9702908716"
assert _normalize_phone("whatsapp:+919702908716") == "9702908716"
assert _normalize_phone("9702908716") == "9702908716"
assert _normalize_phone("") == ""

print("ALL GREEN")
```

Run: `../.venv/Scripts/python.exe <script path>`
Expected: `ALL GREEN`. If `"addvantage of this"` incorrectly matches `_ADD_RE`, tighten the regex to require a word boundary/space after "add" (`^add\s+`  already requires `\s+` immediately after "add", so "addvantage" should correctly fail — this assertion exists specifically to catch a regression if that requirement is ever loosened).

- [ ] **Step 3: pyflakes + boot check**

Run: `cd backend && ../.venv/Scripts/python.exe -m pyflakes whatsapp_standup.py`
Expected: clean (no warnings).

Run: `cd backend && ../.venv/Scripts/python.exe -c "import whatsapp_standup; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add backend/whatsapp_standup.py
git commit -m "Add phone-number matching and deterministic command parser for WhatsApp standup"
```

---

### Task 4: Message builders + dispatch (`whatsapp_standup.py`)

**Files:**
- Modify: `backend/whatsapp_standup.py`

**Interfaces:**
- Consumes: `routes.ops._apply_standup_task_update`, `routes.ops._smart_add_standup_task_impl`, `routes.ops._fetch_standup_tasks_for_user` (Task 2); `db.get_connection`.
- Produces: `handle_standup_message(employee: dict, text: str) -> Optional[str]` — returns the reply text to send back, or `None` if the message didn't match any standup command (caller should fall through to generic chat). Also produces `build_task_list_message(tasks: list, heading: str) -> str` and `save_task_context(user_id: str, date_str: str, task_ids: list)` / `get_task_context(user_id: str, date_str: str) -> list`, used again directly by the scheduler jobs in Task 7.

- [ ] **Step 1: Add context storage helpers**

Append to `whatsapp_standup.py`:

```python
def save_task_context(user_id: str, date_str: str, task_ids: list) -> None:
    """Record which standup_tasks.id each number ('1', '2', ...) in the most
    recently sent WhatsApp list refers to. Overwrites any existing row for
    this user/date -- a reply to 'N done' always resolves against the
    freshest list shown, not a stale morning one (see design doc)."""
    from db import get_connection
    import json
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO whatsapp_standup_context (user_id, date, task_order, sent_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (user_id, date_str, json.dumps(task_ids)),
        )
    conn.close()


def get_task_context(user_id: str, date_str: str) -> list:
    """Returns the ordered list of standup_tasks.id for this user/date, or [] if none sent yet."""
    from db import get_connection
    import json
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT task_order FROM whatsapp_standup_context WHERE user_id=? AND date=?", (user_id, date_str))
    row = cur.fetchone()
    conn.close()
    if not row:
        return []
    try:
        return json.loads(row[0])
    except Exception:
        return []
```

- [ ] **Step 2: Add the message builder**

```python
def build_task_list_message(tasks: list, heading: str) -> str:
    """tasks: list of standup task dicts (id, title, carried_from, ...). Builds
    the numbered list text and returns it; caller is responsible for calling
    save_task_context() with the matching id order separately."""
    if not tasks:
        return ""
    lines = [heading, ""]
    for i, t in enumerate(tasks, start=1):
        prefix = "[carried over] " if t.get("carried_from") else ""
        lines.append(f"{i}. {prefix}{t['title']}")
    return "\n".join(lines)
```

- [ ] **Step 3: Add the dispatch function**

```python
def handle_standup_message(employee: dict, text: str) -> Optional[str]:
    """Returns the WhatsApp reply text for a recognized standup command, or
    None if `text` didn't match one (caller falls through to generic chat)."""
    from utils import today_ist
    from routes.ops import _apply_standup_task_update, _smart_add_standup_task_impl, _fetch_standup_tasks_for_user

    cmd = parse_standup_command(text)
    if cmd["type"] == "none":
        return None

    user_id = employee["id"]
    today = today_ist()
    context_ids = get_task_context(user_id, today)

    if cmd["type"] == "done":
        if not context_ids:
            return "I don't have a task list on file for you today yet -- you'll get one at 10am, or text \"add <task>\" to start one now."

        if cmd["numbers"] == "all":
            indices = list(range(1, len(context_ids) + 1))
        else:
            indices = cmd["numbers"]

        out_of_range = [n for n in indices if n < 1 or n > len(context_ids)]
        if out_of_range:
            return f"No task(s) numbered {', '.join(str(n) for n in out_of_range)} -- your list only has 1-{len(context_ids)}. Nothing was changed."

        marked = []
        for n in indices:
            task_id = context_ids[n - 1]
            result = _apply_standup_task_update(task_id, status="done", progress=100)
            if "error" not in result:
                marked.append(n)

        if not marked:
            return "Couldn't mark those done -- something went wrong. Try again or use the app."
        return f"Marked done: {', '.join(str(n) for n in marked)}"

    if cmd["type"] == "add":
        result = _smart_add_standup_task_impl(user_id=user_id, assigned_to=user_id, title=cmd["title"])
        if "error" in result:
            return f"Couldn't add that task: {result['error']}"
        tasks, _ = _fetch_standup_tasks_for_user(user_id, today)
        save_task_context(user_id, today, [t["id"] for t in tasks])
        listing = build_task_list_message(tasks, "Added. Today's list:")
        return f"Added: {cmd['title']}\n\n{listing}"

    if cmd["type"] == "blocked":
        n = cmd["number"]
        if n < 1 or n > len(context_ids):
            return f"No task numbered {n} -- your list only has 1-{len(context_ids)}. Nothing was changed."
        task_id = context_ids[n - 1]
        result = _apply_standup_task_update(task_id, blocker=cmd["reason"])
        if "error" in result:
            return f"Couldn't flag that as blocked: {result['error']}"
        return f"Flagged blocked: task {n} -- {cmd['reason']}"

    return None
```

- [ ] **Step 4: Scratch-test the dispatch logic against a temp DB**

Write and run a standalone script (not committed) that:
1. Sets `os.environ["DB_PATH"]` to a temp file path **before** `import db` — `db.py` reads `DB_PATH = os.environ.get("DB_PATH", _default_db)` at module import time, so the env var must be set first.
2. Calls `db.init_db()` against that temp DB.
3. Inserts one fake employee-shaped dict (doesn't need to touch `employees.json`) and 2 fake `standup_tasks` rows for `user_id="test_emp"`, `date=today_ist()`.
4. Calls `save_task_context("test_emp", today, [id1, id2])`.
5. Calls `handle_standup_message({"id": "test_emp"}, "1 done")`, asserts the reply mentions "Marked done: 1", then queries `standup_tasks` directly to confirm row `id1`'s status is now `'done'`.
6. Calls `handle_standup_message({"id": "test_emp"}, "5 done")` (out of range), asserts the reply names the valid range and that no row was changed.
7. Calls `handle_standup_message({"id": "test_emp"}, "add scratch test task")`, asserts a new row was inserted and the context was refreshed to include it.
8. Calls `handle_standup_message({"id": "test_emp"}, "blocked: 2 waiting on client")`, asserts `standup_tasks.blocker` for `id2` is now `"waiting on client"`.
9. Prints `ALL GREEN` if every assertion passed.

Run it and confirm `ALL GREEN`. Delete the temp DB file afterward.

- [ ] **Step 5: pyflakes + boot check**

Run: `cd backend && ../.venv/Scripts/python.exe -m pyflakes whatsapp_standup.py`
Expected: clean.

Run: `cd backend && ../.venv/Scripts/python.exe -c "import app; print('OK')"`
Expected: `OK` (confirms no circular-import issue between `whatsapp_standup.py` and `routes.ops`, since the dispatch function imports from `routes.ops` lazily inside the function body specifically to avoid a module-load-time circular import with `app.py`).

- [ ] **Step 6: Commit**

```bash
git add backend/whatsapp_standup.py
git commit -m "Add WhatsApp standup message builders and command dispatch"
```

---

### Task 5: Wire into the WhatsApp webhook (`app.py`)

**Files:**
- Modify: `backend/app.py` (`whatsapp_webhook()`, ~line 3441)

**Interfaces:**
- Consumes: `whatsapp_standup.find_employee_by_whatsapp`, `whatsapp_standup.handle_standup_message` (Tasks 3-4).

- [ ] **Step 1: Add employee-match check before the generic-chat path**

In `whatsapp_webhook()`, right after `text = message["text"]["body"]` (line 3452) and before the budget-guard check, insert:

```python
        # WhatsApp standup: if this sender is a known employee, try the
        # deterministic standup command parser first. Falls through to the
        # existing generic Claude chat below if it's not a recognized
        # standup command (e.g. an employee just chatting with the bot).
        from whatsapp_standup import find_employee_by_whatsapp, handle_standup_message
        employee = find_employee_by_whatsapp(sender)
        if employee:
            standup_reply = handle_standup_message(employee, text)
            if standup_reply is not None:
                send_whatsapp_message(sender, standup_reply)
                logger.info(f"WhatsApp standup command handled for {employee['id']}")
                return "OK", 200
```

This deliberately runs before the budget guard -- standup commands are cheap deterministic DB operations with zero Claude API calls (except `add`, which goes through the existing `_smart_add_standup_task_impl`'s Haiku router call, same as it already does via the web UI's smart-add today -- unchanged cost profile, not a new budget concern).

- [ ] **Step 2: Verify existing generic-chat behavior is unaffected for non-employee senders**

Run: `cd backend && ../.venv/Scripts/python.exe -m pyflakes app.py`
Expected: same warning count as before (this file has pre-existing unused-import noise per CLAUDE.md's Debugging section -- confirm no *new* undefined-name warnings, don't chase the pre-existing baseline).

Run: `cd backend && ../.venv/Scripts/python.exe -c "import app; print('OK')"`
Expected: `OK`

Manual check (dev server running): POST a synthetic webhook payload shaped like Meta's real payload, with `sender` set to a phone number NOT in `employees.json` and text `"hello"` -- confirm it still reaches the generic Claude chat path (check server logs for `"WhatsApp reply sent to..."`, not the new standup log line).

- [ ] **Step 3: Commit**

```bash
git add backend/app.py
git commit -m "Route known-employee WhatsApp senders through the standup command parser before generic chat"
```

---

### Task 6: Weekly completion helper (`routes/ops.py`)

**Files:**
- Modify: `backend/routes/ops.py` (near `get_velocity_summary()`, ~line 358)

**Interfaces:**
- Produces: `get_weekly_completion_by_user(since_date: str) -> dict` — returns `{user_id: {"completed": int, "open": int}}` for every `user_id` with at least one `standup_tasks` row on/after `since_date`. Mirrors `get_velocity_summary()`'s weekday-exclusion convention (gotcha #77's follow-up) since a weekly digest has the same "don't count weekend noise" concern.

- [ ] **Step 1: Add the function**

Insert after `get_velocity_summary()` (after line 357/358):

```python
def get_weekly_completion_by_user(since_date: str) -> dict:
    """user_id -> {"completed": N, "open": N} for the window [since_date, today].
    Weekends excluded from the 'completed' count, matching the existing
    /velocity chart's weekday-only convention (gotcha #77's follow-up) --
    open/overdue counts intentionally include weekend-carried rows since an
    open task doesn't stop being open over a weekend."""
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT user_id, status, date FROM standup_tasks
           WHERE date >= ? AND status != 'deleted'""",
        (since_date,),
    )
    rows = cur.fetchall()
    conn.close()

    result = {}
    for user_id, status, date_str in rows:
        result.setdefault(user_id, {"completed": 0, "open": 0})
        if status == "done":
            import datetime as _dt
            try:
                if _dt.date.fromisoformat(date_str).weekday() < 5:  # Mon-Fri only
                    result[user_id]["completed"] += 1
            except Exception:
                result[user_id]["completed"] += 1
        elif status == "pending":
            result[user_id]["open"] += 1
    return result
```

- [ ] **Step 2: Scratch-test against a temp DB**

Write and run a standalone script that sets `os.environ["DB_PATH"]` to a temp file path before `import db` (same approach as Task 4 Step 4), seeds a handful of `standup_tasks` rows across a weekday and a weekend date for two users, calls `get_weekly_completion_by_user(since_date)`, and asserts the weekday `done` row counts but the weekend `done` row doesn't, while `pending` rows count regardless of weekday. Confirm `ALL GREEN`, delete the temp DB.

- [ ] **Step 3: pyflakes + boot check**

Run: `cd backend && ../.venv/Scripts/python.exe -m pyflakes routes/ops.py`
Expected: same baseline as Task 2's check.

- [ ] **Step 4: Commit**

```bash
git add backend/routes/ops.py
git commit -m "Add get_weekly_completion_by_user() for the WhatsApp founder weekly digest"
```

---

### Task 7: Scheduler jobs (`task_scheduler.py`)

**Files:**
- Modify: `backend/task_scheduler.py`

**Interfaces:**
- Consumes: `whatsapp_standup.find_employee_by_whatsapp`'s underlying employee list (via `utils._load_employees()` directly, not a re-lookup by phone -- this module iterates all employees, not matching a single inbound sender), `whatsapp_standup.build_task_list_message`, `whatsapp_standup.save_task_context`, `routes.ops._fetch_standup_tasks_for_user`, `routes.ops.get_weekly_completion_by_user`, `app.send_whatsapp_message`.
- Produces: three functions registered as new cron jobs in `init_scheduler()`.

- [ ] **Step 1: Add the morning-prompt job**

Append to `task_scheduler.py` (after `_run_attendance_sweep()`, before `init_scheduler()`):

```python
def send_morning_standup_prompts():
    """10:00 IST daily -- text every employee with a WhatsApp number their
    today's task list (including auto-carry-over), and record the numbering
    so a later 'N done' reply resolves correctly."""
    from utils import _load_employees, today_ist
    from routes.ops import _fetch_standup_tasks_for_user
    from whatsapp_standup import build_task_list_message, save_task_context
    from app import send_whatsapp_message

    today = today_ist()
    try:
        emp_data = _load_employees()
    except Exception:
        logger.exception("Morning standup prompt: failed to load employees.json")
        return

    for emp in emp_data.get("employees", []):
        wa = emp.get("whatsapp", "")
        if not wa:
            continue
        try:
            tasks, _ = _fetch_standup_tasks_for_user(emp["id"], today)
            if not tasks:
                continue
            msg = build_task_list_message(tasks, "Good morning! Today's tasks:")
            msg += (
                "\n\nReply \"1 done\", \"1,3 done\", or \"all done\" to mark complete.\n"
                "Reply \"add <task>\" to add something.\n"
                "Reply \"blocked: 2 <reason>\" to flag a blocker."
            )
            save_task_context(emp["id"], today, [t["id"] for t in tasks])
            send_whatsapp_message(wa, msg)
        except Exception:
            logger.exception(f"Morning standup prompt failed for {emp.get('id')}")
```

- [ ] **Step 2: Add the EOD nudge + escalation job**

```python
def send_eod_standup_reminders():
    """19:00 IST daily -- nudge anyone with incomplete tasks today, track
    consecutive missed evenings, and escalate to founders after 2 in a row."""
    from utils import _load_employees, today_ist
    from routes.ops import _fetch_standup_tasks_for_user
    from whatsapp_standup import build_task_list_message, save_task_context
    from app import send_whatsapp_message
    from db import get_connection

    today = today_ist()
    try:
        emp_data = _load_employees()
    except Exception:
        logger.exception("EOD standup reminder: failed to load employees.json")
        return

    employees = emp_data.get("employees", [])
    conn = get_connection()

    for emp in employees:
        wa = emp.get("whatsapp", "")
        if not wa:
            continue
        try:
            tasks, _ = _fetch_standup_tasks_for_user(emp["id"], today)
            incomplete = [t for t in tasks if t["status"] != "done"]

            if incomplete:
                msg = build_task_list_message(incomplete, "End of day check-in -- still open:")
                msg += "\n\nReply \"1 done\" etc. to update, or it'll carry over to tomorrow."
                save_task_context(emp["id"], today, [t["id"] for t in incomplete])
                send_whatsapp_message(wa, msg)

                with conn:
                    cur = conn.execute(
                        "SELECT consecutive_incomplete FROM whatsapp_reminder_state WHERE user_id=?",
                        (emp["id"],),
                    )
                    row = cur.fetchone()
                    new_count = (row[0] if row else 0) + 1
                    conn.execute(
                        "INSERT INTO whatsapp_reminder_state (user_id, consecutive_incomplete, last_checked_date) "
                        "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                        "consecutive_incomplete=excluded.consecutive_incomplete, last_checked_date=excluded.last_checked_date",
                        (emp["id"], new_count, today),
                    )

                if new_count == 2:
                    _escalate_missed_reminders(emp, incomplete, employees)
                    with conn:
                        conn.execute(
                            "UPDATE whatsapp_reminder_state SET consecutive_incomplete=0 WHERE user_id=?",
                            (emp["id"],),
                        )
            else:
                with conn:
                    conn.execute(
                        "INSERT INTO whatsapp_reminder_state (user_id, consecutive_incomplete, last_checked_date) "
                        "VALUES (?, 0, ?) ON CONFLICT(user_id) DO UPDATE SET "
                        "consecutive_incomplete=0, last_checked_date=excluded.last_checked_date",
                        (emp["id"], today),
                    )
        except Exception:
            logger.exception(f"EOD standup reminder failed for {emp.get('id')}")

    conn.close()


def _escalate_missed_reminders(emp: dict, incomplete: list, all_employees: list) -> None:
    """Ping every 'founder' (role contains 'founder', case-insensitive --
    matches 'Founder', 'Co-Founder', and the literal '1Founder' role string,
    see CLAUDE.md gotcha #80) that `emp` has missed 2 EOD checkins in a row."""
    from app import send_whatsapp_message

    titles = "\n".join(f"- {t['title']}" for t in incomplete)
    msg = (
        f"Heads up: {emp['name']} has had incomplete standup tasks for 2 evenings in a row.\n\n"
        f"Still open:\n{titles}"
    )
    for founder in all_employees:
        if "founder" in founder.get("role", "").lower() and founder.get("whatsapp"):
            if founder["id"] == emp["id"]:
                continue  # don't escalate someone to themselves
            send_whatsapp_message(founder["whatsapp"], msg)
```

- [ ] **Step 3: Add the weekly digest job**

```python
def send_weekly_standup_digest():
    """Monday 09:00 IST -- founders get a completed/open breakdown for the
    prior 7 days (Mon-Sun)."""
    from datetime import timedelta
    from utils import _load_employees, today_ist
    from routes.ops import get_weekly_completion_by_user
    from app import send_whatsapp_message

    today = date.fromisoformat(today_ist())
    since = (today - timedelta(days=7)).isoformat()

    try:
        emp_data = _load_employees()
    except Exception:
        logger.exception("Weekly standup digest: failed to load employees.json")
        return

    employees = emp_data.get("employees", [])
    emp_names = {e["id"]: e["name"] for e in employees}
    completion = get_weekly_completion_by_user(since)

    lines = [f"Weekly Standup Digest ({since} to {today_ist()})"]
    for user_id, counts in sorted(completion.items(), key=lambda kv: emp_names.get(kv[0], kv[0])):
        name = emp_names.get(user_id, user_id)
        marker = "OK" if counts["open"] == 0 else "!!"
        lines.append(f"{marker} {name}: {counts['completed']} done, {counts['open']} open")
    msg = "\n".join(lines)

    for founder in employees:
        if "founder" in founder.get("role", "").lower() and founder.get("whatsapp"):
            send_whatsapp_message(founder["whatsapp"], msg)
```

- [ ] **Step 4: Register the three jobs in `init_scheduler()`**

Modify `init_scheduler()` — add `from utils import IST` near the top of the function (or at module level with the other imports), and add three `scheduler.add_job(...)` calls after the existing `attendance_presence_sweep` job registration (before `scheduler.start()`):

```python
        # WhatsApp standup: morning prompt, EOD nudge, weekly founder digest.
        # All explicit timezone=IST -- unlike the pre-existing 08:00 job
        # above, do not rely on the server's local timezone here.
        from utils import IST
        scheduler.add_job(send_morning_standup_prompts, "cron", hour=10, minute=0,
                          timezone=IST, id="whatsapp_morning_standup", replace_existing=True)
        scheduler.add_job(send_eod_standup_reminders, "cron", hour=19, minute=0,
                          timezone=IST, id="whatsapp_eod_standup", replace_existing=True)
        scheduler.add_job(send_weekly_standup_digest, "cron", day_of_week="mon", hour=9, minute=0,
                          timezone=IST, id="whatsapp_weekly_digest", replace_existing=True)
```

- [ ] **Step 5: Scratch-test each job body against a temp DB with a mocked `send_whatsapp_message`**

Write and run a standalone script that:
1. Sets `os.environ["DB_PATH"]` to a temp file path before `import db` (same approach as Task 4 Step 4), seeds `employees.json`-shaped data via a monkeypatched `whatsapp_standup._load_employees`/`task_scheduler._load_employees` reference (don't touch the real `config/employees.json`), and seeds a few `standup_tasks` rows.
2. Monkeypatches `app.send_whatsapp_message` to append `(to, text)` tuples to a list instead of making a real HTTP call.
3. Calls `send_morning_standup_prompts()`, asserts exactly one message was "sent" per employee-with-tasks-and-a-whatsapp-number, and that `whatsapp_standup_context` now has a row for each.
4. Calls `send_eod_standup_reminders()` twice in a row for the same still-incomplete employee, asserts `whatsapp_reminder_state.consecutive_incomplete` reaches 2 on the second call and a founder-addressed message appears in the mocked-send list; asserts the counter resets to 0 after that.
5. Calls `send_weekly_standup_digest()`, asserts the message text contains every seeded employee's name and correct completed/open counts.
6. Prints `ALL GREEN`. Delete the temp DB.

- [ ] **Step 6: pyflakes + boot check**

Run: `cd backend && ../.venv/Scripts/python.exe -m pyflakes task_scheduler.py`
Expected: clean (or same baseline as before this change).

Run: `cd backend && ../.venv/Scripts/python.exe -c "import app; print('OK')"`
Expected: `OK` — confirms `init_scheduler(app)` (called from `app.py` at startup) doesn't blow up wiring the three new jobs.

- [ ] **Step 7: Commit**

```bash
git add backend/task_scheduler.py
git commit -m "Add WhatsApp standup scheduler jobs: 10am morning prompt, 7pm EOD nudge + escalation, Monday weekly founder digest"
```

---

### Task 8: Deploy and live-verify

**Files:** none (deploy + verification only)

- [ ] **Step 1: Push and deploy**

```bash
git push origin main
```
Wait for Railway to redeploy (per this project's established deploy-then-health-check convention), then:

Run: `curl -s https://lumina.mmga.agency/api/health`
Expected: healthy response, confirming the app boots in production with all new scheduler jobs registered (a scheduler wiring bug would typically surface as a startup crash, not a silent no-op).

- [ ] **Step 2: Live-verify the WhatsApp round trip**

Ask a real employee (with a `whatsapp` number in `employees.json`) to text the bot number `"add test standup task"`, confirm they get a confirmation reply and the task appears in `standup.html` for their account. Then have them reply `"1 done"` (or whatever number the confirmation listed) and confirm the task flips to done in `standup.html` too. Clean up the test task afterward via the UI's delete.

This cannot be verified locally — no way to exercise the real Meta Cloud API round-trip in dev (per this codebase's established note on the WhatsApp bot, CLAUDE.md gotcha #59).

- [ ] **Step 3: Confirm scheduled jobs are registered (don't wait for the actual clock time)**

Since 10am/7pm/Monday-9am won't align with when this is deployed, confirm registration rather than waiting hours: if there's a way to inspect APScheduler's running job list (e.g. a debug endpoint, or checking Railway logs at startup for the `"Task delay scheduler started"` log line plus confirming no exception followed it), do that. If no live-inspection path exists, this step is satisfied by Task 7 Step 6's clean `import app` boot check in production being the only available signal — note this limitation rather than fabricating a stronger verification.

---

## Notes for the implementer

- Every new function that iterates `employees.json` filters on `emp.get("whatsapp", "")` being truthy — `emp009`/`emp010`/`emp011` currently have empty `whatsapp` fields (see `config/employees.json`) and will simply be skipped by every job, not error. No special-casing needed.
- The founder-escalation and weekly-digest recipient set (`"founder" in role.lower()`) currently resolves to Vidit (Co-Founder), Kshitij (Founder), and Sid (1Founder) — verify this is the intended recipient list with the user before deploying Task 7, since it wasn't explicitly enumerated during brainstorming (the design doc calls this out but the concrete resulting name list is worth a sanity check).
- `db.py`'s `DB_PATH` is read from the `DB_PATH` env var at **module import time** (`DB_PATH = os.environ.get("DB_PATH", _default_db)`) — every scratch test in Tasks 4/6/7 must set `os.environ["DB_PATH"]` before the first `import db` in that script's process, not after.
