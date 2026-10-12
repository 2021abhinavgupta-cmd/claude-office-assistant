"""
mgmt_views.py -- cross-client / cross-person questions for the WhatsApp bot:

  blocked        "show me all blocked projects"
  at_risk        "what is at risk?"
  needs_vg       "what needs VG?"
  completed_week "what did we complete this week?"
  overloaded     "who is overloaded?"
  waiting_on_me  "who is waiting on me?"
  my_approvals   "what needs my approval?"

Every answer is built in code from live data (Notion tasks, standup rows, the
client-portal feedback table, org_profile for DMs) so the numbers are exact;
the model only passes the text on. Each function returns a short WhatsApp-ready
string and never raises.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from db import get_connection

logger = logging.getLogger(__name__)

VIEWS = ("blocked", "at_risk", "needs_vg", "completed_week", "overloaded",
         "waiting_on_me", "my_approvals")
_NOT_A_CLIENT = {"daily standup tasks"}


def _today() -> date:
    from utils import today_ist
    return date.fromisoformat(today_ist())


def _all_clients() -> list:
    """client_card.gather_row() dicts for every real Notion client."""
    import client_card
    import notion_store
    out = []
    if not notion_store.is_configured():
        return out
    for c in notion_store.list_clients():
        if (c.get("name") or "").strip().lower() in _NOT_A_CLIENT:
            continue
        try:
            g = client_card.gather_row(c)
            if g:
                out.append(g)
        except Exception:
            logger.exception("mgmt_views: gather failed for %s", c.get("name"))
    return out


def _dm(g: dict) -> str:
    import client_card
    return client_card._dm_text(g["proj"])


def _title(t: dict) -> str:
    return str(t.get("title") or "Untitled")


def _more(items: list, n: int) -> list:
    shown = list(items[:n])
    if len(items) > n:
        shown.append(f"...and {len(items) - n} more")
    return shown


# ── blocked / risk / needs VG ───────────────────────────────────────────
def blocked() -> str:
    rows = [(g["name"], g["blockers"]) for g in _all_clients() if g["blockers"]]
    if not rows:
        return "No blocked projects right now. Nobody has a blocker logged on a client task."
    lines = [f"Blocked projects ({len(rows)}):"]
    for name, bl in rows:
        lines.append(f"- {name}: " + "; ".join(bl[:3]))
    return "\n".join(lines)


def _risk_reasons(g: dict) -> tuple:
    import client_card as cc
    unready = [t for t in g["this_week"]
               if cc._st(t) not in cc._READY and cc._due(t) <= (_today() + timedelta(days=3)).isoformat()]
    reasons, score = [], 0
    if g["overdue"]:
        reasons.append(f"{len(g['overdue'])} overdue")
        score += len(g["overdue"])
    if unready:
        reasons.append(f"{len(unready)} post{'' if len(unready) == 1 else 's'} due within 3 days not ready")
        score += 3 * len(unready)
    if g["blockers"]:
        reasons.append("blocked: " + "; ".join(g["blockers"][:2]))
        score += 3 * len(g["blockers"])
    if g["feedback_open"]:
        reasons.append(f"{g['feedback_open']} client feedback open")
        score += g["feedback_open"]
    return score, reasons


def at_risk() -> str:
    ranked = []
    for g in _all_clients():
        score, reasons = _risk_reasons(g)
        if score:
            ranked.append((score, g, reasons))
    if not ranked:
        return "Nothing looks at risk right now: no recent overdue work, nothing late for this week, no blockers."
    ranked.sort(key=lambda x: -x[0])
    lines = ["At risk, worst first:"]
    for _, g, reasons in ranked[:6]:
        lines.append(f"- {g['name']} (DM {_dm(g)}): " + ", ".join(reasons))
    lines.append("(Overdue means due in the last 14 days; older unfinished items are ignored.)")
    return "\n".join(lines)


def needs_vg() -> str:
    import client_card as cc
    rows = [(g, cc._needs_vg(g)) for g in _all_clients()]
    rows = [(g, v) for g, v in rows if v != "No"]
    if not rows:
        return "Nothing needs VG right now: no approvals waiting and nothing overdue."
    lines = [f"Needs VG ({len(rows)} client{'' if len(rows) == 1 else 's'}):"]
    for g, v in rows:
        lines.append(f"- {g['name']}: {v[5:-1]}")
        if g["approvals"]:
            lines += ["    " + x for x in _more([_title(t) for t in g["approvals"]], 3)]
    return "\n".join(lines)


# ── people ──────────────────────────────────────────────────────────────
def _roster() -> list:
    import week_digest
    return week_digest._people()


def _first(n: str) -> str:
    return (n or "").strip().split()[0].lower() if (n or "").strip() else ""


def _owners(t: dict) -> list:
    return [_first(x) for x in re.split(r"[;,]", str(t.get("assigned_to") or "")) if x.strip()]


def overloaded() -> str:
    import client_card as cc
    import notion_store
    today = _today()
    roster = _roster()
    load = {e["id"]: {"name": e["name"], "stand": 0, "board": 0} for e in roster}
    try:
        conn = get_connection()
        try:
            for uid, n in conn.execute(
                    "SELECT user_id, COUNT(*) FROM standup_tasks WHERE date=? AND status='pending' "
                    "GROUP BY user_id", (today.isoformat(),)).fetchall():
                if uid in load:
                    load[uid]["stand"] = n
        finally:
            conn.close()
    except Exception:
        logger.exception("mgmt_views: standup load failed")
    lo = (today - timedelta(days=cc.OVERDUE_DAYS)).isoformat()
    hi = (today + timedelta(days=7)).isoformat()
    try:
        tasks = notion_store.list_tasks() if notion_store.is_configured() else []
    except Exception:
        tasks = []
    by_first = {_first(e["name"]): e["id"] for e in roster}
    for t in tasks:
        if cc._st(t) in cc._CLOSED or cc._st(t) in cc._APPROVAL:
            continue
        d = cc._due(t)
        if not d or not (lo <= d <= hi):
            continue
        for o in _owners(t):
            if o in by_first:
                load[by_first[o]]["board"] += 1
    ranked = sorted(((v["stand"] + v["board"], v) for v in load.values() if v["stand"] + v["board"] > 0),
                    key=lambda x: -x[0])
    if not ranked:
        return "Nobody has any open work on the board or their standup right now."
    avg = sum(s for s, _ in ranked) / len(ranked)
    cut = max(8, 1.5 * avg)
    over = [(s, v) for s, v in ranked if s >= cut]
    lines = []
    if over:
        lines.append("Overloaded: " + ", ".join(f"{v['name']} ({s})" for s, v in over) + ".")
    else:
        lines.append("Nobody is overloaded right now.")
    lines.append("Open work (standup today + board tasks due in the next 7 days or the last 14):")
    for s, v in ranked[:8]:
        lines.append(f"- {v['name']}: {s} ({v['stand']} on standup, {v['board']} on the board)")
    return "\n".join(lines)


def completed_week() -> str:
    import client_card as cc
    today = _today()
    mon = today - timedelta(days=today.weekday())
    roster = {e["id"]: e["name"] for e in _roster()}
    lines = [f"Completed this week ({mon.strftime('%d %b')} to {today.strftime('%d %b')}):"]
    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT user_id, COUNT(DISTINCT title) FROM standup_tasks WHERE date BETWEEN ? AND ? "
                "AND status='done' GROUP BY user_id ORDER BY 2 DESC",
                (mon.isoformat(), today.isoformat())).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.exception("mgmt_views: completed query failed")
        rows = []
    rows = [(roster[u], n) for u, n in rows if u in roster]
    total = sum(n for _, n in rows)
    if rows:
        lines.append(f"Standup tasks done: {total} (" + ", ".join(f"{nm} {n}" for nm, n in rows) + ")")
    else:
        lines.append("Standup tasks done: 0")
    live = []
    for g in _all_clients():
        k = [t for t in g["tasks"] if cc._st(t) in cc._LIVE
             and mon.isoformat() <= cc._due(t) <= today.isoformat()]
        if k:
            live.append((g["name"], len(k)))
    if live:
        lines.append(f"Posts live: {sum(n for _, n in live)} (" + ", ".join(f"{c} {n}" for c, n in live) + ")")
    else:
        lines.append("Posts live: 0")
    return "\n".join(lines)


# ── about the asker ─────────────────────────────────────────────────────
def _my_clients(identity: dict) -> tuple:
    """(is_vg, [org client names the asker is DM/co-DM of])."""
    import ops_rhythm
    roster = _roster()
    vg = ops_rhythm._emp_for(ops_rhythm.VG_NAME, roster)
    is_vg = bool(vg and vg["id"] == identity.get("id"))
    mine = [r["client"] for r in ops_rhythm._clients_by_person(roster).get(identity.get("id"), [])]
    return is_vg, mine


def my_approvals(identity: dict) -> str:
    import org_profile
    is_vg, mine = _my_clients(identity)
    items = []
    for g in _all_clients():
        if not g["approvals"]:
            continue
        p = org_profile.find_project(g["name"])
        if is_vg or (p and p["client"] in mine):
            items.append((g["name"], [_title(t) for t in g["approvals"]]))
    n = sum(len(v) for _, v in items)
    if not n:
        scope = "any client" if is_vg else "your clients"
        return f"Nothing is waiting for your approval on {scope}."
    lines = [f"Waiting for your approval ({n}):"]
    for name, titles in items:
        lines.append(f"{name} ({len(titles)})")
        lines += ["- " + x for x in _more(titles, 6)]
    lines.append("Say 'approve <name>' to approve one.")
    return "\n".join(lines)


def waiting_on_me(identity: dict) -> str:
    uid, name = identity.get("id"), identity.get("name") or ""
    first = _first(name)
    parts = []
    ap = my_approvals(identity)
    if ap.startswith("Waiting"):
        parts.append(ap.rsplit("\n", 1)[0])
    try:
        conn = get_connection()
        try:
            given = conn.execute(
                "SELECT title, delegated_from FROM standup_tasks WHERE user_id=? AND date=? AND status='pending' "
                "AND delegated_from IS NOT NULL AND delegated_from<>'' ORDER BY id",
                (uid, _today().isoformat())).fetchall()
            blk = conn.execute(
                "SELECT s.user_id, s.title, s.blocker FROM standup_tasks s WHERE s.date>=? AND s.user_id<>? "
                "AND s.status NOT IN ('done','deleted') AND s.blocker IS NOT NULL AND s.blocker<>''",
                ((_today() - timedelta(days=3)).isoformat(), uid)).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.exception("mgmt_views: waiting_on_me query failed")
        given, blk = [], []
    if given:
        parts.append("Handed to you and still open:\n" + "\n".join(
            f"- {t} (from {frm})" for t, frm in given[:8]))
    roster = {e["id"]: e["name"] for e in _roster()}
    named = [(roster.get(u, u), t, b) for u, t, b in blk if first and re.search(rf"\b{re.escape(first)}\b", b, re.I)]
    if named:
        parts.append("Blocked on you:\n" + "\n".join(f"- {n}: {t} ({b})" for n, t, b in named[:6]))
    if not parts:
        return "Nobody is waiting on you right now: no approvals, nothing handed to you, no blockers naming you."
    return "\n\n".join(parts)


def view(which: str, identity: dict) -> str:
    w = re.sub(r"[^a-z_]", "", str(which or "").lower().replace(" ", "_"))
    try:
        if w in ("blocked", "blocked_projects"):
            return blocked()
        if w in ("at_risk", "risk", "risks"):
            return at_risk()
        if w in ("needs_vg", "vg"):
            return needs_vg()
        if w in ("completed_week", "completed", "done_this_week"):
            return completed_week()
        if w in ("overloaded", "workload"):
            return overloaded()
        if w in ("waiting_on_me", "waiting"):
            return waiting_on_me(identity)
        if w in ("my_approvals", "approvals"):
            return my_approvals(identity)
    except Exception:
        logger.exception("mgmt_views: %s failed", w)
        return "(couldn't work that out just now)"
    return "Pick one of: " + ", ".join(VIEWS)
