"""
group_digest.py -- rigid, verifiable answers about what was said in the group.

A language model summarising a chat log will, sooner or later, put words in
people's mouths ("Happy said omotec is not working" -- nobody did). So for the
plain "what was discussed in the group" questions the reply is built here, in
code, from the stored messages:

  * every line is a real stored message (time, speaker, text), verbatim;
  * the only thing a model may ever do is GROUP message numbers into topics
    (long days only), and even then the code writes the topic header from the
    data (counts, speakers, times, a quoted first line) and rejects a model
    title that is not made of words found in those messages;
  * it says honestly when recording started later than the window asked for.

`quotes_ok()` is the safety net for the free-form model path: any "quoted
text" in a reply must exist in the log, else the caller swaps the reply for
this module's verbatim digest.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
MAX_REPLY = 3800          # WhatsApp is happy to ~4000
VERBATIM_MAX_MSGS = 45    # above this (or too long) -> topic view

_ASK = re.compile(
    r"\b(?:discuss\w*|summari[sz]e|summary|recap|what (?:was|were|did|have|has|is) .{0,30}"
    r"(?:said|talk\w*|happen\w*|going on|mention\w*|decid\w*|up)|"
    r"catch me up|i missed|talked about|said)\b", re.I)
_GROUPWORD = re.compile(r"\b(?:group|wizards|team chat)\b", re.I)
_URL = re.compile(r"https?://\S+", re.I)


# ---------- window -------------------------------------------------------
def _window(text: str, now: datetime | None = None):
    """(start_utc_iso, end_utc_iso, label) in IST terms; default = today."""
    now = now or datetime.now(_IST)
    t = text.lower()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.search(r"(?:last|past)\s+(\d{1,3})\s*(hour|hr|h|day|d)s?\b", t)
    if m:
        n = int(m.group(1))
        delta = timedelta(hours=n) if m.group(2).startswith("h") else timedelta(days=n)
        return now - delta, now, f"last {n} {'hours' if m.group(2).startswith('h') else 'days'}"
    if "yesterday" in t:
        return day0 - timedelta(days=1), day0, "yesterday"
    if re.search(r"this week|week so far", t):
        mon = day0 - timedelta(days=day0.weekday())
        return mon, now, "this week"
    return day0, now, "today"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---------- filters ------------------------------------------------------
def _people_in(text: str) -> list:
    out = []
    try:
        import utils
        for e in utils._load_employees().get("employees", []):
            nm = str(e.get("name") or "").strip()
            if not nm:
                continue
            first = nm.split()[0]
            if len(first) >= 3 and re.search(rf"\b{re.escape(first)}\b", text, re.I):
                out.append(first.lower())
    except Exception:
        pass
    return out


def _clients_in(text: str) -> list:
    """Client names to filter on when the question is about 'client things'."""
    if not re.search(r"\bclients?\b", text, re.I):
        return []
    try:
        import org_profile
        names = [str(p.get("client") or "") for p in org_profile.projects()]
    except Exception:
        names = []
    return [n for n in names if n]


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# ---------- rendering ----------------------------------------------------
def _domain(m) -> str:
    u = m.group(0).rstrip(".,)")
    parts = u.split("/")
    host = parts[2] if len(parts) > 2 else "link"
    return "[link: " + re.sub(r"^www\.", "", host) + "]"


def _clean(text: str, cap: int) -> str:
    t = _URL.sub(_domain, text)
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= cap else t[:cap].rstrip() + "..."


def _hm(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(_IST).strftime("%H:%M")
    except Exception:
        return "--:--"


def _day(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(_IST).strftime("%a %d %b")
    except Exception:
        return ""


def _speakers(rows) -> str:
    c = {}
    for r in rows:
        c[r["sender_name"]] = c.get(r["sender_name"], 0) + 1
    return ", ".join(f"{n} {k}" for n, k in sorted(c.items(), key=lambda x: -x[1]))


def _line(r, cap: int, with_day: bool) -> str:
    d = f"{_day(r['created_at'])} " if with_day else ""
    return f"{d}{_hm(r['created_at'])} *{r['sender_name']}*: {_clean(r['text'], cap)}"


def _verbatim(rows, label: str, notes: list) -> str | None:
    multi_day = len({_day(r["created_at"]) for r in rows}) > 1
    head = (f"*Group chat, {label}*: {len(rows)} message(s), "
            f"{_hm(rows[0]['created_at'])} to {_hm(rows[-1]['created_at'])}\n"
            f"Spoke: {_speakers(rows)}\n")
    foot = "\n".join(notes + ["These are the exact messages, nothing added."])
    for cap in (200, 140, 100, 70):
        body = "\n".join(_line(r, cap, multi_day) for r in rows)
        out = f"{head}\n{body}\n\n{foot}"
        if len(out) <= MAX_REPLY:
            return out
    return None


def _title_ok(title: str, rows) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3]
    if not words:
        return False
    hay = " ".join(r["text"].lower() for r in rows)
    hits = sum(1 for w in words if w in hay)
    return hits / len(words) >= 0.6


def _topic_view(rows, label: str, notes: list, llm) -> str | None:
    if llm is None:
        return None
    numbered = "\n".join(f"{i + 1}|{_hm(r['created_at'])}|{r['sender_name']}|{_clean(r['text'], 160)}"
                         for i, r in enumerate(rows))
    system = ("Group these numbered chat messages into topics. Reply with ONLY JSON: "
              '{"topics":[{"title":"max 6 words, use only words that appear in the '
              'messages","ids":[1,2]}]}. Every message number appears in at most one '
              "topic. Do not summarise, do not add facts.")
    try:
        raw = llm(system, numbered[:14000])
        m = re.search(r"\{[\s\S]*\}", raw or "")
        topics = json.loads(m.group(0)).get("topics") if m else None
    except Exception:
        logger.debug("group_digest: topic grouping failed", exc_info=True)
        return None
    if not isinstance(topics, list) or not topics:
        return None
    used, blocks = set(), []
    for tp in topics[:12]:
        try:
            ids = [i for i in tp.get("ids", []) if isinstance(i, int) and 1 <= i <= len(rows) and i not in used]
        except Exception:
            continue
        if not ids:
            continue
        used.update(ids)
        sub = [rows[i - 1] for i in sorted(ids)]
        title = str(tp.get("title") or "").strip()
        if not _title_ok(title, sub):
            title = '"' + " ".join(_clean(sub[0]["text"], 60).split()[:6]) + '"'
        shown = [sub[0]] + ([sub[-1]] if len(sub) > 1 else [])
        blocks.append(f"*{title}* ({len(sub)} msgs, {_hm(sub[0]['created_at'])}-{_hm(sub[-1]['created_at'])}, "
                      f"{_speakers(sub)})\n" + "\n".join(_line(r, 140, False) for r in shown))
    rest = [rows[i] for i in range(len(rows)) if (i + 1) not in used]
    if rest:
        blocks.append(f"*Other* ({len(rest)} msgs, {_speakers(rest)})\n"
                      + "\n".join(_line(r, 100, False) for r in rest[:3]))
    if not blocks:
        return None
    head = (f"*Group chat, {label}*: {len(rows)} messages, "
            f"{_hm(rows[0]['created_at'])} to {_hm(rows[-1]['created_at'])}\n")
    foot = "\n".join(notes + ["Topic names are grouping only; the quoted lines are exact messages. "
                              "Ask for a person or a shorter time range to see more."])
    out = head + "\n" + "\n\n".join(blocks) + "\n\n" + foot
    return out if len(out) <= MAX_REPLY else out[:MAX_REPLY - 3] + "..."


# ---------- public -------------------------------------------------------
def is_group_summary_question(text: str) -> bool:
    t = (text or "").strip()
    return bool(t and len(t) < 220 and _GROUPWORD.search(t) and _ASK.search(t))


def build(text: str, group_ids, llm=None, now: datetime | None = None) -> str:
    """The rigid answer for a group-summary question."""
    import group_memory
    gids = [g for g in (group_memory._digits(x) for x in (group_ids or [])) if g]
    if not gids:
        return "I'm not recording any group chat yet."
    start, end, label = _window(text, now)
    people = _people_in(text)
    clients = _clients_in(text)
    where = f"group_id IN ({','.join('?' * len(gids))}) AND created_at>=? AND created_at<=?"
    params = tuple(gids) + (_iso(start), _iso(end))
    rows = group_memory._select(where, params, 900)
    rows.sort(key=lambda r: r["created_at"])   # imported/late messages can have older times than their id order
    notes = []
    first_at = group_memory.first_seen(gids)
    if first_at and first_at > _iso(start):
        notes.append(f"Note: I only started recording this group on {_day(first_at)} at {_hm(first_at)}; "
                     "anything said before that isn't here.")
    if people:
        rows = [r for r in rows if any(p in r["sender_name"].lower() for p in people)]
        label += f", from {', '.join(p.title() for p in people)}"
    if clients:
        keys = [_squash(c) for c in clients]
        rows = [r for r in rows if any(k and k in _squash(r["text"]) for k in keys)]
        label += ", messages naming a client"
    if not rows:
        return "\n".join([f"No messages found in the group chat for {label}."] + notes)
    out = None
    if len(rows) <= VERBATIM_MAX_MSGS:
        out = _verbatim(rows, label, notes)
    if out is None:
        out = _topic_view(rows, label, notes, llm)
    if out is None:   # model unavailable / bad: the latest messages, honestly labelled
        tail = rows[-VERBATIM_MAX_MSGS:]
        out = _verbatim(tail, label + f" (latest {len(tail)} of {len(rows)})", notes) or \
            "Too many messages to show; ask for a shorter time range."
    return out


def try_answer(text: str, group_ids, llm=None) -> str | None:
    if not is_group_summary_question(text):
        return None
    try:
        return build(text, group_ids, llm)
    except Exception:
        logger.exception("group_digest: failed, falling back to the model")
        return None


# ---------- safety net for the free-form path ---------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", str(s).lower())).strip()


def quotes_ok(reply: str, log: str) -> bool:
    """True if every quoted passage (>=12 chars) in the reply exists in the log."""
    hay = _norm(log)
    for q in re.findall(r'["“]([^"”]{12,})["”]', reply or ""):
        if _norm(q) not in hay:
            return False
    return True
