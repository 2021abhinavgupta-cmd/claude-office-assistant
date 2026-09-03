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


def _tasks_for_employee(name: str) -> list:
    if notion_store.is_configured():
        try:
            return notion_store.list_tasks(assigned_to=name)
        except Exception:
            logger.exception("whatsapp_agent: notion list_tasks (employee) failed")
    return []


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
        "description": "List the tasks currently assigned to the person you are "
                       "chatting with.",
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


def _run_tool(name: str, tool_input: dict, identity: dict) -> str:
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

        if name == "get_my_tasks":
            if kind == "employee":
                tasks = _tasks_for_employee(identity["name"])
                if not tasks:
                    return "You have no tasks assigned right now."
                lines = [_fmt_task_line(t, include_assignee=False) for t in tasks[:40]]
                return "Your tasks:\n" + "\n".join(lines)
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
        grp = ""
        if in_group:
            where = f" \"{group_name}\"" if group_name else ""
            grp = (
                f"- You are in a group chat{where} with other MMGA team members. "
                "Keep it to one or two lines, answer what was asked, and don't "
                "@-mention anyone. Light group banter is welcome — keep it "
                "good-natured, don't roast anyone too hard.\n"
            )
        return (
            "You are Lumina, the in-house assistant for MMGA, a creative agency. "
            f"You are replying on WhatsApp to {identity['name']}"
            + (f" ({identity['role']})" if identity.get("role") else "")
            + ".\n"
            + grp
            + "- Personality: dry wit and a bit of sarcasm, like a sharp colleague "
            "who's seen it all. Tease lightly, keep it fun. But you are NEVER "
            "sarcastic or vague about the actual facts — the data, dates and "
            "numbers are always straight and correct; the attitude is only in "
            "how you say it.\n"
            "- Use the tools to look up real client, task, deadline and document "
            "data before answering. Never guess a task's status or date.\n"
            "- If they tell you what they're working on today, add it to their "
            "standup with add_standup_task and confirm it in one line.\n"
            "- You can use web_search for current or external information (news, "
            "trends, competitor info, general facts) the CRM and knowledge base "
            "don't have. Prefer internal tools first; name the source briefly.\n"
            "- Keep replies short and WhatsApp-style: plain text, no markdown "
            "headings, no tables. A line or two, a quip, done. Simple '-' bullets "
            "when you list things.\n"
            "- If nothing has the answer, say so in one line (you can be dry "
            "about it).\n"
            f"- Today is {today} (IST)."
        )
    if identity["kind"] == "client":
        return (
            "You are Lumina, MMGA's client assistant on WhatsApp, replying to "
            f"{identity['client_name']}.\n"
            "- You can ONLY see this client's own deliverables. Never mention "
            "other clients, team members, or internal agency matters.\n"
            "- Use get_my_tasks to check their deliverables and status.\n"
            "- Tone: warm, upbeat and a little playful — a friendly quip is fine "
            "— but stay polished and professional. Don't be sarcastic at the "
            "client's expense. Facts, dates and statuses are always exact.\n"
            "- Keep replies short and plain-text.\n"
            f"- Today is {today} (IST)."
        )
    return "You are Lumina, a helpful assistant. Keep replies short, with a bit of wit."


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
                        out = _run_tool(block.name, block.input or {}, identity)
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
        return "Sorry — I hit an error looking that up. Try again in a moment."

    if not reply:
        reply = "Sorry, I couldn't put together an answer for that one."

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
