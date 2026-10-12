"""
ops_rhythm.py -- Lumina's side of the MMGA operating rhythm (MMGA OS document):

  Mon  10:00  Weekly Planning   "You have N pieces to plan this week" per client,
                                to each client's DM(s) and to VG (all clients)
  Mon  15:00  Reminder          "Your 4 PM planning meeting starts in 1 hour...",
                                to every DM and VG
  Mon-Fri 17:30  6 PM Review    "These N items are waiting for approval", to VG
                                (everything) and to each DM (their clients)

Delivered as WhatsApp DMs through wa_outbox (the laptop poller sends them), run
from the scheduler every 5 minutes: each message has an IST window and is sent at
most once per day (app_settings claim key). Skipped on weekends, on public
holidays set on the office calendar, and for anyone on approved leave. Nothing
here raises into the scheduler.

"Pieces to plan this week" = social posts dated in the week two weeks from now
(the document's 3-week cycle: plan 2 weeks ahead, produce 1 ahead, publish now).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from db import get_connection

logger = logging.getLogger(__name__)

VG_NAME = "Vidit"
# kind -> (weekday filter, window start HH:MM, window end HH:MM)
_WINDOWS = {
    "planning": ((0,), "10:00", "11:00"),
    "reminder": ((0,), "15:00", "15:45"),
    # "approvals" (Mon-Fri 17:30 review) switched off 2026-10-12 at the user's request;
    # run_kind("approvals") still exists, add the line back to re-enable.
}


def _roster() -> list:
    import week_digest
    return week_digest._people()


def _emp_for(name: str, roster: list) -> dict | None:
    first = re.split(r"[\s(+]", (name or "").strip())[0].lower()
    if not first:
        return None
    for e in roster:
        en = (e.get("name") or "").strip().lower()
        if en and (en == first or en.split()[0] == first or first in en):
            return e
    return None


def _names_in(text: str) -> list:
    """'Vidit (VG)' / 'Meet + Shums' -> ['Vidit', 'Meet', 'Shums']"""
    return [re.split(r"[\s(]", x.strip())[0] for x in re.split(r"[+,&/]", text or "") if x.strip()]


def _clients_by_person(roster: list) -> dict:
    """{emp_id: [org client rows she/he is DM or co-DM of]} for recurring clients."""
    import org_profile
    out: dict = {}
    for r in org_profile.projects():
        for nm in _names_in(r["dm"]) + _names_in(r["co_dm"]):
            e = _emp_for(nm, roster)
            if e:
                out.setdefault(e["id"], [])
                if r not in out[e["id"]]:
                    out[e["id"]].append(r)
    return out


def _on_leave(today: str) -> set:
    try:
        import leave_store
        return set(leave_store.on_leave_ids(today))
    except Exception:
        return set()


def _holiday(today: date) -> bool:
    try:
        import week_digest
        return any(e["kind"] == "holiday" for e in week_digest.entries_by_day(today, today)[today.isoformat()])
    except Exception:
        return False


def _claim(kind: str, today: str) -> bool:
    conn = get_connection()
    try:
        with conn:
            return conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                                (f"rhythm_{kind}_{today}", datetime.utcnow().isoformat())).rowcount > 0
    finally:
        conn.close()


def _release(kind: str, today: str) -> None:
    conn = get_connection()
    try:
        with conn:
            conn.execute("DELETE FROM app_settings WHERE key=?", (f"rhythm_{kind}_{today}",))
    finally:
        conn.close()


def _send(emp: dict, text: str) -> bool:
    import wa_outbox
    jid = wa_outbox.wa_jid(emp.get("whatsapp", ""))
    return bool(jid and wa_outbox.enqueue(jid, text))


# ── message builders ────────────────────────────────────────────────────
def _planning_text(rows: list, mon: date) -> str:
    import client_card
    plan_mon = mon + timedelta(days=14)
    lines, total = [], 0
    for r in rows:
        if r["operating_type"] != "recurring":
            continue
        g = client_card.gather(r["client"])
        if not g:
            continue
        n = len(g["plan_week"])
        total += n
        lines.append(f"{r['client']}: {n}" + ("" if n else " (nothing on the calendar yet)"))
    if not lines:
        return ""
    head = (f"You have {total} piece{'' if total == 1 else 's'} to plan this week "
            f"(content for the week of {plan_mon.strftime('%d %b')}).")
    return ("📋 *Weekly Planning*\n\n" + head + "\n\n" + "\n".join(lines)
            + "\n\nEveryone prepares before the 4 PM planning meeting.")


def _approval_items(rows: list) -> dict:
    """{client: [titles]} of tasks waiting for approval, for the given org client rows."""
    import client_card
    out = {}
    for r in rows:
        g = client_card.gather(r["client"])
        if g and g["approvals"]:
            out[r["client"]] = [str(t.get("title") or "Untitled") for t in g["approvals"]]
    return out


def _approval_text(items: dict) -> str:
    n = sum(len(v) for v in items.values())
    if not n:
        return ""
    lines = ["🕕 *6 PM Review*", "", f"These {n} item{'' if n == 1 else 's'} are waiting for approval:"]
    for client, titles in items.items():
        lines.append("")
        lines.append(f"*{client}* ({len(titles)})")
        lines += [f"- {t}" for t in titles[:5]]
        if len(titles) > 5:
            lines.append(f"- ...and {len(titles) - 5} more")
    return "\n".join(lines)


# ── the tick ────────────────────────────────────────────────────────────
def _notion_down() -> bool:
    try:
        import notion_store
        return (not notion_store.is_configured()) or not notion_store.list_clients()
    except Exception:
        return True


def _in_window(kind: str, now: datetime) -> bool:
    days, a, b = _WINDOWS[kind]
    return now.weekday() in days and a <= now.strftime("%H:%M") < b


def run_kind(kind: str, now: datetime, only_ids: list | None = None) -> dict:
    """Build and queue one message kind for `now`'s date. Returns counts."""
    import org_profile
    roster = _roster()
    by_person = _clients_by_person(roster)
    gone = _on_leave(now.date().isoformat())
    vg = _emp_for(VG_NAME, roster)
    sent = 0

    def ok(e):
        return e and e["id"] not in gone and (not only_ids or e["id"] in only_ids)

    all_rows = org_profile.projects()
    if kind == "planning":
        mon = now.date() - timedelta(days=now.weekday())
        targets = {pid: rows for pid, rows in by_person.items()}
        if vg:
            targets[vg["id"]] = all_rows
        for pid, rows in targets.items():
            e = next((x for x in roster if x["id"] == pid), None)
            if not ok(e):
                continue
            txt = _planning_text(rows, mon)
            if txt and _send(e, txt):
                sent += 1
    elif kind == "reminder":
        ids = set(by_person) | ({vg["id"]} if vg else set())
        txt = ("⏰ *Your 4 PM planning meeting starts in 1 hour.*\n\n"
               "Please update your planned deliverables and blockers.")
        for e in roster:
            if e["id"] in ids and ok(e) and _send(e, txt):
                sent += 1
    elif kind == "approvals":
        every = _approval_items([r for r in all_rows])
        if vg and ok(vg):
            t = _approval_text(every)
            if t and _send(vg, t):
                sent += 1
        for pid, rows in by_person.items():
            e = next((x for x in roster if x["id"] == pid), None)
            if not ok(e) or (vg and e["id"] == vg["id"]):
                continue
            mine = {r["client"]: every[r["client"]] for r in rows if r["client"] in every}
            t = _approval_text(mine)
            if t and _send(e, t):
                sent += 1
    return {"kind": kind, "sent": sent}


def run_tick(now: datetime | None = None, force_kind: str = "", only_ids: list | None = None) -> list:
    """Called by the scheduler every 5 min. force_kind (+ only_ids) is for testing:
    skips the window / once-a-day claim but still honours only_ids."""
    results = []
    try:
        if now is None:
            import week_digest
            now = week_digest._ist_now()
        if force_kind:
            if force_kind in _WINDOWS:
                results.append(run_kind(force_kind, now, only_ids))
            return results
        if now.weekday() >= 5 or _holiday(now.date()):
            return results
        today = now.date().isoformat()
        for kind in _WINDOWS:
            if not _in_window(kind, now) or not _claim(kind, today):
                continue
            try:
                res = run_kind(kind, now)
                results.append(res)
                if res["sent"] == 0 and kind in ("planning", "approvals") and _notion_down():
                    _release(kind, today)  # could not read the CRM; try again in 5 minutes
            except Exception:
                logger.exception("ops_rhythm: %s failed", kind)
                _release(kind, today)  # let the next 5-min tick retry inside the window
    except Exception:
        logger.exception("ops_rhythm: tick failed")
    return results
