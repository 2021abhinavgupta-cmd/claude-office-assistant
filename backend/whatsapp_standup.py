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

# People type "all done!" and "1 done." -- _DONE_RE is $-anchored, so without
# stripping this the message silently falls through to generic Claude chat
# instead of being recognized as the command it obviously is. Applied ONLY to
# the text matched against _DONE_RE -- _ADD_RE/_BLOCKED_RE already match
# trailing punctuation via their own `.+` capture group (DOTALL), so their
# captured free text is taken from the un-stripped original instead. Applying
# this strip to all three used to also eat trailing punctuation off of a
# `blocked: N <reason>` reason and an `add <title>` title, which is not
# cosmetic there -- it's the employee's actual sentence.
_TRAILING_PUNCT_RE = re.compile(r"[\s.!?…]+$")


def _normalize_phone(raw: str) -> str:
    """Digits-only, last 10 digits -- handles a stored number with/without
    '+', country code, or a 'whatsapp:' prefix from either side."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


_warned_malformed_numbers = False


def _warn_malformed_numbers(employees: list) -> None:
    """Logs once per process. A stored 'whatsapp' number that isn't a valid
    10-digit Indian mobile (optionally 91- or 0-prefixed) can never match an
    inbound sender in _normalize_phone()'s last-10-digits scheme -- that
    employee's replies silently fall through to generic chat with no error
    anywhere. Caught in practice: Nupur/emp002's stored '+91770085605' is 11
    digits, one short of either valid form. See CLAUDE.md gotcha #94 -- this
    doesn't fix her number (can't guess the missing digit), it just makes
    the problem visible instead of silent."""
    global _warned_malformed_numbers
    if _warned_malformed_numbers:
        return
    _warned_malformed_numbers = True
    for emp in employees:
        wa = emp.get("whatsapp", "")
        if not wa:
            continue
        digits = re.sub(r"\D", "", wa)
        valid = (
            len(digits) == 10
            or (len(digits) == 11 and digits.startswith("0"))
            or (len(digits) == 12 and digits.startswith("91"))
        )
        if not valid:
            logger.warning(
                "employees.json: %s's whatsapp number %r normalizes to %d digits -- "
                "not a valid 10-digit (or 0-/91-prefixed) number, so their WhatsApp "
                "standup replies will never be recognized.",
                emp.get("name", emp.get("id")), wa, len(digits),
            )


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
    employees = data.get("employees", [])
    _warn_malformed_numbers(employees)
    for emp in employees:
        wa = emp.get("whatsapp", "")
        if wa and _normalize_phone(wa) == sender_norm:
            return emp
    return None


def parse_standup_command(text: str) -> dict:
    """Deterministic command grammar -- see module docstring for why this
    is never AI-classified. Checked in order; first match wins.

    `done` is matched against a trailing-punctuation-stripped copy (so
    "all done!" / "1 done." parse); `add`/`blocked` are matched against the
    original text so their captured free text keeps whatever punctuation
    the employee actually typed."""
    original = (text or "").strip()
    stripped = _TRAILING_PUNCT_RE.sub("", original)

    m = _DONE_RE.match(stripped)
    if m:
        raw = m.group(1).strip()
        if raw.lower() == "all":
            return {"type": "done", "numbers": "all"}
        numbers = [int(n.strip()) for n in raw.split(",") if n.strip()]
        return {"type": "done", "numbers": numbers}

    m = _BLOCKED_RE.match(original)
    if m:
        return {"type": "blocked", "number": int(m.group(1)), "reason": m.group(2).strip()}

    m = _ADD_RE.match(original)
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


def _fetch_task_titles(task_ids: list) -> dict:
    """standup_tasks.id -> title, for echoing real titles back in a
    confirmation. Deliberately a direct lookup rather than
    _fetch_standup_tasks_for_user(), which is NOT read-only (it materializes
    carry-over rows and runs the Notion Creation-Date self-heal) -- merely
    confirming a reply should never trigger those writes."""
    if not task_ids:
        return {}
    from db import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        placeholders = ",".join("?" * len(task_ids))
        cur.execute(
            f"SELECT id, title FROM standup_tasks WHERE id IN ({placeholders})",
            list(task_ids),
        )
        return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def _task_label(number: int, task_id: int, titles: dict) -> str:
    """'#2 Draft the brief' -- always echo the TITLE alongside the number.
    The numbering legitimately drifts between the 10am full list and the 7pm
    renumbered incomplete-only list, so the title is the employee's only way
    to notice they just marked the wrong task."""
    title = titles.get(task_id)
    return f"#{number} {title}" if title else f"#{number}"


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


def handle_standup_message(employee: dict, text: str) -> Optional[tuple]:
    """Returns (reply_text, context_to_save) for a recognized standup
    command, or None if `text` didn't match one (caller falls through to
    generic chat). `context_to_save` is either None (nothing to persist --
    'done'/'blocked' don't change what the numbers refer to) or an
    (user_id, date_str, task_ids) tuple the caller must pass to
    save_task_context() -- but ONLY after confirming reply_text was
    actually delivered.

    This mirrors the "send first, persist only if it went out" rule the
    10am/7pm scheduler jobs already follow (see task_scheduler.py): the
    'add' command builds a NEW numbered list, and if the confirmation
    reply fails to send, the employee never saw that new numbering -- a
    later 'N done' must still resolve against whatever list they actually
    have on their screen, not one persisted here on the assumption the
    send would succeed."""
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
            return ("I don't have a task list on file for you today yet -- you'll get one at 10am, or text \"add <task>\" to start one now.", None)

        if cmd["numbers"] == "all":
            indices = list(range(1, len(context_ids) + 1))
        else:
            indices = cmd["numbers"]

        out_of_range = [n for n in indices if n < 1 or n > len(context_ids)]
        if out_of_range:
            return (f"No task(s) numbered {', '.join(str(n) for n in out_of_range)} -- your list only has 1-{len(context_ids)}. Nothing was changed.", None)

        titles = _fetch_task_titles(context_ids)
        marked = []
        failed = []
        for n in indices:
            task_id = context_ids[n - 1]
            label = _task_label(n, task_id, titles)
            result = _apply_standup_task_update(task_id, status="done", progress=100)
            if "error" not in result:
                marked.append(label)
            else:
                # Name what failed -- silently omitting it from the "marked"
                # list reads as if nothing was attempted for that number.
                failed.append(f"{label} ({result['error']})")

        if not marked and not failed:
            return ("Couldn't mark those done -- something went wrong. Try again or use the app.", None)

        segments = []
        if marked:
            segments.append("Marked done: " + "; ".join(marked))
        if failed:
            segments.append("Failed: " + "; ".join(failed))
        return ("\n".join(segments), None)

    if cmd["type"] == "add":
        result = _smart_add_standup_task_impl(user_id=user_id, assigned_to=user_id, title=cmd["title"])
        if "error" in result:
            return (f"Couldn't add that task: {result['error']}", None)
        tasks, _ = _fetch_standup_tasks_for_user(user_id, today)
        listing = build_task_list_message(tasks, "Added. Today's list:")
        context_to_save = (user_id, today, [t["id"] for t in tasks])
        return (f"Added: {cmd['title']}\n\n{listing}", context_to_save)

    if cmd["type"] == "blocked":
        # Same guard the 'done' branch has -- without it an empty context
        # falls through to the range check and emits "your list only has 1-0".
        if not context_ids:
            return ("I don't have a task list on file for you today yet -- you'll get one at 10am, or text \"add <task>\" to start one now.", None)

        n = cmd["number"]
        if n < 1 or n > len(context_ids):
            return (f"No task numbered {n} -- your list only has 1-{len(context_ids)}. Nothing was changed.", None)
        task_id = context_ids[n - 1]
        titles = _fetch_task_titles(context_ids)
        label = _task_label(n, task_id, titles)
        result = _apply_standup_task_update(task_id, blocker=cmd["reason"])
        if "error" in result:
            return (f"Couldn't flag that as blocked: {result['error']}", None)
        return (f"Flagged blocked: {label} -- {cmd['reason']}", None)

    return None
