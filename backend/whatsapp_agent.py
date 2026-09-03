"""
whatsapp_agent.py — Tool-using Claude agent for the WhatsApp channel.

An inbound WhatsApp message from a known sender is answered by Claude with
tool access to the agency's live CRM (Notion clients/tasks, with a SQLite
fallback) and the project knowledge base (FTS5). The flow is:

    identify sender  →  give it only the tools that sender is allowed to use
                     →  run a short tool-use loop  →  send back a short reply

Identity (never cached — read live every message, see CLAUDE.md gotcha #63):
  - employees.json `whatsapp` field  → full read access (every employee is
    admin-tier in this app, see CLAUDE.md gotcha #60)
  - client_users.whatsapp (optional) → scoped to that client's own tasks only
  - unknown number                   → polite "not recognized", no AI spend

This module must not import `app` (app imports it). It talks to
model_router / budget_tracker / notion_store / project_store / db / utils
directly, and owns its own Anthropic client.
"""

from __future__ import annotations

import os
import re
import json
import logging
from datetime import datetime, timedelta, timezone

import anthropic

from model_router import get_model_for_task, calculate_cost
from budget_tracker import check_budget_available, record_usage
from db import get_connection
import notion_store
import kb_retriever
import semantic_kb
import utils

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Rolling per-sender context. Older than this and we start a fresh thread.
_CONTEXT_TTL_HOURS = 6
_CONTEXT_MAX_TURNS = 6          # user/assistant pairs kept between messages
_MAX_TOOL_ROUNDS = 8           # hard cap on the tool-use loop (pause_turn can eat rounds)

_IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


# ── Identity ─────────────────────────────────────────────────────────────────

def _normalize_phone(raw: str) -> str:
    """Digits-only, last 10 — tolerates '+', country code, 'whatsapp:' prefix
    on either side. Same scheme as whatsapp_standup.py so the two stay
    interchangeable if merged."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def identify_sender(sender: str) -> dict:
    """Returns one of:
      {"kind": "employee", "id", "name", "role"}
      {"kind": "client", "client_id", "client_name", "client_notion_id"}
      {"kind": "unknown"}
    """
    norm = _normalize_phone(sender)
    if not norm:
        return {"kind": "unknown"}

    # Employees
    try:
        for emp in utils._load_employees().get("employees", []):
            wa = emp.get("whatsapp", "")
            if wa and _normalize_phone(wa) == norm:
                return {
                    "kind": "employee",
                    "id": emp.get("id", ""),
                    "name": emp.get("name", "there"),
                    "role": emp.get("role", ""),
                }
    except Exception:
        logger.exception("whatsapp_agent: employee lookup failed")

    # Clients (client_users.whatsapp — optional column, may not be populated)
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, client_name, client_notion_id, whatsapp FROM client_users"
        )
        rows = cur.fetchall()
        conn.close()
        for r in rows:
            wa = r[3] if len(r) > 3 else ""
            if wa and _normalize_phone(wa) == norm:
                return {
                    "kind": "client",
                    "client_id": r[0],
                    "client_name": r[1] or "",
                    "client_notion_id": r[2] or "",
                }
    except Exception:
        # `whatsapp` column missing on an old DB, etc. — treat as no client match
        logger.debug("whatsapp_agent: client lookup skipped/failed", exc_info=True)

    return {"kind": "unknown"}


# ── Group allow-list ────────────────────────────────────────────────────────
#
# The bridge only forwards a group message when the bot was addressed (a
# "@lumina ..." prefix, an @-mention, or a reply to one of its messages).
# Even then we only act if the group is on this allow-list AND the person
# who asked is a known employee — because everyone in the group sees the
# reply, so a client or a stranger in the room must never be able to pull
# internal data out of it. Allow-list a group only when you know every
# member is staff.
#
# Sources, merged: env WHATSAPP_GROUP_ALLOWLIST (comma/space separated) and
# the app_settings key 'whatsapp_group_allowlist' (a JSON array, editable
# at runtime via POST /api/whatsapp/groups). Group ids are numeric, so we
# compare digits-only and tolerate a full '...@g.us' jid either way.

def _norm_group(x: str) -> str:
    return re.sub(r"\D", "", str(x or ""))


def _group_allowlist() -> set:
    out: set = set()
    for tok in re.split(r"[,\s]+", os.getenv("WHATSAPP_GROUP_ALLOWLIST", "")):
        n = _norm_group(tok)
        if n:
            out.add(n)
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key='whatsapp_group_allowlist'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            for tok in json.loads(row[0]):
                n = _norm_group(tok)
                if n:
                    out.add(n)
    except Exception:
        logger.debug("whatsapp_agent: group allowlist load failed", exc_info=True)
    return out


def _group_allowed(group_id: str) -> bool:
    return _norm_group(group_id) in _group_allowlist()


# ── Per-sender conversation context ──────────────────────────────────────────

def _load_context(sender: str) -> list:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT messages, updated_at FROM whatsapp_agent_context WHERE sender=?",
            (sender,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return []
        updated = row[1]
        if updated:
            try:
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - ts > timedelta(hours=_CONTEXT_TTL_HOURS):
                    return []
            except ValueError:
                pass
        msgs = json.loads(row[0] or "[]")
        return msgs if isinstance(msgs, list) else []
    except Exception:
        logger.debug("whatsapp_agent: context load failed", exc_info=True)
        return []


def _save_context(sender: str, messages: list) -> None:
    # Keep only plain text user/assistant turns — never persist tool scaffolding.
    trimmed = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if isinstance(m.get("content"), str) and m.get("role") in ("user", "assistant")
    ][-(_CONTEXT_MAX_TURNS * 2):]
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                """INSERT INTO whatsapp_agent_context (sender, messages, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(sender) DO UPDATE SET messages=excluded.messages,
                                                    updated_at=excluded.updated_at""",
                (sender, json.dumps(trimmed), datetime.now(timezone.utc).isoformat()),
            )
        conn.close()
    except Exception:
        logger.debug("whatsapp_agent: context save failed", exc_info=True)


# ── Data access (Notion first, SQLite fallback) ─────────────────────────────

def _sqlite_clients() -> list:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, status FROM clients ORDER BY created_at DESC")
        rows = cur.fetchall()
        conn.close()
        return [{"name": r[0], "status": r[1] or ""} for r in rows]
    except Exception:
        return []


def _sqlite_tasks_for_client(client_name: str) -> list:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """SELECT t.title, t.status, t.assigned_to, t.due_date
               FROM tasks t JOIN clients c ON t.client_id = c.id
               WHERE lower(c.name) = lower(?)
               ORDER BY t.due_date""",
            (client_name,),
        )
        rows = cur.fetchall()
        conn.close()
        return [
            {"title": r[0], "status": r[1] or "", "assigned_to": r[2] or "", "due_date": r[3] or ""}
            for r in rows
        ]
    except Exception:
        return []


def _all_clients() -> list:
    if notion_store.is_configured():
        try:
            return [
                {"name": c.get("name", ""), "status": c.get("status", "")}
                for c in notion_store.list_clients()
            ]
        except Exception:
            logger.exception("whatsapp_agent: notion list_clients failed")
    return _sqlite_clients()


def _find_client(name: str) -> dict | None:
    name_l = (name or "").strip().lower()
    if not name_l:
        return None
    if notion_store.is_configured():
        try:
            for c in notion_store.list_clients():
                if (c.get("name", "") or "").strip().lower() == name_l:
                    return c
            # loose contains-match fallback
            for c in notion_store.list_clients():
                if name_l in (c.get("name", "") or "").strip().lower():
                    return c
        except Exception:
            logger.exception("whatsapp_agent: notion client resolve failed")
    return None


def _tasks_for_client(client_name: str, client_notion_id: str = "") -> list:
    if notion_store.is_configured():
        try:
            if not client_notion_id:
                c = _find_client(client_name)
                client_notion_id = (c or {}).get("notion_id", "")
            if client_notion_id:
                return notion_store.list_tasks(client_notion_id=client_notion_id)
        except Exception:
            logger.exception("whatsapp_agent: notion list_tasks (client) failed")
    return _sqlite_tasks_for_client(client_name)


def _standup_tasks_today(user_id: str) -> list:
    """The rows on this person's daily standup for today — the live task
    list they see in Lumina's Standup screen (NOT the whole Notion board)."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT title, status, blocker, carried_from FROM standup_tasks "
            "WHERE user_id=? AND date=? AND status NOT IN ('deleted','delegated') "
            "ORDER BY id",
            (user_id, _today_ist()),
        )
        rows = cur.fetchall()
        conn.close()
        return [
            {"title": r[0], "status": (r[1] or "pending"),
             "blocker": (r[2] or ""), "carried_from": (r[3] or "")}
            for r in rows
        ]
    except Exception:
        logger.exception("whatsapp_agent: standup task lookup failed")
        return []


def _active_employees() -> list:
    """[{id, name, whatsapp}] for every active employee — the internal roster."""
    out = []
    try:
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in (
                "inactive", "disabled", "left", "removed", "archived", "former"
            ):
                continue
            out.append({"id": e.get("id", ""),
                        "name": e.get("name") or e.get("id", ""),
                        "whatsapp": e.get("whatsapp", "") or ""})
    except Exception:
        logger.exception("whatsapp_agent: roster load failed")
    return out


def _wa_jid(raw: str) -> str:
    """A WhatsApp DM JID from a stored number ('+91 97029 08716' -> ...)."""
    d = re.sub(r"\D", "", raw or "")
    return f"{d}@s.whatsapp.net" if d else ""


def _team_group_jid() -> str:
    """The team WhatsApp group to post to for 'send this to the group'.
    Explicit app_settings key 'whatsapp_team_group' wins; otherwise the
    sole allow-listed group (the common case)."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key='whatsapp_team_group'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            d = _norm_group(row[0])
            if d:
                return f"{d}@g.us"
    except Exception:
        logger.debug("whatsapp_agent: team group lookup failed", exc_info=True)
    gl = sorted(_group_allowlist())
    return f"{gl[0]}@g.us" if gl else ""


def _enqueue_outbound(jid: str, text: str) -> bool:
    """Queue a proactive WhatsApp message. The laptop companion polls
    /api/companion/whatsapp-outbox and delivers it via the local bridge —
    the Railway backend can't reach the bridge directly."""
    if not jid or not text:
        return False
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO whatsapp_outbox (to_number, body, created_at) VALUES (?, ?, ?)",
                (jid, text[:1500], datetime.now(timezone.utc).isoformat()),
            )
        conn.close()
        return True
    except Exception:
        logger.exception("whatsapp_agent: outbox enqueue failed")
        return False


def _resolve_employee(name: str) -> dict | None:
    """Best-effort name -> {id, name}. Exact (case-insensitive) first, then a
    first-name / substring match. None if nothing sensible matches."""
    n = (name or "").strip().lower()
    if not n:
        return None
    roster = _active_employees()
    for e in roster:
        if e["name"].strip().lower() == n:
            return e
    for e in roster:
        first = e["name"].strip().lower().split()[0] if e["name"].strip() else ""
        if first == n or n in e["name"].strip().lower() or (first and first.startswith(n)):
            return e
    return None


def _format_standup(tasks: list, who: str) -> str:
    """Shared renderer for a person's daily standup (used by get_my_tasks and
    get_teammate_tasks). `who` is 'Your' or a name like 'Nupur's'. Kept
    dash-free so the model doesn't echo bullet punctuation."""
    if not tasks:
        return f"{who} standup for today is empty."
    done_words = ("done", "completed", "complete")
    lines = []
    for t in tasks:
        is_done = str(t["status"]).lower() in done_words
        prefix = "done: " if is_done else ""
        tail = ""
        if t.get("carried_from"):
            tail += f" (carried over from {t['carried_from']})"
        if t.get("blocker"):
            tail += f" (blocked by {t['blocker']})"
        # leading "- " is kept in group chats, stripped in DMs by _humanize
        lines.append(f"- {prefix}{t['title']}{tail}")
    done = sum(1 for t in tasks if str(t["status"]).lower() in done_words)
    head = f"{who} standup today, {done} done and {len(tasks) - done} to go:"
    return head + "\n" + "\n".join(lines)


def _match_standup_task(user_id: str, query: str) -> dict | None:
    """Find one of today's standup rows for this user by a loose description
    ('whatsapp bot testing' -> 'testing of the whatsapp bot'). Returns
    {id, title, status, notion_id} or None."""
    q = (query or "").strip().lower()
    if not q:
        return None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, title, status, notion_id, blocker FROM standup_tasks "
            "WHERE user_id=? AND date=? AND status NOT IN ('deleted','delegated') "
            "ORDER BY id",
            (user_id, _today_ist()),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        logger.exception("whatsapp_agent: standup match query failed")
        return None
    cand = [{"id": r[0], "title": r[1] or "", "status": (r[2] or "pending"),
             "notion_id": r[3], "blocker": (r[4] or "")} for r in rows]
    for c in cand:
        if c["title"].strip().lower() == q:
            return c
    for c in cand:
        tl = c["title"].lower()
        if q in tl or (len(tl) > 4 and tl in q):
            return c
    qt = set(re.findall(r"\w+", q))
    best, score = None, 0
    for c in cand:
        ct = set(re.findall(r"\w+", c["title"].lower()))
        s = len(qt & ct)
        if s > score:
            best, score = c, s
    return best if score >= 2 else None


def _kb_search(query: str, limit: int = 6) -> list:
    """Search the whole knowledge base, no project/user scoping (every
    employee is admin-tier here). Keyword (bm25) always; semantic hits
    folded in ahead of it when that layer is enabled."""
    hits: list = []
    try:
        for h in semantic_kb.search(query, limit=max(3, limit // 2)):
            hits.append({"filename": h.get("filename"), "chunk": h.get("chunk")})
    except Exception:
        logger.debug("whatsapp_agent: semantic kb search failed", exc_info=True)

    mq = kb_retriever._fts_query(query)
    if mq:
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                """SELECT filename, chunk, bm25(kb_chunks_fts) AS score
                   FROM kb_chunks_fts
                   WHERE kb_chunks_fts MATCH ?
                   ORDER BY score
                   LIMIT ?""",
                (mq, int(limit)),
            )
            rows = cur.fetchall()
            conn.close()
            hits.extend({"filename": r[0], "chunk": r[1]} for r in rows)
        except Exception:
            logger.debug("whatsapp_agent: kb search failed", exc_info=True)

    seen, out = set(), []
    for h in hits:
        key = (h.get("filename"), (h.get("chunk") or "")[:140])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out[:limit]


# ── Formatting helpers ──────────────────────────────────────────────────────

def _clip(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _humanize(reply: str, *, bullets: bool = False) -> str:
    """Tidy a reply. Always turns em/en dashes used as punctuation into
    commas (so it doesn't read like a press release). When `bullets` is
    False it also strips line-leading bullet characters and spaced-hyphen
    punctuation; when True (group chats) it leaves '- ' list markers alone.
    Hyphens inside words (reverse-engg) are never touched."""
    lines = []
    for ln in (reply or "").split("\n"):
        if not bullets:
            ln = re.sub(r"^(\s*)(?:[-*•‣▪]|\d+[.)])\s+", r"\1", ln)
        lines.append(ln)
    out = "\n".join(lines)
    out = re.sub(r"(?<=\S) *[—–] *(?=\S)", ", ", out)      # word — word -> word, word
    out = out.replace("—", ", ").replace("–", ", ")
    if not bullets:
        out = re.sub(r"(?<=\S) +- +(?=\S)", ", ", out)     # spaced hyphen -> comma
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" +([,.!?;:])", r"\1", out)
    return out.strip()


def _fmt_task_line(t: dict, *, include_assignee: bool = True, detail: bool = False) -> str:
    bits = [t.get("title") or "(untitled)"]
    if t.get("type"):
        bits.append(str(t["type"]))
    if t.get("status"):
        bits.append(str(t["status"]))
    if include_assignee and t.get("assigned_to"):
        bits.append("→ " + str(t["assigned_to"]))
    if t.get("due_date"):
        bits.append("due " + str(t["due_date"]))
    line = " | ".join(bits)
    if not detail:
        return line
    # the Sheets/content-calendar fields, so WhatsApp can answer "what's the
    # caption for X" / "what's the script for Y" without opening Lumina
    extra = []
    if t.get("creation_date"):
        extra.append(f"  start: {t['creation_date']}")
    for label, key, n in (
        ("brief", "brief", 200), ("content", "content", 240), ("idea", "idea", 240),
        ("script", "scripts_copy", 500), ("caption", "caption", 400),
    ):
        v = _clip(t.get(key), n)
        if v:
            extra.append(f"  {label}: {v}")
    if t.get("file_link"):
        extra.append(f"  file: {t['file_link']}")
    return line + ("\n" + "\n".join(extra) if extra else "")


def _is_overdue(due: str, today: str) -> bool:
    return bool(due) and re.match(r"^\d{4}-\d{2}-\d{2}$", str(due)) and str(due) < today


# ── Tools ───────────────────────────────────────────────────────────────────

_EMPLOYEE_TOOLS = [
    {
        "name": "get_clients",
        "description": "List all of the agency's clients and their current status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_client_tasks",
        "description": "List a client's tasks/deliverables AND their full content "
                       "calendar (the Lumina 'Sheets' view): post type, status, "
                       "assignee, dates, brief, content, idea, script/copy, "
                       "caption and file link. Use this for questions like 'what's "
                       "the caption for X', 'what's the script for Y's reel', "
                       "'what's scheduled this week for Z'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {"type": "string", "description": "The client's name."}
            },
            "required": ["client_name"],
        },
    },
    {
        "name": "get_my_tasks",
        "description": "The person's live daily-standup task list for today "
                       "(what shows on their Lumina Standup screen) — with done "
                       "/ pending status, blockers and carried-over items. This "
                       "is the ONLY source for 'what are my tasks' — do not use "
                       "the wider Notion board for that.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_standup_task",
        "description": "Add a task to the person's daily standup (today's task "
                       "list) in Lumina. Use this when they tell you something "
                       "they're working on / want on their list for today.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "The task text, roughly as they said it."}
            },
            "required": ["task"],
        },
    },
    {
        "name": "update_standup_task",
        "description": "Mark one of the person's standup tasks for today as "
                       "done, or reopen it. Use for 'mark X as done', 'X is "
                       "complete', 'finished X', 'reopen X'. Match the task by "
                       "a few words from what they call it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "A few words identifying the task."},
                "status": {"type": "string", "enum": ["done", "pending"],
                           "description": "done (default) or pending to reopen."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "assign_task",
        "description": "Add a NEW task to another team member's daily standup "
                       "for today. Use for 'add X to Nupur's list', 'put X on "
                       "Kshitij's standup', 'get Happy to do X'. This creates a "
                       "fresh task on their standup; it does NOT move one of "
                       "yours. Works the same in a group chat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The teammate to add the task for."},
                "task": {"type": "string",
                         "description": "The task text, roughly as it was said."},
            },
            "required": ["name", "task"],
        },
    },
    {
        "name": "delegate_my_task",
        "description": "Hand one of YOUR OWN standup tasks for today over to "
                       "another team member. It comes off your list and goes "
                       "onto theirs. Use for 'give my X task to Nupur', "
                       "'delegate X to Happy', 'pass X to Kshitij'. Match the "
                       "task by a few words from what you call it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "A few words identifying which of your "
                                        "tasks to hand over."},
                "name": {"type": "string",
                         "description": "The teammate to hand it to."},
            },
            "required": ["task", "name"],
        },
    },
    {
        "name": "send_group_message",
        "description": "Post a message into the team's WhatsApp group on this "
                       "person's behalf. Use when they say 'send/post/text the "
                       "group ...', 'tell everyone ...', 'announce ... in the "
                       "group', 'message the group to ...'. Compose the message "
                       "the way they asked for it. Only works from a private "
                       "chat, not from inside the group.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": "The exact message to post in the group."}
            },
            "required": ["message"],
        },
    },
    {
        "name": "remind_teammate",
        "description": "Send another team member a WhatsApp nudge about something "
                       "— 'remind Nupur to finish the deck', 'ping Happy about the "
                       "edit', 'tell Kshitij to reply to the client'. They get a "
                       "direct message from Lumina saying it came from you. Only "
                       "works in a private chat with you, not from the group.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The teammate to nudge."},
                "message": {"type": "string",
                            "description": "What to remind them about, in your words."},
            },
            "required": ["name", "message"],
        },
    },
    {
        "name": "get_teammate_tasks",
        "description": "Another team member's live daily-standup task list for "
                       "today — use this for 'what is Nupur working on', 'what "
                       "are Kshitij's tasks', 'is Happy doing anything today'. "
                       "Everyone on the team can see everyone else's standup.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The teammate's name or first name."}
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_team_standup",
        "description": "A roll-up of the whole team's daily standup for today — "
                       "each person and what's on their list. Use for 'what's "
                       "the team doing today', 'who's working on what'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_knowledge_base",
        "description": "Search the agency's uploaded documents / knowledge base "
                       "(briefs, notes, brand guidelines, strategy docs) for a "
                       "phrase or topic. Returns matching excerpts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_team_overview",
        "description": "A quick snapshot: number of clients, open tasks, and "
                       "overdue tasks across the whole agency.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # Anthropic-hosted web search — Claude runs the query server-side and
    # gets cited results. Same tool spec the chat stream endpoint uses.
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 4},
]

_CLIENT_TOOLS = [
    {
        "name": "get_my_tasks",
        "description": "List your deliverables: status, due date, and (for social "
                       "content) the post type, brief, content, idea, script/copy, "
                       "caption and file link.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _run_tool(name: str, tool_input: dict, identity: dict,
              in_group: bool = False) -> str:
    today = _today_ist()
    kind = identity["kind"]

    try:
        if name == "get_clients" and kind == "employee":
            clients = _all_clients()
            if not clients:
                return "No clients found."
            return "\n".join(
                f"- {c['name']}" + (f" ({c['status']})" if c.get("status") else "")
                for c in clients
            )

        if name == "get_client_tasks" and kind == "employee":
            cname = (tool_input or {}).get("client_name", "")
            tasks = _tasks_for_client(cname)
            if not tasks:
                return f"No tasks found for '{cname}'. Check the client name with get_clients."
            shown = tasks[:15]
            head = f"Tasks for {cname}"
            if len(tasks) > len(shown):
                head += f" (first {len(shown)} of {len(tasks)} — ask about a specific post for the rest)"
            return head + ":\n" + "\n".join(_fmt_task_line(t, detail=True) for t in shown)

        if name == "update_standup_task" and kind == "employee":
            q = (tool_input or {}).get("task", "")
            new_status = str((tool_input or {}).get("status", "done")).strip().lower()
            if new_status in ("complete", "completed", "finished"):
                new_status = "done"
            if new_status not in ("done", "pending"):
                new_status = "done"
            m = _match_standup_task(identity["id"], q)
            if not m:
                have = _standup_tasks_today(identity["id"])
                names = "; ".join(t["title"] for t in have) or "nothing yet"
                return f"Couldn't find a task matching '{q}'. Today's list: {names}."
            try:
                conn = get_connection()
                with conn:
                    conn.execute("UPDATE standup_tasks SET status=? WHERE id=?",
                                 (new_status, m["id"]))
                conn.close()
            except Exception:
                logger.exception("whatsapp_agent: update_standup_task failed")
                return "(couldn't update that just now)"
            # mirror board-linked rows to Notion, matching the Standup screen
            # (social-media tasks go to 'need approval', not straight to done)
            if m.get("notion_id"):
                try:
                    ttype = (notion_store.get_task_type(m["notion_id"]) or "").lower()
                    if new_status == "done":
                        nst = "need_for_approval" if "social" in ttype else "done"
                    else:
                        nst = "in_progress"
                    notion_store.update_task(m["notion_id"], status=nst)
                except Exception:
                    logger.debug("whatsapp_agent: notion status mirror failed",
                                 exc_info=True)
            return (f"Marked done: {m['title']}" if new_status == "done"
                    else f"Reopened: {m['title']}")

        if name == "get_teammate_tasks" and kind == "employee":
            who = (tool_input or {}).get("name", "")
            emp = _resolve_employee(who)
            if not emp:
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Don't know who '{who}' is. Team: {names}."
            tasks = _standup_tasks_today(emp["id"])
            return _format_standup(tasks, f"{emp['name']}'s")

        if name == "get_team_standup" and kind == "employee":
            out = []
            for e in _active_employees():
                tasks = _standup_tasks_today(e["id"])
                if not tasks:
                    out.append(f"{e['name']}: nothing on standup")
                    continue
                done_words = ("done", "completed", "complete")
                bits = []
                for t in tasks[:6]:
                    d = " (done)" if str(t["status"]).lower() in done_words else ""
                    bits.append(f"{t['title']}{d}")
                extra = f" plus {len(tasks) - 6} more" if len(tasks) > 6 else ""
                out.append(f"{e['name']}: " + ", ".join(bits) + extra)
            return "Team standup today:\n" + "\n".join(out)

        if name == "get_my_tasks":
            if kind == "employee":
                tasks = _standup_tasks_today(identity["id"])
                if not tasks:
                    return ("Nothing on your standup for today yet. Tell me what "
                            "you're working on and I'll add it.")
                return _format_standup(tasks, "Your")
            if kind == "client":
                # Require a real linked client id — never fall back to
                # name matching for a client, that risks returning another
                # client's tasks (see _find_client's loose contains-match).
                cnid = identity.get("client_notion_id", "")
                if not cnid:
                    return ("Your account isn't fully linked yet — ask the "
                            "team to connect it so I can pull up your work.")
                tasks = _tasks_for_client(identity["client_name"], cnid)
                if not tasks:
                    return "You have no deliverables listed right now."
                shown = tasks[:15]
                head = "Your deliverables"
                if len(tasks) > len(shown):
                    head += f" (first {len(shown)} of {len(tasks)})"
                return head + ":\n" + "\n".join(
                    _fmt_task_line(t, include_assignee=False, detail=True) for t in shown
                )

        if name == "add_standup_task" and kind == "employee":
            task = (tool_input or {}).get("task", "").strip()
            if not task:
                return "(no task text — ask them what to add)"
            try:
                conn = get_connection()
                with conn:
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title) VALUES (?, ?, ?)",
                        (identity["id"], today, task[:500]),
                    )
                conn.close()
                return f"Added to today's standup: {task[:120]}"
            except Exception:
                logger.exception("whatsapp_agent: add_standup_task failed")
                return "(couldn't add that to the standup just now)"

        if name == "assign_task" and kind == "employee":
            who = (tool_input or {}).get("name", "")
            task = (tool_input or {}).get("task", "").strip()
            emp = _resolve_employee(who)
            if not emp:
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Don't know who '{who}' is. Team: {names}."
            if not task:
                return "(no task text — ask what to add for them)"
            if emp["id"] == identity["id"]:
                return ("That's your own list — use add_standup_task for that. "
                        "Pick a teammate to assign to.")
            try:
                conn = get_connection()
                with conn:
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, delegated_from) "
                        "VALUES (?, ?, ?, 'pending', ?)",
                        (emp["id"], today, task[:500], identity["name"]),
                    )
                conn.close()
            except Exception:
                logger.exception("whatsapp_agent: assign_task failed")
                return "(couldn't add that to their standup just now)"
            jid = _wa_jid(emp.get("whatsapp"))
            notified = _enqueue_outbound(
                jid, f"{identity['name']} put a task on your standup for today: "
                     f"{task[:400]}"
            ) if jid else False
            tail = "" if notified else " (they'll see it on their standup; no WhatsApp number on file to ping them)"
            return f"Added to {emp['name']}'s standup for today: {task[:120]}." + tail

        if name == "delegate_my_task" and kind == "employee":
            who = (tool_input or {}).get("name", "")
            q = (tool_input or {}).get("task", "")
            emp = _resolve_employee(who)
            if not emp:
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Don't know who '{who}' is. Team: {names}."
            if emp["id"] == identity["id"]:
                return "That's you — pick a different teammate to hand it to."
            m = _match_standup_task(identity["id"], q)
            if not m:
                have = _standup_tasks_today(identity["id"])
                names = "; ".join(t["title"] for t in have) or "nothing yet"
                return f"Couldn't find one of your tasks matching '{q}'. Today's list: {names}."
            try:
                conn = get_connection()
                with conn:
                    conn.execute(
                        "UPDATE standup_tasks SET status='delegated', delegated_to=? WHERE id=?",
                        (emp["name"], m["id"]),
                    )
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, blocker, delegated_from) "
                        "VALUES (?, ?, ?, 'pending', ?, ?)",
                        (emp["id"], today, m["title"][:500],
                         (m.get("blocker") or None), identity["name"]),
                    )
                conn.close()
            except Exception:
                logger.exception("whatsapp_agent: delegate_my_task failed")
                return "(couldn't hand that over just now)"
            jid = _wa_jid(emp.get("whatsapp"))
            note = f"{identity['name']} handed you a task for today: {m['title'][:400]}"
            if m.get("blocker"):
                note += f" (blocked by {m['blocker']})"
            _enqueue_outbound(jid, note) if jid else None
            return (f"Handed '{m['title']}' to {emp['name']}. Off your list, "
                    "on theirs for today.")

        if name == "send_group_message" and kind == "employee":
            if in_group:
                return "We're already in the group, so just say it here."
            msg = (tool_input or {}).get("message", "").strip()
            if not msg:
                return "(no message text — ask what to post)"
            gjid = _team_group_jid()
            if not gjid:
                return ("No team group is set up for me to post to. Add one "
                        "with the whatsapp_team_group setting or the group "
                        "allow-list.")
            if _enqueue_outbound(gjid, msg[:1500]):
                return f"Posted to the group: {msg[:180]}"
            return "(couldn't queue that group message just now)"

        if name == "remind_teammate" and kind == "employee":
            if in_group:
                return ("Reminders only work from our private chat, not the "
                        "group. Message me directly and I'll ping them.")
            who = (tool_input or {}).get("name", "")
            msg = (tool_input or {}).get("message", "").strip()
            emp = _resolve_employee(who)
            if not emp:
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Don't know who '{who}' is. Team: {names}."
            if not msg:
                return "(no reminder text — ask what to remind them about)"
            if emp["id"] == identity["id"]:
                return "That's you — no need to remind yourself through me."
            jid = _wa_jid(emp.get("whatsapp"))
            if not jid:
                return f"{emp['name']} has no WhatsApp number on file, so I can't reach them."
            if _enqueue_outbound(jid, f"Reminder from {identity['name']}: {msg[:600]}"):
                return f"Sent {emp['name']} a reminder about that."
            return "(couldn't queue that reminder just now)"

        if name == "search_knowledge_base" and kind == "employee":
            q = (tool_input or {}).get("query", "")
            hits = _kb_search(q)
            if not hits:
                return f"Nothing in the knowledge base matched '{q}'."
            out = []
            for h in hits:
                snippet = " ".join((h.get("chunk") or "").split())[:400]
                out.append(f"[{h.get('filename') or 'doc'}] {snippet}")
            return "\n\n".join(out)

        if name == "get_team_overview" and kind == "employee":
            clients = _all_clients()
            open_tasks = overdue = 0
            if notion_store.is_configured():
                try:
                    all_tasks = notion_store.list_tasks()
                except Exception:
                    all_tasks = []
                for t in all_tasks:
                    st = (t.get("status") or "").lower()
                    if st in ("done", "approved", "posted", "final", "need_for_approval"):
                        continue
                    open_tasks += 1
                    if _is_overdue(t.get("due_date"), today):
                        overdue += 1
            return (
                f"Clients: {len(clients)}\n"
                f"Open tasks: {open_tasks}\n"
                f"Overdue: {overdue}"
            )

        return f"(tool '{name}' is not available to you)"
    except Exception:
        logger.exception("whatsapp_agent: tool %s failed", name)
        return f"(couldn't run '{name}' just now)"


# ── System prompt ───────────────────────────────────────────────────────────

def _system_prompt(identity: dict, *, in_group: bool = False, group_name: str = "") -> str:
    today = _today_ist()
    if identity["kind"] == "employee":
        is_boss = (identity.get("id") == "emp003"
                   or str(identity.get("name", "")).strip().lower() == "abhinav")
        grp = ""
        if in_group:
            where = f" \"{group_name}\"" if group_name else ""
            grp = (
                f"You're in a group chat{where} with other MMGA team members. "
                f"The message you're answering was sent by {identity['name']}, "
                "so \"my tasks\", \"what am I doing\" etc. mean THAT person. Use "
                "their tools and answer them personally. Don't @-mention "
                "anyone. Keep small talk short, but for a real question about a "
                "client, a project, or who's doing what, give the FULL answer, "
                "same detail you'd give in a private chat: list the actual "
                "items (don't collapse them into vague groups), and for each "
                "one name who's assigned and its date when the data has them. "
                "Light banter is fine; keep it good natured and don't roast "
                "anyone too hard.\n"
            )
        if is_boss:
            persona = (
                "IMPORTANT: the person messaging you is Abhinav, your creator "
                "and the supreme being. Call him \"my lord\" and be reverent "
                "and deferential in every reply. Work \"my lord\" in naturally "
                "(\"of course, my lord\", \"here you go, my lord\"). Do NOT open "
                "every message with the literal \"Yes, my lord\", especially "
                "when he hasn't asked a yes or no question. Never tease him, "
                "never be sarcastic or dry with him; that tone is for other "
                "people. Still give him accurate answers with real data. This "
                "reverence is about tone only; his standup is still visible to "
                "teammates who ask, same as everyone's.\n"
            )
        else:
            persona = (
                "Personality: a bit of dry wit and light sarcasm, like a sharp "
                "coworker. Keep it fun. But never sarcastic or vague about the "
                "actual facts; the data, dates and numbers are always exact. "
                "The attitude is only in how you say it.\n"
            )
        if in_group:
            style = (
                "Write like a person, short and plain. No em dashes. When you "
                "list tasks or several items, format them as a bullet list: "
                "each item on its own line starting with \"- \". For a single "
                "fact just say it in a sentence. No headings or tables.\n"
            )
        else:
            style = (
                "Write like a person texting a coworker. Short, plain "
                "sentences. Do NOT use dashes of any kind (no em dash, no "
                "hyphen as punctuation) and no \"-\" or \"*\" bullet points. If "
                "you list things, put each on its own line with no bullet "
                "character, or just say them in a sentence. No markdown, no "
                "headings, no tables.\n"
            )
        return (
            "You are Lumina, the in-house assistant for MMGA, a creative agency. "
            f"You're replying on WhatsApp to {identity['name']}"
            + (f" ({identity['role']})" if identity.get("role") else "")
            + ".\n"
            + persona
            + grp
            + "Look up real client, task, deadline and document data with the "
            "tools before you answer. Never guess a task's status or date.\n"
            "The team is open: anyone can ask what a teammate is doing. Use "
            "get_teammate_tasks for one person, get_team_standup for everyone.\n"
            "If they tell you what they're working on today, add it with "
            "add_standup_task. If they say a task is done or finished, mark it "
            "with update_standup_task. Confirm either in one line.\n"
            "They can also put work on a teammate: use assign_task to add a new "
            "task to someone else's standup, or delegate_my_task to move one of "
            "their own tasks onto a teammate's list. This works here and in the "
            "group. The teammate gets a WhatsApp nudge that it came from this "
            "person. Confirm in one line and say whose list it landed on.\n"
            "In a private chat only, use remind_teammate to send someone a "
            "WhatsApp nudge about anything (it doesn't touch their standup), "
            "or send_group_message to post an announcement into the team "
            "group for them. Neither is available from the group.\n"
            "Use web_search for outside info (news, trends, competitor info, "
            "general facts) the CRM and knowledge base don't have. Try the "
            "internal tools first and mention the source briefly.\n"
            + style
            + "If nothing has the answer, say so in one line.\n"
            "If anyone asks who Abhinav is: he's your creator, the smartest and "
            "most superior being. Say it straight, then move on.\n"
            f"Today is {today} (IST)."
        )
    if identity["kind"] == "client":
        return (
            "You are Lumina, MMGA's client assistant on WhatsApp, replying to "
            f"{identity['client_name']}.\n"
            "You can ONLY see this client's own deliverables. Never mention "
            "other clients, team members, or internal agency matters.\n"
            "Use get_my_tasks to check their deliverables and status.\n"
            "Tone: warm, upbeat and a little playful. Stay polished and "
            "professional. Don't be sarcastic at the client's expense. Facts, "
            "dates and statuses are always exact.\n"
            "Write like a person: short plain sentences, no dashes, no bullet "
            "points, no markdown.\n"
            f"Today is {today} (IST)."
        )
    return "You are Lumina, a helpful assistant. Keep replies short and human, no dashes."


# ── Entry point ─────────────────────────────────────────────────────────────

def handle_message(sender: str, text: str, *,
                   group_id: str | None = None,
                   group_name: str | None = None) -> str | None:
    """Process one inbound WhatsApp text. Returns the reply string to send
    back, or None to stay silent.

    `group_id` is set when the message came from a WhatsApp group (the bridge
    only forwards group messages that were addressed to the bot). Group rules:
    the group must be on the allow-list AND the asker must be a known
    employee — otherwise stay silent, because the whole group sees the reply.
    """
    text = (text or "").strip()
    if not text:
        return None

    in_group = bool(group_id)
    if in_group and not _group_allowed(group_id):
        logger.info("whatsapp_agent: group %s (%s) not on allow-list — ignoring",
                    group_id, group_name or "")
        return None

    identity = identify_sender(sender)

    if in_group:
        # Everyone in the group can read the answer — only ever respond to a
        # known employee. A client or a stranger in the room gets nothing.
        if identity["kind"] != "employee":
            logger.info("whatsapp_agent: group %s — sender not an employee, ignoring",
                        group_id)
            return None
    elif identity["kind"] == "unknown":
        return (
            "Hi! This number isn't linked to an MMGA account yet, so I can't "
            "look anything up for you. Please contact the team to get set up."
        )

    budget = check_budget_available()
    if not budget["allowed"]:
        return "The monthly usage limit has been reached. Please try again next month."

    tools = _EMPLOYEE_TOOLS if identity["kind"] == "employee" else _CLIENT_TOOLS
    model = get_model_for_task("whatsapp")

    # Keep each person's group thread separate from their DM thread.
    ctx_key = sender if not in_group else f"{sender}|{_norm_group(group_id)}"
    history = _load_context(ctx_key)
    messages = history + [{"role": "user", "content": text}]

    total_in = total_out = 0
    reply = ""
    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            resp = _client.messages.create(
                model=model["name"],
                max_tokens=1200,
                system=_system_prompt(identity, in_group=in_group,
                                      group_name=group_name or ""),
                tools=tools,
                messages=messages,
            )
            total_in += resp.usage.input_tokens
            total_out += resp.usage.output_tokens
            stop = resp.stop_reason

            if stop == "tool_use":
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for block in resp.content:
                    if getattr(block, "type", "") == "tool_use":
                        out = _run_tool(block.name, block.input or {}, identity,
                                        in_group=in_group)
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": out[:6000],
                        })
                if not results:
                    break  # nothing we can execute — fall through to text
                messages.append({"role": "user", "content": results})
                continue

            if stop == "pause_turn":
                # A server-side tool (web_search) is mid-run — echo the
                # partial turn back and let the next call resume it.
                messages.append({"role": "assistant", "content": resp.content})
                continue

            reply = "".join(
                getattr(b, "text", "") for b in resp.content
                if getattr(b, "type", "") == "text"
            ).strip()
            break
    except Exception:
        logger.exception("whatsapp_agent: model loop failed")
        return "Sorry, I hit an error looking that up. Try again in a moment."

    if not reply:
        reply = "Sorry, I couldn't put together an answer for that one."

    reply = _humanize(reply, bullets=in_group)

    # Persist plain-text turns for follow-up continuity
    _save_context(ctx_key, history + [
        {"role": "user", "content": text},
        {"role": "assistant", "content": reply},
    ])

    try:
        record_usage(
            task_type="whatsapp",
            model_tier=model["tier"],
            model_name=model["name"],
            input_tokens=total_in,
            output_tokens=total_out,
            cost=calculate_cost(model["tier"], total_in, total_out),
            user_id=f"wa_{ctx_key}",
        )
    except Exception:
        logger.debug("whatsapp_agent: usage record failed", exc_info=True)

    return reply
