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
