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
import wa_outbox

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Rolling per-sender context. Older than this and we start a fresh thread.
_CONTEXT_TTL_HOURS = 6
_CONTEXT_MAX_TURNS = 6          # user/assistant pairs kept between messages
_MAX_TOOL_ROUNDS = 10          # hard cap on the tool-use loop (pause_turn can eat rounds)

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
    return wa_outbox.wa_jid(raw)


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


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


# ── confirm-before-broadcast (send_group_message) ───────────────────────────

_PENDING_TTL_MIN = 10


def _set_pending_broadcast(emp_id: str, message: str) -> None:
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO wa_pending_action (sender, kind, payload, created_at) "
                "VALUES (?, 'broadcast', ?, ?) "
                "ON CONFLICT(sender) DO UPDATE SET kind=excluded.kind, "
                "payload=excluded.payload, created_at=excluded.created_at",
                (emp_id, message, datetime.now(timezone.utc).isoformat()),
            )
        conn.close()
    except Exception:
        logger.exception("whatsapp_agent: set pending broadcast failed")


def _pop_pending_broadcast(emp_id: str) -> str | None:
    """Return the pending broadcast text for this employee (and delete it).
    None if there isn't one or it's older than _PENDING_TTL_MIN."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT payload, created_at FROM wa_pending_action "
                    "WHERE sender=? AND kind='broadcast'", (emp_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        with conn:
            conn.execute("DELETE FROM wa_pending_action WHERE sender=?", (emp_id,))
        conn.close()
        try:
            ts = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - ts > timedelta(minutes=_PENDING_TTL_MIN):
                return None
        except ValueError:
            pass
        return row[0]
    except Exception:
        logger.exception("whatsapp_agent: pop pending broadcast failed")
        return None


# ── audit log ──────────────────────────────────────────────────────────────

_WRITE_TOOLS = {
    "assign_task", "delegate_my_task", "remind_teammate", "send_group_message",
    "add_standup_task", "update_standup_task", "create_task", "set_task_meta",
    "set_attendance", "review_task",
}
_SUCCESS_PREFIXES = (
    "added", "created", "handed", "updated", "marked", "reopened",
    "approved", "sent", "posted", "checked",
)


def _audit(scope: str, identity: dict, action: str, detail: str) -> None:
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO wa_action_log (sender, sender_name, scope, action, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (identity.get("id", ""), identity.get("name", ""), scope, action,
                 str(detail)[:400], datetime.now(timezone.utc).isoformat()),
            )
        conn.close()
    except Exception:
        logger.debug("whatsapp_agent: audit write failed", exc_info=True)


def _enqueue_outbound(jid: str, text: str) -> bool:
    """Queue a proactive WhatsApp message. The laptop companion polls
    /api/companion/whatsapp-outbox and delivers it via the local bridge —
    the Railway backend can't reach the bridge directly."""
    return wa_outbox.enqueue(jid, text)


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
    dash-free so the model doesn't echo bullet punctuation.

    Numbered 1-based, in the same order _match_standup_task() resolves a
    bare number against (both order by `id`) -- without this, "mark 4 as
    done" had nothing to resolve against: the list had no numbers and
    _match_standup_task only ever did text matching, so a bare digit query
    just failed to match anything."""
    if not tasks:
        return f"{who} standup for today is empty."
    done_words = ("done", "completed", "complete")
    lines = []
    for i, t in enumerate(tasks, 1):
        is_done = str(t["status"]).lower() in done_words
        prefix = "done: " if is_done else ""
        tail = ""
        if t.get("carried_from"):
            tail += f" (carried over from {t['carried_from']})"
        if t.get("blocker"):
            tail += f" (blocked by {t['blocker']})"
        lines.append(f"{i}. {prefix}{t['title']}{tail}")
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
    # Bare/near-bare number ("4", "#4", "task 4") -> 1-based position in
    # this same `cand` order, which is exactly the order _format_standup()
    # numbers the list in (both order by `id`). Checked before any text
    # matching since a pure digit query can't meaningfully match a title.
    num_m = re.fullmatch(r"#?\s*(?:task\s*)?(\d+)", q)
    if num_m:
        idx = int(num_m.group(1)) - 1
        return cand[idx] if 0 <= idx < len(cand) else None
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


def _find_notion_task(query: str, pool: list) -> dict | None:
    """Loose title match of `query` against a small list of Notion task dicts
    (exact -> substring -> word-overlap). None if nothing is confident."""
    q = (query or "").strip().lower()
    if not q or not pool:
        return None
    for t in pool:
        if (t.get("title") or "").strip().lower() == q:
            return t
    for t in pool:
        tl = (t.get("title") or "").lower()
        if q in tl or (len(tl) > 4 and tl in q):
            return t
    qt = set(re.findall(r"\w+", q))
    best, sc = None, 0
    for t in pool:
        ct = set(re.findall(r"\w+", (t.get("title") or "").lower()))
        s = len(qt & ct)
        if s > sc:
            best, sc = t, s
    return best if sc >= 2 else None


_APPROVAL_STATUSES = {
    "need for approval", "need approval", "needs approval",
    "pending review", "in review", "for approval",
}
_CLOSED_STATUSES = {
    "done", "approved", "posted", "final", "complete", "completed",
    "cancelled", "canceled", "need_for_approval",
}


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
    Hyphens inside words (reverse-engg) are never touched.

    Deliberately does NOT strip a leading number ("1. ", "2) ") the way it
    used to -- _format_standup() now numbers each task 1-based specifically
    so 'mark 4 as done' has something real to refer back to, and this used
    to silently strip that numbering back out again in every DM."""
    lines = []
    for ln in (reply or "").split("\n"):
        if not bullets:
            ln = re.sub(r"^(\s*)[-*•‣▪]\s+", r"\1", ln)
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
                       "complete', 'finished X', 'reopen X', or 'mark 4 as "
                       "done'/'mark #4 as done' referring to its number from "
                       "get_my_tasks' list. Match the task by a few words "
                       "from what they call it, OR by that list number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "A few words identifying the task, "
                                        "or its bare number from the list "
                                        "(e.g. '4')."},
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
                       "today, WITH each item's real status -- shows 'done: ' "
                       "on anything they've actually marked finished. Use this "
                       "for 'what is Nupur working on', 'what are Kshitij's "
                       "tasks', 'is Happy doing anything today', 'has Happy "
                       "done his tasks', 'what has X finished today' -- ANY "
                       "'X's tasks' question where completion status matters. "
                       "This is the right tool for that, not "
                       "get_task_schedule (that one is deadline-only and "
                       "silently drops completed items instead of marking "
                       "them done). Everyone on the team can see everyone "
                       "else's standup.",
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
    {
        "name": "create_task",
        "description": "Create a real task on the Notion board AND put it on the "
                       "assignee's daily standup for today. Use for 'add task: "
                       "shoot the Omotec reel, Friday, assign Happy'. Convert "
                       "relative dates ('Friday', 'tomorrow') to YYYY-MM-DD "
                       "yourself. Leave assignee/client/date out if not given.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The task."},
                "client": {"type": "string", "description": "Client name, if any."},
                "assignee": {"type": "string", "description": "Who it's for; blank = unassigned."},
                "due_date": {"type": "string", "description": "YYYY-MM-DD, if given."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "set_task_meta",
        "description": "Add or clear a blocker, or change the due date, on one "
                       "of the person's own standup tasks for today. Use for "
                       "'blocked on client feedback for X', 'push the deck to "
                       "Monday', 'clear the blocker on Y'. Match the task by a "
                       "few words; dates as YYYY-MM-DD.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A few words identifying the task."},
                "blocker": {"type": "string", "description": "Blocker text, or empty string to clear it."},
                "due_date": {"type": "string", "description": "New due date YYYY-MM-DD, or empty to clear."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "get_task_schedule",
        "description": "Board tasks by deadline: what's overdue, due today, or "
                       "due this week. Use for 'what's overdue for Omotec', "
                       "'what's due this week', 'what is Happy behind on'. "
                       "Optionally narrow by client or by person. Does NOT "
                       "tell you what someone has finished today -- it's "
                       "deadline filtering only, and completed/closed items "
                       "are left out entirely rather than marked done. For "
                       "'has Happy done his tasks', 'what has X finished "
                       "today', use get_teammate_tasks instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {"type": "string", "enum": ["overdue", "today", "week"],
                         "description": "Which window (default week)."},
                "client": {"type": "string", "description": "Narrow to one client."},
                "person": {"type": "string", "description": "Narrow to one assignee."},
            },
        },
    },
    {
        "name": "get_attendance",
        "description": "Who has checked in / out today, and who hasn't checked "
                       "in yet. Use for 'who's in', 'is Nupur working today'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_attendance",
        "description": "Check the person in or out for today. Use for "
                       "'checking in', 'I'm here', 'leaving for the day', 'done "
                       "for today'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["in", "out"]},
            },
            "required": ["action"],
        },
    },
    {
        "name": "get_daily_brief",
        "description": "The assembled daily brief: overdue / due-today / "
                       "due-tomorrow tasks, standups in, sheet-sync health, API "
                       "budget. Use for 'summarise today', 'what's the status', "
                       "'end of day rundown'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "enum": ["morning", "evening"],
                           "description": "morning = look ahead, evening = wrap-up (default)."},
            },
        },
    },
    {
        "name": "get_content_calendar",
        "description": "What's scheduled to be posted across ALL social-media "
                       "clients in the next few days, soonest date first. Use "
                       "for 'what are we posting this week', 'what's going "
                       "out tomorrow', 'what's coming next', 'what's going "
                       "live', 'what's posting soon' -- any question about "
                       "the upcoming content schedule, in a DM or a group. "
                       "Groups by day (nearest day first) and shows client, "
                       "post type, assignee and whether the caption is "
                       "written.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "How many days ahead (default 7)."}
            },
        },
    },
    {
        "name": "get_post_content",
        "description": "Pull the actual Sheets content for ONE task by name -- "
                       "the Idea, Script/Copy, Caption, and Drive link that were "
                       "typed into that task's row in Lumina Sheets. Use when "
                       "someone points at a specific task and asks for its "
                       "script, idea, caption, brief, or link, e.g. Happy "
                       "asking 'what's the script for my reel today'. Not for "
                       "browsing what's scheduled -- use get_content_calendar "
                       "for that.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task title or a fragment "
                          "of it, e.g. 'the reel today', 'Post 12'."},
                "person": {"type": "string", "description": "Whose task, if the "
                            "sender is asking about someone else's. Omit for "
                            "'my task'."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "get_client_status",
        "description": "A full snapshot for one client in a single reply: open "
                       "task count, overdue items, what's due in the next 7 "
                       "days, anything awaiting approval, blockers, and the last "
                       "time the client did something in their portal. Use for "
                       "'status on Omotec', 'where are we with Mellow'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client": {"type": "string", "description": "The client's name."}
            },
            "required": ["client"],
        },
    },
    {
        "name": "list_capabilities",
        "description": "Explain what you can do for this person. Use when they "
                       "ask 'what can you do', 'help', 'what do you know'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_sticker",
        "description": "React with a sticker from the team's saved pack, sent "
                       "alongside your text reply. Use it SPARINGLY — only when "
                       "a sticker genuinely lands (a real win, a groan, a "
                       "deserved roast), not on ordinary replies. Pass a mood "
                       "word ('smug', 'bruh', 'nice', 'overdue', 'approved') to "
                       "match one, or leave it blank for a random sticker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mood": {"type": "string", "description": "Optional mood/tag to match."}
            },
        },
    },
    {
        "name": "get_recent_actions",
        "description": "The audit trail of writes you've made on people's behalf "
                       "(tasks created, approved, delegated, standup edits, "
                       "check-ins). Use for 'what have you done today', 'show "
                       "the log'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many entries (default 15)."}
            },
        },
    },
    {
        "name": "get_pending_approvals",
        "description": "Tasks currently waiting on approval / review across the "
                       "board. Use for 'what's pending approval', 'anything for "
                       "me to review'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "review_task",
        "description": "Approve a task that's waiting on approval, or send it "
                       "back for changes. Use for 'approve the Omotec reel', "
                       "'reject X', 'send Y back'. Match by a few words from "
                       "the title.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A few words from the task title."},
                "decision": {"type": "string", "enum": ["approve", "reject"]},
            },
            "required": ["task", "decision"],
        },
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


def group_vibe_sticker(group_id: str, messages: list, tags: list | None = None) -> str | None:
    """The bridge saw a burst of activity in an allow-listed group. Read the
    recent messages and return a one-word mood for a reaction sticker, or None
    to stay quiet (which is the usual answer). `tags` is the bridge's actual
    sticker-tag vocabulary — the model picks from that. One cheap Haiku call."""
    if not group_id or not _group_allowed(group_id) or not messages:
        return None
    try:
        if not check_budget_available().get("allowed"):
            return None
    except Exception:
        return None
    transcript = "\n".join(
        f"{(m.get('name') or 'someone')}: {str(m.get('text') or '')[:200]}"
        for m in messages[-15:] if str(m.get("text") or "").strip()
    )
    if not transcript.strip():
        return None
    allowed = set(_VIBE_WORDS)
    if tags:
        allowed |= {re.sub(r"[^a-z]", "", str(t).lower()) for t in tags if str(t).strip()}
        allowed.discard("")
    vocab = ", ".join(sorted(allowed))
    model = get_model_for_task("whatsapp")
    try:
        resp = _client.messages.create(
            model=model["name"],
            max_tokens=12,
            system=(
                "You lurk silently in a team's WhatsApp group. Below is the last "
                "minute or two of chat during a busy moment. If it CLEARLY calls "
                "for a single reaction sticker, reply with ONE word from this "
                f"list: {vocab}. If it doesn't clearly call for one — which is "
                "most of the time — reply exactly: none. One word, nothing else."
            ),
            messages=[{"role": "user", "content": transcript}],
        )
        try:
            record_usage("whatsapp", model["tier"], model["name"],
                         resp.usage.input_tokens, resp.usage.output_tokens,
                         calculate_cost(model["tier"], resp.usage.input_tokens,
                                        resp.usage.output_tokens),
                         user_id=f"wa_vibe_{_norm_group(group_id)}")
        except Exception:
            pass
        raw = "".join(getattr(b, "text", "") for b in resp.content
                      if getattr(b, "type", "") == "text").strip().lower()
        word = re.sub(r"[^a-z]", "", raw.split()[0]) if raw.split() else ""
        return word if word in allowed else None
    except Exception:
        logger.exception("whatsapp_agent: group vibe check failed")
        return None


_VIBE_WORDS = {"lol", "smug", "bruh", "oof", "nice", "celebrate", "facepalm",
               "shock", "agree", "cry", "fire"}


_STICKER_CMD_RE = re.compile(r"^\s*!sticke?rs?\b\s*([a-z]+)?", re.I)


def sticker_command(text: str, identity: dict):
    """If `text` is a '!stickers ...' management command from an employee,
    return (reply_text, cmd) with cmd in on|off|list|clear|None. Else None.
    The bridge acts on the cmd; the agent never runs for these."""
    if identity.get("kind") != "employee":
        return None
    m = _STICKER_CMD_RE.match(text or "")
    if not m:
        return None
    sub = (m.group(1) or "on").lower()
    if sub in ("on", "add", "learn", "teach", "capture", "start"):
        return ("Sticker capture is on for 5 minutes. Forward me the stickers "
                "you want (from your WhatsApp favourites). Send a one-word tag "
                "right after a sticker to label it. Say '!stickers done' when "
                "you're finished.", "on")
    if sub in ("off", "done", "stop", "end", "finish"):
        return ("Sticker capture off.", "off")
    if sub in ("list", "count", "status"):
        return ("(count)", "list")   # bridge overwrites with its real number
    if sub in ("clear", "reset", "wipe", "delete"):
        return ("Cleared every saved sticker.", "clear")
    return ("Say '!stickers' to start teaching, '!stickers done' to stop, "
            "'!stickers list' for a count, '!stickers clear' to wipe.", None)


def _run_tool(name: str, tool_input: dict, identity: dict,
              in_group: bool = False, turn: dict | None = None) -> str:
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
            if not _team_group_jid():
                return ("No team group is set up for me to post to. Add one "
                        "with the whatsapp_team_group setting or the group "
                        "allow-list.")
            _set_pending_broadcast(identity["id"], msg[:1500])
            return (f"Ready to post this to the team group:\n\n\"{msg[:400]}\"\n\n"
                    "Reply 'yes' to send it, or 'no' to cancel.")

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

        if name == "create_task" and kind == "employee":
            title = (tool_input or {}).get("title", "").strip()
            if not title:
                return "(no task title — ask what the task is)"
            cli = (tool_input or {}).get("client", "").strip()
            who = (tool_input or {}).get("assignee", "").strip()
            due = (tool_input or {}).get("due_date", "").strip()
            if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
                due = ""
            emp = _resolve_employee(who) if who else None
            if who and not emp:
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Don't know who '{who}' is. Team: {names}."
            c = _find_client(cli) if cli else None
            cname = (c or {}).get("name", "") or cli
            cnid = (c or {}).get("notion_id", "")
            nid = ""
            if notion_store.is_configured():
                try:
                    res = notion_store.create_task(
                        title=title, client_name=cname, client_notion_id=cnid,
                        assigned_to=(emp["name"] if emp else ""),
                        due_date=due, creation_date=today,
                    )
                    nid = (res or {}).get("notion_id", "")
                except Exception:
                    logger.exception("whatsapp_agent: create_task notion failed")
            su_uid = emp["id"] if emp else identity["id"]
            try:
                conn = get_connection()
                with conn:
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, notion_id, due_date, delegated_from) "
                        "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                        (su_uid, today, title[:500], (nid or None), (due or None),
                         (identity["name"] if emp and emp["id"] != identity["id"] else None)),
                    )
                conn.close()
            except Exception:
                logger.exception("whatsapp_agent: create_task standup insert failed")
            if emp and emp["id"] != identity["id"]:
                jid = _wa_jid(emp.get("whatsapp"))
                if jid:
                    _enqueue_outbound(
                        jid,
                        f"{identity['name']} created a task for you"
                        + (f" ({cname})" if cname else "") + f": {title[:300]}"
                        + (f", due {due}" if due else ""),
                    )
            bits = ["Created: " + title[:120]]
            if cname:
                bits.append(f"client {cname}")
            bits.append(f"for {emp['name']}" if emp else "unassigned")
            if due:
                bits.append(f"due {due}")
            if not nid and notion_store.is_configured():
                bits.append("(on the standup only, Notion write failed)")
            return ", ".join(bits) + "."

        if name == "set_task_meta" and kind == "employee":
            q = (tool_input or {}).get("task", "")
            blk = (tool_input or {}).get("blocker", None)
            due = (tool_input or {}).get("due_date", None)
            if due not in (None, "") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(due)):
                return "Give the date as YYYY-MM-DD."
            m = _match_standup_task(identity["id"], q)
            if not m:
                have = _standup_tasks_today(identity["id"])
                names = "; ".join(t["title"] for t in have) or "nothing yet"
                return f"Couldn't find a task matching '{q}'. Today's list: {names}."
            sets, params = [], []
            if blk is not None:
                sets.append("blocker=?")
                params.append(str(blk)[:300] or None)
            if due is not None:
                sets.append("due_date=?")
                params.append(str(due) or None)
            if not sets:
                return "(nothing to change — give a blocker or a due date)"
            try:
                conn = get_connection()
                with conn:
                    conn.execute(
                        f"UPDATE standup_tasks SET {', '.join(sets)} WHERE id=?",
                        (*params, m["id"]),
                    )
                conn.close()
            except Exception:
                logger.exception("whatsapp_agent: set_task_meta failed")
                return "(couldn't update that just now)"
            if due is not None and m.get("notion_id"):
                try:
                    notion_store.update_task(m["notion_id"], due_date=str(due))
                except Exception:
                    logger.debug("whatsapp_agent: notion due mirror failed", exc_info=True)
            done_bits = []
            if blk is not None:
                done_bits.append(f"blocker: {blk}" if blk else "blocker cleared")
            if due is not None:
                done_bits.append(f"due {due}" if due else "due date cleared")
            return f"Updated '{m['title']}': " + ", ".join(done_bits) + "."

        if name == "get_task_schedule" and kind == "employee":
            when = str((tool_input or {}).get("when", "week")).strip().lower()
            cf = str((tool_input or {}).get("client", "")).strip().lower()
            pf = str((tool_input or {}).get("person", "")).strip().lower()
            try:
                tasks = notion_store.list_tasks() if notion_store.is_configured() else []
            except Exception:
                tasks = []
            wk_end = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")

            def _keep(t):
                if (t.get("status") or "").strip().lower().replace(" ", "_") in _CLOSED_STATUSES:
                    return False
                due = str(t.get("due_date") or "")[:10]
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
                    return False
                if when == "overdue" and not (due < today):
                    return False
                if when in ("today", "day") and due != today:
                    return False
                if when not in ("overdue", "today", "day") and due > wk_end:
                    return False
                if cf and cf not in (t.get("client_name") or "").lower():
                    return False
                if pf and pf not in (t.get("assigned_to") or "").lower():
                    return False
                return True

            hits = sorted((t for t in tasks if _keep(t)), key=lambda t: str(t.get("due_date")))
            if not hits:
                return "Nothing matches that."
            lines = []
            for t in hits[:25]:
                od = " OVERDUE" if _is_overdue(t.get("due_date"), today) else ""
                lines.append(
                    f"{t.get('title')} | {t.get('client_name') or 'no client'} | "
                    f"{t.get('assigned_to') or 'unassigned'} | due {t.get('due_date')}{od}"
                )
            head = {"overdue": "Overdue", "today": "Due today", "day": "Due today"}.get(when, "This week")
            return f"{head} ({len(hits)}):\n" + "\n".join(lines)

        if name == "get_attendance" and kind == "employee":
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT user_id, checkin_time, checkout_time FROM daily_attendance WHERE date=?",
                    (today,),
                )
                rows = cur.fetchall()
                conn.close()
            except Exception:
                rows = []
            names = {e["id"]: e["name"] for e in _active_employees()}
            inn, out, seen = [], [], set()
            for uid, ci, co in rows:
                seen.add(uid)
                nm = names.get(uid, uid)
                if ci and co:
                    out.append(f"{nm} (left {str(co)[:5]})")
                elif ci:
                    inn.append(f"{nm} (in {str(ci)[:5]})")
            missing = sorted(n for i, n in names.items() if i not in seen)
            parts = []
            if inn:
                parts.append("In now: " + ", ".join(sorted(inn)))
            if out:
                parts.append("Left: " + ", ".join(sorted(out)))
            if missing:
                parts.append("No check-in: " + ", ".join(missing))
            return "\n".join(parts) if parts else "No attendance recorded yet today."

        if name == "set_attendance" and kind == "employee":
            action = str((tool_input or {}).get("action", "")).strip().lower()
            try:
                if action.startswith("in") or action in ("here", "arrive", "start"):
                    from routes.attendance import _attendance_checkin
                    _attendance_checkin(identity["id"])
                    return "Checked you in for today."
                if action.startswith("out") or action in ("leave", "leaving", "done", "bye"):
                    from routes.attendance import _attendance_checkout
                    _attendance_checkout(identity["id"])
                    return "Checked you out. See you tomorrow."
            except Exception:
                logger.exception("whatsapp_agent: set_attendance failed")
                return "(couldn't update attendance just now)"
            return "Say 'in' to check in or 'out' to check out."

        if name == "get_daily_brief" and kind == "employee":
            win = str((tool_input or {}).get("window", "")).strip().lower()
            win = "morning" if win in ("morning", "am", "today", "ahead") else "evening"
            try:
                from routes.companion import _build_digest
                return _build_digest(win).get("text") or "Nothing to report."
            except Exception:
                logger.exception("whatsapp_agent: daily brief failed")
                return "(couldn't build the brief just now)"

        if name == "get_pending_approvals" and kind == "employee":
            try:
                tasks = notion_store.list_tasks() if notion_store.is_configured() else []
            except Exception:
                tasks = []
            pend = [t for t in tasks
                    if (t.get("status") or "").strip().lower() in _APPROVAL_STATUSES]
            if not pend:
                return "Nothing is waiting on approval right now."
            lines = [
                f"{t.get('title')} | {t.get('client_name') or 'no client'} | "
                f"{t.get('assigned_to') or 'unassigned'}"
                for t in pend[:25]
            ]
            return (f"Waiting on approval ({len(pend)}):\n" + "\n".join(lines)
                    + "\n\nSay 'approve <name>' or 'reject <name>'.")

        if name == "review_task" and kind == "employee":
            q = (tool_input or {}).get("task", "")
            dec = str((tool_input or {}).get("decision", "approve")).strip().lower()
            dec = "reject" if dec in ("reject", "rejected", "deny", "decline", "send back") else "approve"
            try:
                tasks = notion_store.list_tasks() if notion_store.is_configured() else []
            except Exception:
                tasks = []
            pend = [t for t in tasks
                    if (t.get("status") or "").strip().lower() in _APPROVAL_STATUSES]
            if not pend:
                return "Nothing is waiting on approval right now."
            m = _find_notion_task(q, pend)
            if not m:
                opts = "; ".join(t.get("title") for t in pend[:10])
                return f"Not sure which one you mean. Pending: {opts}"
            try:
                ok = notion_store.update_task(
                    m["notion_id"], status=("approved" if dec == "approve" else "in_progress")
                )
            except Exception:
                logger.exception("whatsapp_agent: review_task failed")
                ok = False
            if not ok:
                return "(Notion didn't accept that update)"
            return (f"Approved: {m.get('title')}" if dec == "approve"
                    else f"Sent back for changes: {m.get('title')}")

        if name == "send_sticker" and kind == "employee":
            mood = str((tool_input or {}).get("mood", "")).strip().lower()
            if turn is not None:
                turn["sticker"] = mood or "random"
            return "A sticker will go out with your reply. Write the text reply now."

        if name == "list_capabilities":
            if kind == "client":
                return ("I can look up your deliverables and their status, due "
                        "dates, and the content details (post type, brief, "
                        "script, caption). Just ask.")
            extra = "" if in_group else (
                " In a private chat I can also send a reminder to a teammate, "
                "post an announcement to the group (with a confirm step), check "
                "you in or out, and give you the full daily brief.")
            return (
                "Ask me about: your standup tasks, a teammate's tasks, the whole "
                "team's standup, any client's tasks and content calendar, what's "
                "overdue or due this week, what's pending approval, the "
                "knowledge base, and general web search.\n"
                "I can also: add a task to your or a teammate's standup, create "
                "a real board task, mark tasks done, add a blocker or move a due "
                "date, delegate a task, approve or send back a task, and show "
                "who's checked in." + extra
            )

        if name == "get_recent_actions" and kind == "employee":
            lim = (tool_input or {}).get("limit", 15)
            try:
                lim = max(1, min(int(lim), 40))
            except Exception:
                lim = 15
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT created_at, sender_name, action, detail FROM wa_action_log "
                    "ORDER BY id DESC LIMIT ?", (lim,),
                )
                rows = cur.fetchall()
                conn.close()
            except Exception:
                rows = []
            if not rows:
                return "No logged actions yet."
            return "Recent actions:\n" + "\n".join(
                f"{str(r[0])[:16].replace('T', ' ')}  {r[1]}: {r[2]}"
                + (f" — {str(r[3])[:80]}" if r[3] else "")
                for r in rows
            )

        if name == "get_content_calendar" and kind == "employee":
            try:
                days = max(1, min(int((tool_input or {}).get("days", 7) or 7), 31))
            except Exception:
                days = 7
            try:
                tasks = notion_store.list_tasks() if notion_store.is_configured() else []
            except Exception:
                tasks = []
            end = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")

            def _social(t):
                ty = (t.get("type") or t.get("service") or "").lower()
                return ("social" in ty) or bool(re.search(
                    r"\[(story|reel|static|carousel|post|video)\]",
                    t.get("title") or "", re.I))

            rows = [t for t in tasks
                    if re.match(r"^\d{4}-\d{2}-\d{2}$", str(t.get("due_date") or "")[:10])
                    and today <= str(t.get("due_date"))[:10] <= end
                    and _social(t)
                    and (t.get("status") or "").strip().lower() not in ("cancelled", "canceled")]
            if not rows:
                return f"Nothing scheduled to post in the next {days} days."
            rows.sort(key=lambda t: (str(t.get("due_date"))[:10], t.get("client_name") or ""))
            out, cur_day = [], None
            for t in rows:
                d = str(t.get("due_date"))[:10]
                if d != cur_day:
                    out.append(f"\n{d}:")
                    cur_day = d
                cap = "caption ready" if (t.get("caption") or "").strip() else "no caption"
                out.append(
                    f"  {t.get('client_name') or '?'} | {t.get('type') or 'post'} | "
                    f"{t.get('title')} | {t.get('assigned_to') or 'unassigned'} | "
                    f"{(t.get('status') or '').lower() or 'planned'} | {cap}"
                )
            return f"Posting in the next {days} days ({len(rows)}):" + "\n".join(out)

        if name == "get_post_content" and kind == "employee":
            q = (tool_input or {}).get("task", "").strip()
            person = (tool_input or {}).get("person", "").strip()
            if not q:
                return "Which task? Give me the title or part of it."
            try:
                tasks = notion_store.list_tasks() if notion_store.is_configured() else []
            except Exception:
                tasks = []
            if not tasks:
                return "Can't reach Notion right now to look that up."

            if person:
                emp = _resolve_employee(person)
                who = (emp["name"] if emp else person).lower()
            else:
                who = identity["name"].lower()
            pool = [t for t in tasks if who in (t.get("assigned_to") or "").lower()]
            match = _find_notion_task(q, pool) or _find_notion_task(q, tasks)
            if not match:
                return (f"Can't find a task matching '{q}'"
                        + (f" assigned to {who}" if pool else "") + ".")

            lines = [
                f"{match.get('title')} -- {match.get('client_name') or 'no client'}",
                f"{match.get('assigned_to') or 'unassigned'} | "
                f"{(match.get('status') or 'planned')} | due {match.get('due_date') or '?'}",
            ]
            fields = [
                ("Idea", match.get("idea")),
                ("Script/Copy", match.get("scripts_copy")),
                ("Caption", match.get("caption")),
                ("Content", match.get("content")),
                ("Link", match.get("link")),
            ]
            had_any = False
            for label, val in fields:
                v = (val or "").strip()
                if v:
                    had_any = True
                    lines.append(f"\n{label}:\n{v[:1200]}")
            if not had_any:
                lines.append("\n(Nothing's been filled in on this row yet -- Idea/Script/Caption are all empty.)")
            return "\n".join(lines)

        if name == "get_client_status" and kind == "employee":
            cli = (tool_input or {}).get("client", "").strip()
            c = _find_client(cli)
            cname = (c or {}).get("name", "") or cli
            cnid = (c or {}).get("notion_id", "")
            if not cname:
                return "Which client? Use get_clients for the list."
            try:
                tasks = _tasks_for_client(cname, cnid)
            except Exception:
                tasks = []
            wk_end = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
            open_t = [t for t in tasks
                      if (t.get("status") or "").strip().lower().replace(" ", "_") not in _CLOSED_STATUSES]
            overdue = [t for t in open_t if _is_overdue(t.get("due_date"), today)]
            upcoming = sorted(
                (t for t in open_t
                 if re.match(r"^\d{4}-\d{2}-\d{2}$", str(t.get("due_date") or "")[:10])
                 and today <= str(t.get("due_date"))[:10] <= wk_end),
                key=lambda t: str(t.get("due_date")))
            appr = [t for t in tasks
                    if (t.get("status") or "").strip().lower() in _APPROVAL_STATUSES]
            lines = [f"{cname}:"]
            lines.append(f"Open tasks: {len(open_t)}"
                         + (f", {len(overdue)} overdue" if overdue else ""))
            if overdue:
                lines.append("Overdue: " + "; ".join(
                    f"{t.get('title')} ({t.get('assigned_to') or '?'})" for t in overdue[:6]))
            if upcoming:
                lines.append("Next 7 days: " + "; ".join(
                    f"{t.get('title')} {str(t.get('due_date'))[:10]}" for t in upcoming[:6]))
            if appr:
                lines.append(f"Awaiting approval ({len(appr)}): "
                             + "; ".join(t.get("title") for t in appr[:5]))
            ctitles = {_norm(t.get("title") or "") for t in tasks if t.get("title")}
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT title, blocker FROM standup_tasks "
                    "WHERE date=? AND blocker IS NOT NULL AND blocker<>''", (today,))
                for tt, bb in cur.fetchall():
                    nt = _norm(tt)
                    if any(nt == ct or (len(ct) > 5 and (nt in ct or ct in nt)) for ct in ctitles):
                        lines.append(f"Blocked: {tt} — {bb}")
                conn.close()
            except Exception:
                pass
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM client_users WHERE lower(client_name)=lower(?) "
                    "OR (client_notion_id<>'' AND client_notion_id=?)", (cname, cnid))
                cu = cur.fetchone()
                if cu:
                    cur.execute("SELECT MAX(created_at) FROM client_dependencies WHERE client_id=?",
                                (str(cu[0]),))
                    r = cur.fetchone()
                    if r and r[0]:
                        lines.append(f"Last portal activity: {str(r[0])[:16].replace('T', ' ')}")
                conn.close()
            except Exception:
                pass
            if len(lines) == 2 and not open_t:
                return f"{cname}: no open tasks, nothing pending."
            return "\n".join(lines)

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
            "get_teammate_tasks for one person, get_team_standup for everyone "
            "-- both show real done/pending status per task. For 'X's "
            "tasks' or 'has X finished today', prefer get_teammate_tasks "
            "over get_task_schedule: the schedule tool only filters by "
            "deadline and just omits anything already done instead of "
            "marking it, which reads as 'not shown as done' even when it "
            "really is.\n"
            "If they tell you what they're working on today, add it with "
            "add_standup_task. If they say a task is done or finished, mark it "
            "with update_standup_task. Confirm either in one line.\n"
            "When you show someone THEIR OWN standup, read the scoreboard and "
            "react to it. If they've cleared most of the list, give them real "
            "credit in a sentence. If they've completed more than 5 tasks in "
            "the day, add a line that at this rate they've earned a raise, an "
            "early leave or a full holiday"
            + (" — keep it admiring, my lord, never teasing.\n" if is_boss
               else " — say it dry and sarcastic.\n")
            + "Don't do this for a teammate's standup, only their own.\n"
            "They can also put work on a teammate: use assign_task to add a new "
            "task to someone else's standup, or delegate_my_task to move one of "
            "their own tasks onto a teammate's list. This works here and in the "
            "group. The teammate gets a WhatsApp nudge that it came from this "
            "person. Confirm in one line and say whose list it landed on.\n"
            "In a private chat only, use remind_teammate to send someone a "
            "WhatsApp nudge about anything (it doesn't touch their standup), "
            "or send_group_message to post an announcement into the team "
            "group for them. Neither is available from the group.\n"
            "Other things you can do: create_task makes a real board task + "
            "standup entry (turn 'Friday'/'tomorrow' into a YYYY-MM-DD date "
            "yourself). set_task_meta adds a blocker or moves a due date on one "
            "of their tasks. get_task_schedule answers what's overdue / due "
            "today / due this week (optionally per client or per person). "
            "get_attendance / set_attendance for who's in and checking in or "
            "out. get_daily_brief for a full status rundown. "
            "get_pending_approvals lists what's awaiting sign-off and "
            "review_task approves or sends one back. For 'what's coming "
            "next', 'what's going live', 'what's posting soon' -- anything "
            "about the upcoming content schedule -- use get_content_calendar, "
            "not get_task_schedule; it's sorted soonest-date-first and works "
            "the same in a DM or the group. When someone points at a "
            "specific task and wants its actual script, idea, caption, or "
            "link -- 'what's the script for my reel today', 'send me the "
            "idea for Post 12' -- use get_post_content, not "
            "get_content_calendar (that one only shows whether a caption "
            "exists, not the content itself); defaults to the sender's own "
            "tasks unless they name someone else. get_client_status gives a "
            "one-shot snapshot for one client. "
            "list_capabilities explains what you do; get_recent_actions is the "
            "log of writes you've made. Confirm every write in one short line.\n"
            "send_group_message does NOT post immediately — it asks the person "
            "to reply 'yes' first; just relay that.\n"
            "send_sticker adds a sticker to your reply. Use it when the "
            "person's message really lands — a genuine win, a good burn, "
            "something absurd, a facepalm moment — in a DM or the group. Not on "
            "a routine answer, and at most once in a while.\n"
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
                   group_name: str | None = None):
    """Process one inbound WhatsApp text. Returns the reply string to send
    back, None to stay silent, or {"reply": str, "sticker": str} when the
    agent also wants a sticker sent alongside the text.

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

    # Confirm-before-broadcast: a pending send_group_message waiting on a yes/no.
    if identity["kind"] == "employee" and not in_group:
        pending = _pop_pending_broadcast(identity["id"])
        if pending is not None:
            low = _norm(text).rstrip("!. ")
            if low in ("yes", "y", "yeah", "yep", "send", "send it", "confirm",
                       "do it", "go", "ok", "okay", "post it"):
                gjid = _team_group_jid()
                if gjid and _enqueue_outbound(gjid, pending):
                    _audit("dm", identity, "send_group_message", pending[:200])
                    return "Posted to the group."
                return "(couldn't send that to the group just now)"
            if low in ("no", "n", "nope", "cancel", "stop", "nvm", "nevermind",
                       "never mind", "don't", "dont"):
                return "Cancelled, nothing was sent."
            # anything else -> treat as a fresh message (pending already cleared)

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
    last_tool_text = ""
    turn = {"sticker": None}
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
                                        in_group=in_group, turn=turn)
                        if (block.name in _WRITE_TOOLS
                                and _norm(out).startswith(_SUCCESS_PREFIXES)):
                            _audit("group:" + _norm_group(group_id) if in_group else "dm",
                                   identity, block.name, out)
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": out[:6000],
                        })
                        last_tool_text = out
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
        # The model finished a tool call (e.g. update_standup_task actually
        # succeeded) but never produced a final text turn -- ran out of
        # _MAX_TOOL_ROUNDS, or a round's response was tool-use-only with no
        # accompanying text. Relaying the last tool's own result (which is
        # always a real, informative string -- "Marked done: X", "Couldn't
        # find a task matching 'Y'", etc.) is far better than a generic
        # non-answer that hides whether the action actually happened.
        reply = last_tool_text or "Sorry, I couldn't put together an answer for that one."

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

    if turn["sticker"]:
        return {"reply": reply, "sticker": turn["sticker"]}
    return reply
