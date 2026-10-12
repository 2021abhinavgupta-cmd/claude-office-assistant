"""The boss's personal morning brief -- what a PA would tell you at 9 AM.

Pulls from everything the assistant knows: today's calendar, promises people made
in the group that are due/overdue, decisions taken since yesterday, who has no
standup yet, posts going live today, approvals waiting on the boss, who is on
leave. The facts are gathered by code (so nothing is invented); one model call
then writes them up in the assistant's voice, with a plain-text fallback so the
brief still goes out if the model is unavailable.

Sent once per weekday by task_scheduler._run_pa_brief to the alert recipients
(app_settings 'alert_recipient_ids', default the boss), and available on demand
through the agent's get_my_brief tool.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import get_connection

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_APPROVAL = {"need for approval", "need_for_approval", "pending review", "in review",
             "awaiting approval", "submitted"}


def _section(title: str, lines: list) -> str:
    lines = [l for l in lines if l]
    return f"{title}:\n" + "\n".join(f"- {l}" for l in lines) if lines else ""


def gather(today: str | None = None) -> dict:
    """Plain facts, section by section. Each part is best-effort."""
    now = datetime.now(_IST)
    today = today or now.strftime("%Y-%m-%d")
    parts: dict = {}

    try:
        import gcal
        if gcal.configured():
            ev = gcal.format_events(gcal.upcoming(days=1, start=now.replace(hour=0, minute=0, second=0)))
            parts["calendar"] = ev.split("\n") if ev else []
    except Exception:
        logger.debug("pa_brief: calendar failed", exc_info=True)

    try:
        import group_intel
        due, over = [], []
        for r in group_intel.list_commitments("", "open", 60):
            d = r.get("due") or ""
            line = f"{r['owner']}: {r['action']}" + (f" (due {d})" if d else "")
            if d and d < today:
                over.append(line + " [overdue]")
            elif d == today:
                due.append(line)
        parts["group_due"] = due + over
        conn = get_connection()
        group_intel.ensure_schema(conn)
        since = (datetime.now(timezone.utc) - timedelta(hours=26)).isoformat()
        parts["decisions"] = [
            (f"[{r[1]}] " if r[1] else f"[{r[0]}] ") + r[2]
            for r in conn.execute("SELECT kind, client, text FROM group_facts WHERE created_at>=? "
                                  "ORDER BY id DESC LIMIT 8", (since,)).fetchall()]
        conn.close()
    except Exception:
        logger.debug("pa_brief: group intel failed", exc_info=True)

    try:
        import utils, leave_store
        emps = [e for e in utils._load_employees().get("employees", [])
                if str(e.get("status", "active")).lower() not in
                {"inactive", "disabled", "left", "removed", "archived", "former"}]
        on_leave = leave_store.on_leave_ids(today)
        conn = get_connection()
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM standup_tasks WHERE date=? AND status NOT IN ('deleted','delegated')",
            (today,)).fetchall()}
        conn.close()
        parts["on_leave"] = [e["name"] for e in emps if e["id"] in on_leave]
        parts["no_standup"] = [e["name"] for e in emps if e["id"] not in have and e["id"] not in on_leave]
    except Exception:
        logger.debug("pa_brief: standup/leave failed", exc_info=True)

    try:
        import notion_store
        import google_sheets_store as gs
        if notion_store.is_configured():
            tasks = notion_store.list_tasks() or []
            live, appr = [], []
            for t in tasks:
                st = str(t.get("status") or "").strip().lower().replace("_", " ")
                title = f"{t.get('client_name') or t.get('client') or ''} {t.get('title') or ''}".strip()
                if st in _APPROVAL:
                    appr.append(f"{title} ({t.get('assigned_to') or 'unassigned'})")
                try:
                    social = gs._is_social_task(t)
                except Exception:
                    social = False
                if social and str(t.get("due_date") or "")[:10] == today and st not in {"posted", "final", "done"}:
                    live.append(f"{title} ({t.get('assigned_to') or 'unassigned'}, {t.get('status') or 'no status'})")
            parts["going_live"] = live[:12]
            parts["approvals"] = appr[:12]
    except Exception:
        logger.debug("pa_brief: notion failed", exc_info=True)
    return parts


def facts_text(parts: dict) -> str:
    sections = [
        _section("On your calendar today", parts.get("calendar") or []),
        _section("Promises from the group due today or overdue", parts.get("group_due") or []),
        _section("Decisions and client preferences picked up since yesterday", parts.get("decisions") or []),
        _section("Posts going live today (not yet marked posted)", parts.get("going_live") or []),
        _section("Waiting for your approval", parts.get("approvals") or []),
        _section("No standup yet", parts.get("no_standup") or []),
        _section("On leave today", parts.get("on_leave") or []),
    ]
    return "\n\n".join(s for s in sections if s)


def build_brief(employee: dict) -> str:
    """The finished brief text for one person (the boss)."""
    now = datetime.now(_IST)
    facts = facts_text(gather(now.strftime("%Y-%m-%d")))
    head = f"Morning brief, {now.strftime('%A %d %b')}"
    if not facts:
        return f"{head}: nothing urgent on my radar, calendar and promises are clear."
    try:
        import whatsapp_agent as wa
        if wa.check_budget_available().get("allowed"):
            identity = {"kind": "employee", "id": employee.get("id"), "name": employee.get("name"),
                        "role": employee.get("role", "")}
            model = wa.get_model_for_task("whatsapp")
            usage: dict = {}
            resp = wa._llm_create(
                model,
                [{"type": "text", "text": wa._system_prompt(identity) + (
                    "\nYou are writing the morning brief you send them unprompted, like a sharp "
                    "personal assistant. Use ONLY the facts given; never invent a name, task, "
                    "time or number. Lead with what needs their attention most. Short lines, "
                    "no headings, no greeting boilerplate, skip empty areas. Output only the message.")}],
                None,
                [{"role": "user", "content": f"{head}\n\n{facts}\n\nWrite the brief."}],
                500, usage)
            text = "".join(getattr(b, "text", "") for b in resp.content
                           if getattr(b, "type", "") == "text").strip()
            wa._record_llm_usage(model, usage, "wa_pa_brief")
            text = wa._clean_followup(text)
            if text:
                return wa._humanize(text)
    except Exception:
        logger.exception("pa_brief: compose failed, sending plain facts")
    return f"{head}\n\n{facts}"


def run_daily() -> dict:
    """Send once per weekday, 09:00-11:00 IST, to the alert recipients."""
    now = datetime.now(_IST)
    if now.weekday() >= 5 or not (9 <= now.hour < 11):
        return {"skipped": "outside window"}
    key = f"pa_brief_sent_{now.strftime('%Y-%m-%d')}"
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM app_settings WHERE key=?", (key,)).fetchone():
            return {"skipped": "already sent today"}
        with conn:      # claim first so two overlapping runs can't both send
            conn.execute("INSERT INTO app_settings (key, value) VALUES (?, '1')", (key,))
    finally:
        conn.close()
    sent = 0
    try:
        import wa_outbox
        import utils
        emps = {e.get("id"): e for e in utils._load_employees().get("employees", [])}
        conn = get_connection()
        row = conn.execute("SELECT value FROM app_settings WHERE key='alert_recipient_ids'").fetchone()
        conn.close()
        ids = [x.strip() for x in str(row[0] if row and row[0] else "emp003").replace(",", " ").split() if x.strip()]
        for uid in ids:
            e = emps.get(uid)
            if not e or not str(e.get("whatsapp") or "").strip():
                continue
            if wa_outbox.enqueue(wa_outbox.wa_jid(e["whatsapp"]), build_brief(e)):
                sent += 1
    except Exception:
        logger.exception("pa_brief: send failed")
    return {"sent": sent}
