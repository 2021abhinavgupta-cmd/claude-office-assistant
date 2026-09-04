"""
Companion Blueprint -- endpoints for the always-on laptop companion
(scripts/laptop_agent.py). All token-gated with the same ?token= /
Authorization: Bearer scheme as the storage-sync endpoints (STORAGE_SYNC_TOKEN
or FLASK_SECRET_KEY), so the laptop -- which already holds those tokens --
can call them without a browser session.

Routes:
  GET  /api/companion/digest?window=morning|evening   -- assembled daily brief
  GET  /api/companion/uploads-archive                  -- zip of logs/uploads/
  GET  /api/companion/sheets-health                    -- per-client sync health
  POST /api/companion/sheets-pull-all                  -- reconcile every linked sheet
  GET  /api/companion/tomorrow-live                     -- social posts due live tomorrow
  GET  /api/companion/content-calendar-recipients        -- who to DM it to (default Vidit)
"""
from __future__ import annotations

import hmac
import io
import logging
import os
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from db import get_connection
import utils

logger = logging.getLogger(__name__)
companion_bp = Blueprint("companion", __name__)

_UPLOADS = Path(__file__).parent.parent.parent / "logs" / "uploads"

# task statuses that count as "no longer needs attention"
_CLOSED = {"done", "approved", "posted", "final", "complete", "completed",
           "closed", "published", "live"}


def _auth_ok() -> bool:
    expected = os.getenv("STORAGE_SYNC_TOKEN") or os.getenv("FLASK_SECRET_KEY") or ""
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.args.get("token", "")
    return bool(token) and hmac.compare_digest(token, expected)


def _emp_names() -> dict:
    try:
        return {e.get("id"): e.get("name", e.get("id"))
                for e in utils._load_employees().get("employees", [])}
    except Exception:
        return {}


# ── digest ─────────────────────────────────────────────────────────────────

def _iso(d: str) -> bool:
    return bool(d) and len(str(d)) >= 10 and str(d)[4] == "-" and str(d)[7] == "-"


def _build_digest(window: str) -> dict:
    today = utils.today_ist()
    _dt = datetime.strptime(today, "%Y-%m-%d")
    tomorrow = (_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    # only surface overdue items that are still plausibly actionable -- a task
    # 6 months past due with status "Not Started" is dead weight in a daily brief
    overdue_floor = (_dt - timedelta(days=30)).strftime("%Y-%m-%d")
    names = _emp_names()
    data: dict = {"window": window, "date": today}
    lines: list[str] = []

    # ── tasks (Notion is the source of truth when configured) ──
    overdue, due_today, due_tomorrow = [], [], []
    overdue_total = 0
    try:
        import notion_store
        if notion_store.is_configured():
            for t in notion_store.list_tasks():
                st = str(t.get("status") or "").strip().lower().replace(" ", "_")
                if st in _CLOSED:
                    continue
                due = str(t.get("due_date") or "")[:10]
                if not _iso(due):
                    continue
                label = t.get("title") or "(untitled)"
                who = t.get("assigned_to") or ""
                row = f"{label}" + (f" -> {who}" if who else "") + f" (due {due})"
                if due < today:
                    overdue_total += 1
                    if due >= overdue_floor:
                        overdue.append(row)
                elif due == today:
                    due_today.append(row)
                elif due == tomorrow:
                    due_tomorrow.append(row)
    except Exception:
        logger.debug("digest: task pull failed", exc_info=True)

    data["overdue"] = overdue
    data["overdue_total"] = overdue_total
    data["due_today"] = due_today
    data["due_tomorrow"] = due_tomorrow

    # ── standups today ──
    submitted, done_ct, pending_ct = [], 0, 0
    try:
        conn = get_connection()
        rows = conn.execute("SELECT user_id FROM standups WHERE date=?", (today,)).fetchall()
        submitted = sorted({names.get(r[0], r[0]) for r in rows})
        for status, ct in conn.execute(
            "SELECT status, COUNT(*) FROM standup_tasks WHERE date=? GROUP BY status", (today,)
        ).fetchall():
            if str(status).lower() in ("done", "completed"):
                done_ct += ct
            else:
                pending_ct += ct
        conn.close()
    except Exception:
        logger.debug("digest: standup pull failed", exc_info=True)
    data["standups_submitted"] = submitted
    data["standup_done"] = done_ct
    data["standup_pending"] = pending_ct

    # ── sheet-sync health ──
    sync_bad = []
    try:
        conn = get_connection()
        for cid, cname, ok, lpull in conn.execute(
            "SELECT client_id, client_name, last_push_ok, last_pull_at FROM google_sheet_links"
        ).fetchall():
            if ok == 0:
                sync_bad.append(f"{cname or cid}: last push FAILED")
            elif lpull:
                try:
                    age = datetime.strptime(str(lpull)[:10], "%Y-%m-%d")
                    if (datetime.now() - age).days >= 3:
                        sync_bad.append(f"{cname or cid}: no pull in {(datetime.now()-age).days}d")
                except Exception:
                    pass
        conn.close()
    except Exception:
        logger.debug("digest: sheet health failed", exc_info=True)
    data["sheet_sync_issues"] = sync_bad

    # ── budget ──
    try:
        from budget_tracker import get_usage_summary
        s = get_usage_summary()
        data["budget_percent"] = s.get("percent_used", 0)
        data["budget_spent"] = s.get("total_spent_ever", s.get("monthly_spend"))
    except Exception:
        data["budget_percent"] = None

    # ── render plain text ──
    head = "Good morning" if window == "morning" else "End of day"
    lines.append(f"{head} -- {today}")
    lines.append("")
    if overdue:
        extra = f", {overdue_total} total incl. old" if overdue_total > len(overdue) else ""
        lines.append(f"OVERDUE (last 30d: {len(overdue)}{extra}):")
        lines += [f"  - {r}" for r in overdue[:15]]
        if len(overdue) > 15:
            lines.append(f"  ...and {len(overdue) - 15} more")
        lines.append("")
    if window == "morning":
        lines.append(f"DUE TODAY ({len(due_today)}):")
        lines += [f"  - {r}" for r in due_today[:20]] or ["  (nothing)"]
        lines.append("")
        lines.append(f"DUE TOMORROW ({len(due_tomorrow)}):")
        lines += [f"  - {r}" for r in due_tomorrow[:15]] or ["  (nothing)"]
        lines.append("")
    else:
        lines.append(f"WAS DUE TODAY ({len(due_today)}):")
        lines += [f"  - {r}" for r in due_today[:20]] or ["  (nothing)"]
        lines.append("")
    if submitted:
        lines.append(f"Standups in: {', '.join(submitted)}")
    else:
        lines.append("Standups in: none yet")
    lines.append(f"Standup tasks: {done_ct} done / {pending_ct} pending")
    if sync_bad:
        lines.append("")
        lines.append("SHEET SYNC:")
        lines += [f"  - {r}" for r in sync_bad]
    if data.get("budget_percent") is not None:
        lines.append("")
        lines.append(f"API budget used: {data['budget_percent']}%")

    data["text"] = "\n".join(lines)
    return data


# ── attendance roll-call ───────────────────────────────────────────────────

_INACTIVE = {"inactive", "disabled", "left", "removed", "archived", "former"}

# a few lines so the noon roll-call isn't word-for-word identical every day
_ROLLCALL_LINES = [
    "Noon roll-call. Still missing in action: {names}. The login button doesn't bite. ⏰",
    "It's 12 o'clock and {names} still haven't graced the attendance sheet with their presence. 👀",
    "Half the day's gone and {names} are yet to clock in. Bold strategy.",
    "Roll-call: {names} officially 'not logged in yet'. We'll wait. ⏰",
    "12 o'clock headcount — {names} unaccounted for. Send a search party?",
    "{names}: the check-in button misses you. It's a two-second job, promise. 🙂",
]
_ROLLCALL_ALL_IN = "Noon roll-call: everyone's actually logged in. Someone mark the calendar. ✅"
_ROLLCALL_WA_HINT = (
    "Can't get to Lumina right now? Reply \"lumina in\" here in the group "
    "(or just DM me \"in\") and I'll check you in from WhatsApp."
)


@companion_bp.route("/api/companion/attendance-missing", methods=["GET"])
def companion_attendance_missing():
    """Who on the active roster has NOT checked in today. Powers the laptop
    companion's noon roll-call message to the team WhatsApp group."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401

    today = utils.today_ist()
    try:
        weekend = datetime.strptime(today, "%Y-%m-%d").weekday() >= 5  # 5=Sat 6=Sun
    except Exception:
        weekend = False

    roster = []
    try:
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in _INACTIVE:
                continue
            roster.append((e.get("id", ""), e.get("name") or e.get("id", "")))
    except Exception:
        logger.exception("attendance-missing: roster load failed")
        return jsonify({"error": "roster load failed"}), 500

    checked_in = set()
    try:
        conn = get_connection()
        for row in conn.execute(
            "SELECT user_id FROM daily_attendance "
            "WHERE date=? AND checkin_time IS NOT NULL AND checkin_time<>''",
            (today,),
        ).fetchall():
            checked_in.add(row[0])
        conn.close()
    except Exception:
        logger.exception("attendance-missing: query failed")
        return jsonify({"error": "query failed"}), 500

    present = [n for (i, n) in roster if i in checked_in]
    missing = [n for (i, n) in roster if i not in checked_in]

    # id + whatsapp for the ones missing, for the 10:30 personal nag
    wa_by_id = {}
    try:
        for e in utils._load_employees().get("employees", []):
            wa_by_id[e.get("id", "")] = re.sub(r"\D", "", e.get("whatsapp", "") or "")
    except Exception:
        pass
    missing_detail = [
        {"id": i, "name": n, "whatsapp": wa_by_id.get(i, "")}
        for (i, n) in roster if i not in checked_in
    ]

    if weekend:
        text = ""
    elif not missing:
        text = _ROLLCALL_ALL_IN
    else:
        names = ", ".join(missing)
        # rotate the wording by date so it's not a copy-paste every day
        line = _ROLLCALL_LINES[sum(ord(c) for c in today) % len(_ROLLCALL_LINES)]
        text = line.format(names=names) + "\n\n" + _ROLLCALL_WA_HINT

    return jsonify({
        "date": today,
        "weekend": weekend,
        "present": present,
        "missing": missing,
        "missing_detail": missing_detail,
        "roster_count": len(roster),
        "text": text,
    })


@companion_bp.route("/api/companion/alert-recipients", methods=["GET"])
def companion_alert_recipients():
    """Employee ids in app_settings 'alert_recipient_ids' (default emp003),
    resolved to name + whatsapp -- who the laptop should DM for ops alerts
    (budget crossed 80%, sheet sync broke, ...)."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    ids = ["emp003"]
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key='alert_recipient_ids'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            ids = [x.strip() for x in str(row[0]).replace(",", " ").split() if x.strip()]
    except Exception:
        pass
    by_id = {}
    try:
        for e in utils._load_employees().get("employees", []):
            by_id[e.get("id", "")] = e
    except Exception:
        pass
    out = []
    for i in ids:
        e = by_id.get(i)
        if e and re.sub(r"\D", "", e.get("whatsapp", "") or ""):
            out.append({"id": i, "name": e.get("name") or i,
                        "whatsapp": re.sub(r"\D", "", e.get("whatsapp", ""))})
    return jsonify({"recipients": out})


# ── standup nudge ──────────────────────────────────────────────────────────

@companion_bp.route("/api/companion/standup-missing", methods=["GET"])
def companion_standup_missing():
    """Active employees (with a WhatsApp number) who have NOT added a fresh
    task to today's standup — i.e. they have no `standup_tasks` row for today
    that isn't just a carry-over. Powers the 11:30 personal nudge."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401

    today = utils.today_ist()
    try:
        weekend = datetime.strptime(today, "%Y-%m-%d").weekday() >= 5
    except Exception:
        weekend = False

    emps = []
    try:
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in _INACTIVE:
                continue
            wa = re.sub(r"\D", "", e.get("whatsapp", "") or "")
            if not wa:
                continue
            emps.append({"id": e.get("id", ""),
                         "name": e.get("name") or e.get("id", ""),
                         "whatsapp": wa})
    except Exception:
        logger.exception("standup-missing: roster load failed")
        return jsonify({"error": "roster load failed"}), 500

    have = set()
    try:
        conn = get_connection()
        # A fresh (non-carried) row counts as "added a task today". So does a
        # carried-over row (main task persisting day to day, e.g. Noorish's
        # setup) that's been PATCHed today -- status/blocker/subtasks all
        # bump updated_at (update_my_task, ops.py) -- since ticking subtasks
        # under a standing task IS today's work, even with no new row.
        for row in conn.execute(
            "SELECT DISTINCT user_id FROM standup_tasks "
            "WHERE date=? AND ("
            "  (carried_from IS NULL OR carried_from='')"
            "  OR updated_at IS NOT NULL"
            ")",
            (today,),
        ).fetchall():
            have.add(row[0])
        conn.close()
    except Exception:
        logger.exception("standup-missing: query failed")
        return jsonify({"error": "query failed"}), 500

    missing = [e for e in emps if e["id"] not in have]
    return jsonify({
        "date": today,
        "weekend": weekend,
        "checked": len(emps),
        "missing": missing,
    })


@companion_bp.route("/api/companion/whatsapp-outbox", methods=["GET"])
def companion_whatsapp_outbox():
    """Pending proactive WhatsApp messages for the laptop companion to deliver
    via the local Baileys bridge. Messages older than 24h are expired first
    (a stale 'remind X' nudge delivered a day late is worse than not at all)."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "UPDATE whatsapp_outbox SET status='expired' "
                "WHERE status='pending' AND created_at < ?",
                (cutoff,),
            )
        rows = conn.execute(
            "SELECT id, to_number, body FROM whatsapp_outbox "
            "WHERE status='pending' ORDER BY id LIMIT 50"
        ).fetchall()
        conn.close()
    except Exception:
        logger.exception("companion whatsapp-outbox failed")
        return jsonify({"error": "failed"}), 500
    return jsonify({"messages": [{"id": r[0], "to": r[1], "text": r[2]} for r in rows]})


_OUTBOX_MAX_ATTEMPTS = 3


@companion_bp.route("/api/companion/whatsapp-outbox/ack", methods=["POST"])
def companion_whatsapp_outbox_ack():
    """Laptop reports which queued messages it delivered (or failed to).
    `sent` may be bare ids or {id, wa_id} objects (the WhatsApp message id
    from the bridge, for a delivery receipt). A failed row stays 'pending'
    for retry until it has missed _OUTBOX_MAX_ATTEMPTS times, then 'failed'."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    sent, failed = [], []
    for item in (body.get("sent") or []):
        if isinstance(item, dict) and str(item.get("id", "")).isdigit():
            sent.append((int(item["id"]), item.get("wa_id") or None))
        elif str(item).isdigit():
            sent.append((int(item), None))
    for i in (body.get("failed") or []):
        if str(i).isdigit():
            failed.append(int(i))
    now = datetime.utcnow().isoformat()
    gave_up = 0
    try:
        conn = get_connection()
        with conn:
            for i, wa_id in sent:
                conn.execute(
                    "UPDATE whatsapp_outbox SET status='sent', sent_at=?, wa_message_id=? WHERE id=?",
                    (now, wa_id, i),
                )
            for i in failed:
                row = conn.execute(
                    "SELECT COALESCE(attempts,0) FROM whatsapp_outbox WHERE id=?", (i,)
                ).fetchone()
                att = (row[0] if row else 0) + 1
                if att >= _OUTBOX_MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE whatsapp_outbox SET status='failed', attempts=?, sent_at=? WHERE id=?",
                        (att, now, i),
                    )
                    gave_up += 1
                else:
                    conn.execute(
                        "UPDATE whatsapp_outbox SET attempts=? WHERE id=?", (att, i)
                    )
        conn.close()
    except Exception:
        logger.exception("companion whatsapp-outbox ack failed")
        return jsonify({"error": "failed"}), 500
    return jsonify({"ok": True, "sent": len(sent), "retrying": len(failed) - gave_up,
                    "failed": gave_up})


@companion_bp.route("/api/companion/wa-audit", methods=["GET"])
def companion_wa_audit():
    """Recent writes the WhatsApp agent made on someone's behalf."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except Exception:
        limit = 50
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT created_at, sender_name, scope, action, detail FROM wa_action_log "
            "ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        conn.close()
    except Exception:
        logger.exception("companion wa-audit failed")
        return jsonify({"error": "failed"}), 500
    return jsonify({"actions": [
        {"at": r[0], "by": r[1], "scope": r[2], "action": r[3], "detail": r[4]}
        for r in rows
    ]})


_DONE_WORDS = {"done", "completed", "complete"}


def _eod_rows(today: str) -> dict:
    """{user_id: {"pending":[titles], "done_titles":[titles], "done":n, "total":n}}
    from today's standup_tasks, ignoring deleted/delegated rows."""
    out: dict = {}
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT user_id, title, status FROM standup_tasks "
            "WHERE date=? AND status NOT IN ('deleted','delegated') ORDER BY id",
            (today,),
        ).fetchall()
        conn.close()
    except Exception:
        logger.exception("eod-summary: standup query failed")
        return out
    for uid, title, status in rows:
        d = out.setdefault(uid, {"pending": [], "done_titles": [], "done": 0, "total": 0})
        d["total"] += 1
        if str(status or "").strip().lower() in _DONE_WORDS:
            d["done"] += 1
            d["done_titles"].append(title or "(untitled)")
        else:
            d["pending"].append(title or "(untitled)")
    return out


# sarcasm for a big day — >5 done. rotated by name+date so it's not identical.
_RAISE_LINES = [
    "  {n} tasks done in one day — get this one a raise.",
    "  {n} done today. An early mark tomorrow seems fair.",
    "  {n} tasks cleared in a day — someone approve a holiday.",
    "  {n} done. At this rate they're owed a raise AND an early leave.",
    "  {n} in one day. Give them the afternoon off, they've earned it.",
]


@companion_bp.route("/api/companion/eod-summary", methods=["GET"])
def companion_eod_summary():
    """End-of-day standup rollup for the laptop companion:
      group_text  -- 5pm: short per-person done-count line for the team group
      per_person  -- 7:30pm: each person's still-open tasks for their own DM
      leads_text / leads -- 7:30pm: cross-team 'not done' list for the leads
    """
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401

    today = utils.today_ist()
    try:
        weekend = datetime.strptime(today, "%Y-%m-%d").weekday() >= 5
    except Exception:
        weekend = False

    roster = []
    try:
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in _INACTIVE:
                continue
            roster.append({
                "id": e.get("id", ""),
                "name": e.get("name") or e.get("id", ""),
                "whatsapp": re.sub(r"\D", "", e.get("whatsapp", "") or ""),
            })
    except Exception:
        logger.exception("eod-summary: roster load failed")
        return jsonify({"error": "roster load failed"}), 500

    stats = _eod_rows(today)

    lead_ids = ["emp001", "emp004"]   # Vidit, Kshitij — overridable
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key='eod_lead_ids'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            lead_ids = [x.strip() for x in str(row[0]).replace(",", " ").split() if x.strip()]
    except Exception:
        pass

    per_person, with_tasks, no_standup = [], [], []
    done_total = task_total = 0
    for r in roster:
        s = stats.get(r["id"])
        if not s:
            no_standup.append(r["name"])
            continue
        done_total += s["done"]
        task_total += s["total"]
        with_tasks.append((r, s))
        per_person.append({
            "id": r["id"], "name": r["name"], "whatsapp": r["whatsapp"],
            "pending": s["pending"], "done_titles": s["done_titles"],
            "done": s["done"], "total": s["total"],
        })

    if with_tasks:
        gl = [f"Tasks done today: {done_total}/{task_total} across the team."]
        gl += [f"{r['name']} {s['done']}/{s['total']}" for r, s in with_tasks]
        if no_standup:
            gl.append("No standup yet: " + ", ".join(no_standup))
        group_text = "\n".join(gl)
    else:
        group_text = ""

    # full per-person breakdown for the leads: every task today, marked
    # done / not done, plus a raise-or-holiday jab for a 5+ day.
    lead_blocks = []
    for r, s in with_tasks:
        blk = [f"{r['name']} — {s['done']}/{s['total']} done"]
        blk += [f"  done: {t}" for t in s["done_titles"][:15]]
        blk += [f"  not done: {t}" for t in s["pending"][:15]]
        if s["done"] > 5:
            line = _RAISE_LINES[(sum(ord(c) for c in r["name"] + today))
                                % len(_RAISE_LINES)]
            blk.append(line.format(n=s["done"]))
        lead_blocks.append("\n".join(blk))
    if no_standup:
        lead_blocks.append("No standup submitted: " + ", ".join(no_standup))
    if lead_blocks:
        leads_text = "EOD check — everyone's tasks for today:\n\n" + "\n\n".join(lead_blocks)
    else:
        leads_text = "EOD check: no standup tasks logged today."

    by_id = {r["id"]: r for r in roster}
    leads = [{"name": by_id[i]["name"], "whatsapp": by_id[i]["whatsapp"]}
             for i in lead_ids if i in by_id and by_id[i]["whatsapp"]]

    return jsonify({
        "date": today, "weekend": weekend,
        "group_text": group_text,
        "leads_text": leads_text,
        "leads": leads,
        "per_person": per_person,
    })


@companion_bp.route("/api/companion/weekly-summary", methods=["GET"])
def companion_weekly_summary():
    """Per-person completed-vs-total standup tasks for the current week
    (Monday -> today). Powers the Friday 6pm group wrap-up."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    today = utils.today_ist()
    try:
        d0 = datetime.strptime(today, "%Y-%m-%d")
    except Exception:
        return jsonify({"error": "bad date"}), 500
    monday = (d0 - timedelta(days=d0.weekday())).strftime("%Y-%m-%d")

    # A still-pending task gets a brand-new standup_tasks row every day it
    # carries over (same title, same carried_from = the ORIGIN day it first
    # went unfinished -- gotcha #7/#8) -- a plain COUNT(*) across a multi-day
    # range double(triple, ...)-counts that one task once per day it was
    # outstanding, inflating the denominator (and, for a task never
    # finished, contributing 0 done but N total, dragging the ratio down
    # for no real reason). Count distinct task LINEAGES for the total
    # instead, same idea as get_velocity_summary()'s dedup (gotcha #77) --
    # but that one's key is title+carried_from ALONE, which only works
    # because it deliberately excludes origin rows (carried_from IS NOT
    # NULL, "has this been carried at least once"). Here every row counts,
    # including a lineage's very first day, whose OWN row always has
    # carried_from=NULL (it hasn't been carried FROM anywhere yet) -- so the
    # dedup key has to fall back to that row's own `date` for the origin
    # day, or day 1 and days 2+ of the same lineage split into two "tasks".
    # `done` doesn't need any of this -- a task is only ever marked done on
    # the one row/day that happened, carry-over never re-inserts a done row.
    agg: dict = {}
    try:
        conn = get_connection()
        for uid, ct in conn.execute(
            "SELECT user_id, COUNT(DISTINCT title || '|' || COALESCE(carried_from, date)) "
            "FROM standup_tasks WHERE date >= ? AND date <= ? "
            "AND status NOT IN ('deleted','delegated') GROUP BY user_id",
            (monday, today)
        ).fetchall():
            agg.setdefault(uid, [0, 0])[1] = ct
        for uid, ct in conn.execute(
            "SELECT user_id, COUNT(*) FROM standup_tasks "
            "WHERE date >= ? AND date <= ? AND status IN ('done','completed','complete') "
            "GROUP BY user_id", (monday, today)
        ).fetchall():
            agg.setdefault(uid, [0, 0])[0] = ct
        conn.close()
    except Exception:
        logger.exception("weekly-summary: query failed")
        return jsonify({"error": "query failed"}), 500

    active = {}
    try:
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in _INACTIVE:
                continue
            active[e.get("id", "")] = e.get("name") or e.get("id", "")
    except Exception:
        pass

    people = sorted(
        ((active[u], a[0], a[1]) for u, a in agg.items() if u in active),
        key=lambda x: (-x[1], x[0]),
    )
    td = sum(p[1] for p in people)
    tt = sum(p[2] for p in people)
    if people:
        lines = [f"Week wrap ({monday} to {today}): {td}/{tt} tasks done."]
        lines += [f"{n} {d}/{t}" for n, d, t in people]
        text = "\n".join(lines)
    else:
        text = ""
    return jsonify({"date": today, "monday": monday, "weekday": d0.weekday(),
                    "done": td, "total": tt, "text": text})


@companion_bp.route("/api/companion/digest", methods=["GET"])
def companion_digest():
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    window = request.args.get("window", "morning").lower()
    if window not in ("morning", "evening"):
        window = "morning"
    try:
        return jsonify(_build_digest(window))
    except Exception:
        logger.exception("companion digest failed")
        return jsonify({"error": "digest failed"}), 500


# ── uploads archive ────────────────────────────────────────────────────────

@companion_bp.route("/api/companion/uploads-archive", methods=["GET"])
def companion_uploads_archive():
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not _UPLOADS.exists():
        return jsonify({"error": "no uploads dir", "files": 0}), 404
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in _UPLOADS.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(_UPLOADS).as_posix())
                n += 1
    if n == 0:
        return jsonify({"error": "uploads dir empty", "files": 0}), 404
    buf.seek(0)
    stamp = utils.today_ist()
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"uploads-{stamp}.zip")


# ── sheets health / bulk pull ──────────────────────────────────────────────

@companion_bp.route("/api/companion/sheets-health", methods=["GET"])
def companion_sheets_health():
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    out = []
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT client_id, client_name, last_push_at, last_push_ok, "
            "last_pull_at, last_pull_summary FROM google_sheet_links"
        ).fetchall()
        conn.close()
        for cid, cname, lpush, ok, lpull, summ in rows:
            stale = False
            if lpull:
                try:
                    d = datetime.strptime(str(lpull)[:10], "%Y-%m-%d")
                    stale = (datetime.now() - d).days >= 3
                except Exception:
                    pass
            out.append({
                "client_id": cid, "client_name": cname,
                "last_push_at": lpush, "last_push_ok": (None if ok is None else bool(ok)),
                "last_pull_at": lpull, "last_pull_summary": summ,
                "push_failing": ok == 0, "pull_stale": stale,
            })
    except Exception:
        logger.exception("companion sheets-health failed")
        return jsonify({"error": "failed"}), 500
    issues = [r for r in out if r["push_failing"] or r["pull_stale"]]
    return jsonify({"links": out, "count": len(out), "issues": len(issues)})


@companion_bp.route("/api/companion/sheets-pull-all", methods=["POST"])
def companion_sheets_pull_all():
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    from routes.sheets_sync import pull_one_link
    results = []
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT client_id, spreadsheet_id, link_token, is_notion, client_name, "
            "linked_by, multi_tab FROM google_sheet_links"
        ).fetchall()
        conn.close()
    except Exception:
        logger.exception("companion pull-all: link list failed")
        return jsonify({"error": "failed"}), 500

    for r in rows:
        link = {"client_id": r[0], "spreadsheet_id": r[1], "is_notion": bool(r[3]),
                "client_name": r[4], "linked_by": r[5], "multi_tab": bool(r[6])}
        try:
            summary = pull_one_link(link, r[2])
            results.append({"client": r[4] or r[0], "ok": True, **summary})
        except Exception as e:
            logger.warning("companion pull-all: %s failed: %s", r[4] or r[0], e)
            results.append({"client": r[4] or r[0], "ok": False, "error": str(e)[:200]})
    return jsonify({"pulled": len(results), "results": results})


@companion_bp.route("/api/companion/content-calendar-recipients", methods=["GET"])
def companion_content_calendar_recipients():
    """Employee ids in app_settings 'content_calendar_recipient_ids' (default
    emp001 -- Vidit), resolved to name + whatsapp. Who gets DM'd the
    tomorrow-live reminder."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    ids = ["emp001"]
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key='content_calendar_recipient_ids'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            ids = [x.strip() for x in str(row[0]).replace(",", " ").split() if x.strip()]
    except Exception:
        pass
    by_id = {}
    try:
        for e in utils._load_employees().get("employees", []):
            by_id[e.get("id", "")] = e
    except Exception:
        pass
    out = []
    for i in ids:
        e = by_id.get(i)
        if e and re.sub(r"\D", "", e.get("whatsapp", "") or ""):
            out.append({"id": i, "name": e.get("name") or i,
                        "whatsapp": re.sub(r"\D", "", e.get("whatsapp", ""))})
    return jsonify({"recipients": out})


def _social_task(t: dict) -> bool:
    """Same social-media detection used everywhere else (gotcha #87 fifth
    audit round -- unanchored, matches a bracket type anywhere in the title,
    not just at position 0)."""
    ty = (t.get("type") or t.get("service") or "").lower()
    return ("social" in ty) or bool(re.search(
        r"\[(story|reel|static|carousel|post|video)\]",
        t.get("title") or "", re.I))


@companion_bp.route("/api/companion/tomorrow-live", methods=["GET"])
def companion_tomorrow_live():
    """Every social-media task (Sheets row) due to go live tomorrow, pulled
    straight from Notion -- powers the evening reminder to Vidit so nothing
    scheduled for the next day gets missed. Flags anything not yet approved
    or missing a caption so it reads as an actionable heads-up, not just a
    list."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    tomorrow = (datetime.strptime(utils.today_ist(), "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    tasks = []
    try:
        import notion_store
        if notion_store.is_configured():
            tasks = notion_store.list_tasks()
    except Exception:
        logger.exception("companion tomorrow-live: notion read failed")

    rows = [t for t in tasks
            if str(t.get("due_date") or "")[:10] == tomorrow
            and _social_task(t)
            and (t.get("status") or "").strip().lower() not in ("cancelled", "canceled")]
    rows.sort(key=lambda t: (t.get("client_name") or "", t.get("title") or ""))

    items = []
    for t in rows:
        status = (t.get("status") or "").strip().lower()
        ready = status in ("approved", "final", "scheduled") and bool((t.get("caption") or "").strip())
        items.append({
            "client": t.get("client_name") or "?",
            "type": t.get("type") or "post",
            "title": t.get("title") or "",
            "assigned_to": t.get("assigned_to") or "unassigned",
            "status": status or "planned",
            "has_caption": bool((t.get("caption") or "").strip()),
            "ready": ready,
        })

    if not items:
        text = f"Nothing scheduled to go live tomorrow ({tomorrow})."
    else:
        not_ready = [i for i in items if not i["ready"]]
        lines = [f"Going live tomorrow ({tomorrow}) -- {len(items)} post(s):"]
        for i in items:
            flag = "" if i["ready"] else "  <- NOT READY"
            lines.append(
                f"  {i['client']} | {i['type']} | {i['title']} | "
                f"{i['assigned_to']} | {i['status']}"
                f"{'' if i['has_caption'] else ' | no caption'}{flag}"
            )
        if not_ready:
            lines.append(f"\n{len(not_ready)} of {len(items)} not yet approved/captioned.")
        text = "\n".join(lines)

    return jsonify({"date": tomorrow, "count": len(items), "items": items, "text": text})
