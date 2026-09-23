"""Initiative engine -- the bot notices loose ends and reaches out first.

Everything else proactive in this codebase fires on a fixed clock and sends
a canned string (scripts/laptop_agent.py's login/EOD/roll-call jobs,
task_scheduler's 08:00 overdue escalation). This module is the other half:
it watches for things that have quietly stalled and lets the AGENT compose
the nudge, so it reads like the assistant noticed rather than like a cron
job fired.

DELIBERATELY NON-OVERLAPPING with the existing nudges. Those already cover
"you haven't checked in", "you have no standup yet", "here's today's
pending list" and "this task is overdue". Re-reporting any of that would
make this the fifth thing DMing someone the same fact. What nothing covered
before, and what this looks for:

  stale_task        a standup task that has rolled over for DAYS, not just
                    "pending today" -- the real loose end
  assigned_stalled  work THIS person put on a teammate that hasn't moved,
                    told to the person who assigned it (nobody was ever
                    told their delegation went nowhere)
  approval          something they submitted that's been awaiting sign-off
                    too long
  rule              a recurring standing instruction coming due
                    (standing_rules.py)

Safety rails, because an engine that messages people unprompted is only
useful if it stays quiet most of the time:
  - weekday + working-hours window only
  - people on leave / inactive / with no WhatsApp number are skipped
  - per-(person, kind, ref) cooldown, so one stalled task is mentioned once
    a day at most, not every sweep
  - a hard per-person daily cap on follow-up messages

Delivery is wa_outbox (the laptop poller drains it), so this runs entirely
on Railway with no companion-side change and no bridge restart.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from db import get_connection

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Working-hours window (IST) -- nothing is sent outside it.
WINDOW_START_HOUR = 10
WINDOW_END_HOUR = 19

# A standup task that has carried over for at least this many days counts as
# stalled. 3 means "it survived the weekend or two full working days".
STALE_TASK_DAYS = 3
# A delegated task nobody has touched for this long gets reported back to
# whoever assigned it.
ASSIGNED_STALLED_DAYS = 2
# Something awaiting sign-off longer than this is worth chasing.
APPROVAL_STALE_DAYS = 2

# Don't repeat the same specific item to the same person more often than this.
ITEM_COOLDOWN_HOURS = 20
# Hard ceiling on unprompted messages per person per day.
MAX_PER_PERSON_PER_DAY = 2

_INACTIVE = {"inactive", "disabled", "left", "removed", "archived", "former"}
_CLOSED = {"done", "complete", "completed", "approved", "posted", "final",
           "cancelled", "canceled", "archived", "delegated", "deleted"}
_APPROVAL = {"need for approval", "need_for_approval", "pending review",
             "in review", "awaiting approval", "submitted"}

_DDL = """CREATE TABLE IF NOT EXISTS followup_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    ref       TEXT NOT NULL,
    sent_at   TEXT DEFAULT (datetime('now'))
)"""


def _ensure(conn) -> None:
    conn.execute(_DDL)


def _now_ist() -> datetime:
    return datetime.now(_IST)


def _today_ist() -> str:
    return _now_ist().strftime("%Y-%m-%d")


def _norm_ref(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())[:180]


def in_working_window(now: datetime | None = None) -> bool:
    """Weekday, inside the IST working-hours window."""
    n = now or _now_ist()
    if n.weekday() >= 5:
        return False
    return WINDOW_START_HOUR <= n.hour < WINDOW_END_HOUR


# ── dedup log ────────────────────────────────────────────────────────────────

def was_recently_sent(user_id: str, kind: str, ref: str,
                      hours: int = ITEM_COOLDOWN_HOURS) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = get_connection()
    try:
        _ensure(conn)
        row = conn.execute(
            "SELECT 1 FROM followup_log WHERE user_id=? AND kind=? AND ref=? "
            "AND sent_at >= ? LIMIT 1",
            (user_id, kind, _norm_ref(ref), cutoff),
        ).fetchone()
        return bool(row)
    except Exception:
        logger.debug("followups: was_recently_sent failed", exc_info=True)
        # Fail CLOSED -- on a DB hiccup, stay quiet rather than risk spamming.
        return True
    finally:
        conn.close()


def sent_today_count(user_id: str) -> int:
    """How many follow-up items have already gone to this person today (IST)."""
    start = _now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = start.astimezone(timezone.utc).isoformat()
    conn = get_connection()
    try:
        _ensure(conn)
        row = conn.execute(
            "SELECT COUNT(DISTINCT ref) FROM followup_log "
            "WHERE user_id=? AND sent_at >= ?",
            (user_id, cutoff),
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        logger.debug("followups: sent_today_count failed", exc_info=True)
        return MAX_PER_PERSON_PER_DAY  # fail closed
    finally:
        conn.close()


def mark_sent(user_id: str, items: list) -> None:
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            for it in items:
                conn.execute(
                    "INSERT INTO followup_log (user_id, kind, ref, sent_at) "
                    "VALUES (?,?,?,?)",
                    (user_id, it.get("kind", "?"), _norm_ref(it.get("ref", "")),
                     datetime.now(timezone.utc).isoformat()),
                )
    except Exception:
        logger.exception("followups: mark_sent failed")
    finally:
        conn.close()


def prune_log(days: int = 30) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            cur = conn.execute("DELETE FROM followup_log WHERE sent_at < ?", (cutoff,))
        return cur.rowcount or 0
    except Exception:
        return 0
    finally:
        conn.close()


# ── detection ────────────────────────────────────────────────────────────────

def _days_since(date_str: str, today: str) -> int:
    try:
        a = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        b = datetime.strptime(today, "%Y-%m-%d").date()
        return (b - a).days
    except Exception:
        return 0


def _stale_standup_tasks(user_id: str, today: str) -> list:
    """Tasks still pending on today's standup that first went unfinished
    STALE_TASK_DAYS or more ago. `carried_from` is set once, to the day the
    task first rolled over, and never updated after -- so it is exactly the
    'how long has this been hanging around' signal (see CLAUDE.md gotcha
    #77, which relies on the same property)."""
    out = []
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT title, carried_from, blocker FROM standup_tasks "
            "WHERE user_id=? AND date=? AND status NOT IN "
            "('done','deleted','delegated') "
            "AND carried_from IS NOT NULL AND carried_from != ''",
            (user_id, today),
        ).fetchall()
    except Exception:
        logger.debug("followups: stale standup query failed", exc_info=True)
        return []
    finally:
        conn.close()
    for title, carried_from, blocker in rows:
        age = _days_since(carried_from, today)
        if age >= STALE_TASK_DAYS:
            out.append({
                "kind": "stale_task",
                "ref": title,
                "days": age,
                "text": (f"\"{title}\" has been sitting on their standup for "
                         f"{age} days"
                         + (f" (blocker noted: {blocker})" if blocker else "")),
            })
    return out


def _stalled_delegations(user_id: str, name: str, today: str,
                         names_by_id: dict) -> list:
    """Work this person put on a teammate that still hasn't moved. Reported
    to the ASSIGNER -- nothing else in this codebase ever tells someone
    their delegation went nowhere.

    Matched on NAME, not user_id: every writer of this column stores the
    assigner's display name (`identity["name"]` in whatsapp_agent's
    assign_task / delegate_my_task / create_task, and routes/ops.py's
    delegate_task), never their employee id.
    """
    if not name:
        return []
    out = []
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT user_id, title, MIN(date) AS first_seen FROM standup_tasks "
            "WHERE LOWER(delegated_from)=LOWER(?) AND status NOT IN "
            "('done','deleted','delegated') "
            "GROUP BY user_id, title",
            (name,),
        ).fetchall()
    except Exception:
        logger.debug("followups: delegation query failed", exc_info=True)
        return []
    finally:
        conn.close()
    for owner, title, first_seen in rows:
        if owner == user_id:
            continue  # their own task, covered by stale_task
        age = _days_since(first_seen, today)
        if age >= ASSIGNED_STALLED_DAYS:
            who = names_by_id.get(owner, owner)
            out.append({
                "kind": "assigned_stalled",
                "ref": f"{owner}:{title}",
                "days": age,
                "text": (f"they gave {who} \"{title}\" {age} days ago and it "
                         "still hasn't moved"),
            })
    return out


def _stale_approvals(name: str, today: str) -> list:
    """Their tasks parked in an approval state for too long."""
    out = []
    try:
        import notion_store
        if not notion_store.is_configured():
            return []
        tasks = notion_store.list_tasks()
    except Exception:
        logger.debug("followups: approval lookup failed", exc_info=True)
        return []
    nl = (name or "").strip().lower()
    if not nl:
        return []
    for t in tasks or []:
        status = str(t.get("status") or "").strip().lower().replace("_", " ")
        if status not in {s.replace("_", " ") for s in _APPROVAL}:
            continue
        assignee = str(t.get("assigned_to") or "").lower()
        if nl not in assignee:
            continue
        # last_edited_at is the only "how long has it sat here" signal Notion
        # tasks carry in this schema; fall back to due_date when it's absent.
        stamp = t.get("last_edited_at") or t.get("due_date") or ""
        age = _days_since(stamp, today) if stamp else 0
        if age >= APPROVAL_STALE_DAYS:
            title = t.get("title") or "an untitled task"
            client = t.get("client_name") or ""
            out.append({
                "kind": "approval",
                "ref": t.get("notion_id") or title,
                "days": age,
                "text": (f"\"{title}\"" + (f" ({client})" if client else "")
                         + f" has been waiting on approval for {age} days"),
            })
    return out


def _due_rules(user_id: str, now: datetime) -> list:
    try:
        import standing_rules
        due = standing_rules.due_reminders(
            user_id, now.weekday(), now.strftime("%H:%M"))
    except Exception:
        logger.debug("followups: standing-rule lookup failed", exc_info=True)
        return []
    return [{
        "kind": "rule",
        "ref": f"rule:{r['id']}",
        "days": 0,
        "text": f"their standing instruction is due now: {r['rule']}",
    } for r in due]


def loose_ends_for(employee: dict, *, today: str | None = None,
                   now: datetime | None = None,
                   names_by_id: dict | None = None) -> list:
    """Everything currently hanging for one person, newest signal last.
    Pure reads -- no messages sent, no state changed. Also the backing
    query for the on-demand `get_followups` agent tool."""
    today = today or _today_ist()
    now = now or _now_ist()
    names_by_id = names_by_id or {}
    uid = employee.get("id") or ""
    name = employee.get("name") or ""
    if not uid:
        return []
    items = []
    items += _due_rules(uid, now)
    items += _stale_standup_tasks(uid, today)
    items += _stalled_delegations(uid, name, today, names_by_id)
    items += _stale_approvals(name, today)
    # Longest-stalled first: if the per-person cap trims the list, the worst
    # loose end is the one that survives.
    items.sort(key=lambda i: (i["kind"] != "rule", -int(i.get("days") or 0)))
    return items


# ── sweep ────────────────────────────────────────────────────────────────────

def _eligible_employees() -> list:
    try:
        import utils
        emps = utils._load_employees().get("employees", [])
    except Exception:
        logger.debug("followups: employee load failed", exc_info=True)
        return []
    out = []
    for e in emps:
        if str(e.get("status", "active")).strip().lower() in _INACTIVE:
            continue
        if not str(e.get("whatsapp") or "").strip():
            continue
        out.append(e)
    return out


def sweep(*, dry_run: bool = False) -> dict:
    """Find loose ends across the team and send at most one composed nudge
    per person. Returns a summary dict (also what the companion endpoint
    reports, so a sweep is inspectable rather than a black box)."""
    now = _now_ist()
    if not in_working_window(now):
        return {"skipped": "outside working window",
                "weekday": now.weekday(), "hour": now.hour, "sent": 0}

    today = now.strftime("%Y-%m-%d")
    employees = _eligible_employees()
    names_by_id = {e.get("id"): e.get("name") for e in employees}

    on_leave = set()
    try:
        import leave_store
        on_leave = leave_store.on_leave_ids(today)
    except Exception:
        logger.debug("followups: leave lookup failed", exc_info=True)

    sent = 0
    details = []
    for emp in employees:
        uid = emp.get("id")
        if uid in on_leave:
            continue
        try:
            remaining = MAX_PER_PERSON_PER_DAY - sent_today_count(uid)
            if remaining <= 0:
                continue
            items = loose_ends_for(emp, today=today, now=now,
                                   names_by_id=names_by_id)
            fresh = [
                it for it in items
                if not was_recently_sent(uid, it["kind"], it["ref"])
            ][:remaining]
            if not fresh:
                continue
            if dry_run:
                details.append({"user_id": uid, "name": emp.get("name"),
                                "would_send": [i["text"] for i in fresh]})
                continue

            text = _compose(emp, fresh)
            if not text:
                continue
            import wa_outbox
            if wa_outbox.enqueue(wa_outbox.wa_jid(emp.get("whatsapp", "")), text):
                mark_sent(uid, fresh)
                sent += 1
                details.append({"user_id": uid, "name": emp.get("name"),
                                "items": len(fresh)})
        except Exception:
            logger.exception("followups: sweep failed for %s", uid)

    prune_log()
    return {"sent": sent, "checked": len(employees), "details": details,
            "dry_run": dry_run}


def _fallback_text(employee: dict, items: list) -> str:
    """Deterministic nudge, used when the agent can't compose one (budget
    exhausted, model error). The feature still works with zero LLM spend --
    it just sounds like a cron job instead of an assistant."""
    lead = f"{employee.get('name', 'Hi')} — a couple of things still hanging:"
    if len(items) == 1:
        lead = f"{employee.get('name', 'Hi')} — one thing still hanging:"
    return lead + "\n" + "\n".join(f"- {i['text']}" for i in items)


def _compose(employee: dict, items: list) -> str:
    """Let the agent write the nudge in its own voice; fall back to plain
    text on any failure."""
    try:
        import whatsapp_agent
        text = whatsapp_agent.compose_followup(employee, items)
        if text:
            return text
    except Exception:
        logger.exception("followups: agent compose failed, using fallback")
    return _fallback_text(employee, items)
