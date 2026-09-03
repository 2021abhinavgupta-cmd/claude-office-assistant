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
"""
from __future__ import annotations

import hmac
import io
import logging
import os
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

    if weekend:
        text = ""
    elif not missing:
        text = _ROLLCALL_ALL_IN
    else:
        names = ", ".join(missing)
        # rotate the wording by date so it's not a copy-paste every day
        line = _ROLLCALL_LINES[sum(ord(c) for c in today) % len(_ROLLCALL_LINES)]
        text = line.format(names=names)

    return jsonify({
        "date": today,
        "weekend": weekend,
        "present": present,
        "missing": missing,
        "roster_count": len(roster),
        "text": text,
    })


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
