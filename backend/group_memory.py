"""Group chat memory for the WhatsApp assistant.

The bridge forwards every text message from an allow-listed (staff-only) group
in small batches; they are stored here with NO AI call, so remembering costs
nothing. The agent then gets the recent chat as context when it is addressed in
the group, and a `read_group_chat` tool to search or summarise older messages.

Only allow-listed groups are ever stored (the same list that gates replies), and
rows older than RETENTION_DAYS are pruned by data_retention.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from db import get_connection

logger = logging.getLogger(__name__)

RETENTION_DAYS = 7   # Railway keeps only a short working copy; the full archive is on the laptop
_MAX_TEXT = 1000
_IST = timezone(timedelta(hours=5, minutes=30))


def _digits(x) -> str:
    return re.sub(r"\D", "", str(x or ""))


def _ensure_table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS wa_group_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id     TEXT NOT NULL,
        group_name   TEXT,
        sender       TEXT,
        sender_name  TEXT,
        text         TEXT NOT NULL,
        wa_id        TEXT,
        created_at   TEXT NOT NULL,
        processed    INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_group_log_g ON wa_group_log (group_id, id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_wa_group_log_u "
                 "ON wa_group_log (group_id, wa_id)")   # NULL wa_id is never a duplicate


def _employee_names() -> dict:
    """last-10-digits of a phone -> employee display name."""
    out = {}
    try:
        import utils
        for e in utils._load_employees().get("employees", []):
            d = _digits(e.get("whatsapp"))
            if d:
                out[d[-10:]] = e.get("name") or ""
    except Exception:
        logger.debug("group_memory: employee names unavailable", exc_info=True)
    return out


def record(group_id: str, group_name: str, messages: list) -> int:
    """Store a batch. Returns how many rows were inserted. The caller must have
    already checked the group is allow-listed."""
    gid = _digits(group_id)
    if not gid or not messages:
        return 0
    names = _employee_names()
    n = 0
    conn = get_connection()
    try:
        _ensure_table(conn)
        with conn:
            for m in messages:
                if not isinstance(m, dict):
                    continue
                text = re.sub(r"\s+", " ", str(m.get("text") or "")).strip()[:_MAX_TEXT]
                if not text:
                    continue
                phone = _digits(m.get("from"))
                name = names.get(phone[-10:]) or str(m.get("name") or "").strip() or phone[-10:] or "someone"
                try:
                    ts = datetime.fromtimestamp(float(m.get("ts")) / 1000.0, timezone.utc)
                except Exception:
                    ts = datetime.now(timezone.utc)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO wa_group_log "
                    "(group_id, group_name, sender, sender_name, text, wa_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (gid, (group_name or "")[:120], phone, name[:80], text,
                     (str(m.get("id") or "").strip() or None), ts.isoformat()))
                n += cur.rowcount or 0
    finally:
        conn.close()
    return n


def _fmt(rows, with_group: bool = False) -> list:
    lines = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["created_at"]).astimezone(_IST)
            when = t.strftime("%a %d %b %H:%M")
        except Exception:
            when = ""
        grp = f"[{r['group_name']}] " if with_group and r["group_name"] else ""
        lines.append(f"{grp}{when} {r['sender_name']}: {r['text']}")
    return lines


def _select(where: str, params: tuple, limit: int):
    conn = get_connection()
    try:
        _ensure_table(conn)
        conn.row_factory = None
        cur = conn.execute(
            f"SELECT group_id, group_name, sender_name, text, created_at FROM wa_group_log "
            f"WHERE {where} ORDER BY id DESC LIMIT ?", params + (limit,))
        cols = ["group_id", "group_name", "sender_name", "text", "created_at"]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    rows.reverse()   # oldest first
    return rows


def first_seen(gids) -> str | None:
    """created_at of the earliest recorded message of these groups."""
    gids = [g for g in (_digits(x) for x in (gids or [])) if g]
    if not gids:
        return None
    conn = get_connection()
    try:
        _ensure_table(conn)
        row = conn.execute(
            f"SELECT MIN(created_at) FROM wa_group_log WHERE group_id IN ({','.join('?' * len(gids))})",
            tuple(gids)).fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def _cutoff(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def recent_text(group_id: str, limit: int = 40, hours: float = 48, max_chars: int = 3000) -> str:
    """The latest messages of one group, oldest first, trimmed to max_chars."""
    gid = _digits(group_id)
    if not gid:
        return ""
    try:
        rows = _select("group_id=? AND created_at>=?", (gid, _cutoff(hours)), limit)
    except Exception:
        logger.debug("group_memory: recent failed", exc_info=True)
        return ""
    lines = _fmt(rows)
    while lines and sum(len(l) + 1 for l in lines) > max_chars:
        lines.pop(0)     # drop the oldest first
    return "\n".join(lines)


def search_text(query: str = "", hours: float = 168, group_ids: set | None = None,
                limit: int = 0, max_chars: int = 0) -> str:
    """Messages matching `query` (every word must appear) across the given
    allow-listed groups, or everything in the window when query is blank."""
    gids = [g for g in (_digits(x) for x in (group_ids or [])) if g]
    if not gids:
        return ""
    where = f"group_id IN ({','.join('?' * len(gids))}) AND created_at>=?"
    params = tuple(gids) + (_cutoff(hours),)
    words = [w for w in re.split(r"\s+", (query or "").strip()) if w][:6]
    for w in words:
        where += " AND LOWER(text) LIKE ?"
        params += (f"%{w.lower()}%",)
    # A summary (no query) must see the WHOLE window, even 100+ messages a day;
    # a keyword search only needs the matching ones.
    if not limit:
        limit = 80 if words else 800
    if not max_chars:
        max_chars = 6000 if words else 22000
    cap = 400 if words else 240   # per-message cap so long pastes don't crowd out the day
    try:
        rows = _select(where, params, limit)
    except Exception:
        logger.debug("group_memory: search failed", exc_info=True)
        return ""
    for r in rows:
        if len(r["text"]) > cap:
            r["text"] = r["text"][:cap].rstrip() + "..."
    lines = _fmt(rows, with_group=len(gids) > 1)
    dropped = 0
    while lines and sum(len(l) + 1 for l in lines) > max_chars:
        lines.pop(0)
        dropped += 1
    if dropped and lines:
        lines.insert(0, f"(older: {dropped} earlier message(s) in this window not shown; "
                        "ask for a shorter time range to see them)")
    return "\n".join(lines)


def prune(days: int = RETENTION_DAYS) -> int:
    conn = get_connection()
    try:
        _ensure_table(conn)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with conn:
            return conn.execute("DELETE FROM wa_group_log WHERE created_at < ?", (cutoff,)).rowcount
    finally:
        conn.close()
