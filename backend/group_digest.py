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

import difflib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
MAX_REPLY = 3800          # WhatsApp is happy to ~4000
VERBATIM_MAX_MSGS = 45    # above this (or too long) -> topic view

_ASK = re.compile(
    r"\b(?:exact messages|full chat|discuss\w*|summari[sz]e|summary|recap|what (?:was|were|did|have|has|is) .{0,30}"
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


# ---------- checked summary ---------------------------------------------
_EXACT = re.compile(r"\b(?:exact|verbatim|word for word|full chat|every message|all (?:the )?messages|"
                    r"show (?:me )?(?:the )?(?:chat|messages)|raw)\b", re.I)
_HEDGE = re.compile(r"\b(?:probably|likely|seems?|maybe|might|sounds? like|possibly|apparently|"
                    r"presumably|perhaps|i think|appears? to)\b", re.I)
_CAPS_OK = {"the", "a", "an", "in", "on", "at", "and", "he", "she", "they", "it", "i", "monday", "tuesday",
            "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march",
            "april", "may", "june", "july", "august", "september", "october", "november", "december",
            "everyone", "group", "team", "link", "drive", "meet", "google", "whatsapp", "also", "then",
            "later", "after", "before", "one", "two", "three", "his", "her", "their", "tomorrow", "today"}

_SUM_SYSTEM = (
    "You summarise a team WhatsApp group chat for the boss. The messages are numbered "
    "as: number|time|speaker|text. Return ONLY JSON: "
    '{"points":[{"text":"one short plain sentence","ids":[message numbers that prove it]}]}. '
    "Rules: 4 to 9 points in time order, grouped by real topic. Every fact in a point must be "
    "stated in the messages you cite. Never guess a purpose, mood or meaning (no 'probably', "
    "'likely', 'seems'). A message that is only a link: say who shared a link and its site, "
    "nothing more. Do not put quotes around anything. Name only people who sent the cited "
    "messages or are named inside them. A message that tags or calls out a named person with a "
    "request (shown as @Name) is IMPORTANT: always make it its own point: who asked whom to do what. "
    "Skip noise like '.' or 'ok'. Hinglish: give the meaning "
    "faithfully in English. Cover EVERY substantive message: plans, requests, assignments (which person "
    "got which role), decisions, links shared, collaborations and follow-ups. Skip only pure noise. "
    "If nothing real was discussed, return fewer points.")


_FUNC = set("""shared said says asked announced posted sent wrote mentioned confirmed picked chose chosen
planned told wants wanted need needs needed bring brought requested suggested replied agreed joined about
around again after before during while where which their there these those would could should other others
everyone someone message messages chat group team character characters list lists version versions first
second third final tomorrow today tonight morning evening afternoon event tagging tagged people member
members file files video photo image link links site started asking saying people being have has had will
been were with from into over also then later each both more most some many another among between group's
selected reacted responded liked loved shared""".split())


def _words_ok(text: str, hay: str) -> bool:
    """Every content word of the point must come from the cited messages (5-letter stem
    match) or be an everyday connective/verb -- stops a made-up topic slipping through."""
    for w in re.findall(r"[A-Za-z][A-Za-z']+", text):
        wl = w.lower().strip("'")
        if len(wl) < 5 or wl in _FUNC or wl in _CAPS_OK:
            continue
        stem = wl[:5]
        if stem not in hay and _squash(wl) not in _squash(hay):
            return False
    return True


def _point_ok(text: str, ids: list, rows) -> bool:
    if not text or len(text) > 300 or _HEDGE.search(text):
        return False
    cited = [rows[i - 1] for i in ids]
    hay = " ".join(f"{r['sender_name']} {r['text']}" for r in cited).lower()
    hay_sq = _squash(hay)
    for w in re.findall(r"[A-Za-z][A-Za-z0-9']+", text):
        if w[0].isupper() and w.lower() not in _CAPS_OK and len(w) > 2:
            if w.lower() not in hay and _squash(w) not in hay_sq:
                if not difflib.get_close_matches(w.lower(), re.findall(r"[a-z0-9']+", hay), n=1, cutoff=0.75):
                    return False
    for n in re.findall(r"\d+", text):
        if n not in hay:
            return False
    return quotes_ok(text, hay)


LAST: dict = {}   # last summary attempt (raw model points + verdicts), for the debug route


def _verify(cand, use, llm) -> dict:
    """{n: bool} from an independent fact-check call, or {} if it can't be run."""
    if not cand or llm is None:
        return {}
    blocks = []
    for n, (ids, text) in enumerate(cand, 1):
        msgs = "\n".join(f"  [{_hm(use[i - 1]['created_at'])}] {use[i - 1]['sender_name']}: "
                         f"{_clean(use[i - 1]['text'], 400)}" for i in ids)
        blocks.append(f"CLAIM {n}: {text}\nMESSAGES:\n{msgs}")
    system = ("You are a strict fact checker. For each CLAIM decide if the MESSAGES under it "
              "directly support EVERY detail of the claim (who, what, when, numbers). Ignore spelling differences in names. A claim "
              "that adds a purpose, motive, feeling or detail not in the messages is NOT supported. "
              'Reply ONLY with JSON: {"results":[{"n":1,"supported":true}]}.')
    try:
        raw = llm(system, "\n\n".join(blocks)[:20000])
        m = re.search(r"\{[\s\S]*\}", raw or "")
        res = json.loads(m.group(0)).get("results") if m else None
        out = {int(r["n"]): bool(r.get("supported")) for r in res or []}
        LAST["verifier"] = out
        return out if len(out) == len(cand) else {}
    except Exception:
        logger.debug("group_digest: verifier failed", exc_info=True)
        return {}


def _summary(rows, label: str, notes: list, llm) -> str | None:
    if llm is None or not rows:
        return None
    use = rows[-300:]
    numbered = "\n".join(f"{i + 1}|{_hm(r['created_at'])}|{r['sender_name']}|{_clean(r['text'], 220)}"
                         for i, r in enumerate(use))
    try:
        raw = llm(_SUM_SYSTEM, numbered[:24000])
        LAST.clear()
        LAST["raw"] = (raw or "")[:4000]
        LAST["verdicts"] = []
        m = re.search(r"\{[\s\S]*\}", raw or "")
        points = json.loads(m.group(0)).get("points") if m else None
    except Exception:
        logger.debug("group_digest: summary call failed", exc_info=True)
        return None
    if not isinstance(points, list) or not points:
        return None
    cand, bad = [], 0
    for pt in points[:14]:
        try:
            ids = sorted({i for i in pt.get("ids", []) if isinstance(i, int) and 1 <= i <= len(use)})[:12]
            text = re.sub(r"\s+", " ", str(pt.get("text") or "")).strip()
        except Exception:
            bad += 1
            continue
        ok = bool(ids) and _point_ok(text, ids, use)
        LAST.setdefault("verdicts", []).append({"ok": ok, "text": text, "ids": ids})
        if ok:
            cand.append((ids, text))
        else:
            bad += 1
    # Second opinion: a strict fact-checker pass over each surviving point and
    # the exact messages it cites. If the checker is unavailable, fall back to
    # the conservative word-by-word test instead of trusting the writer.
    verdict = _verify(cand, use, llm)
    good = []
    for n, (ids, text) in enumerate(cand, 1):
        hay = " ".join(f"{use[i - 1]['sender_name']} {use[i - 1]['text']}" for i in ids).lower()
        ok = verdict.get(n) if verdict else _words_ok(text, hay)
        LAST["verdicts"].append({"checked": ok, "text": text})
        if ok:
            good.append((ids[0], text))
        else:
            bad += 1
    if len(good) < 1 or bad > len(good):
        logger.warning("group_digest: summary rejected (%d ok, %d bad)", len(good), bad)
        return None
    good.sort()
    accepted_ids = set()
    for n, (ids, text) in enumerate(cand, 1):
        hay = " ".join(f"{use[i - 1]['sender_name']} {use[i - 1]['text']}" for i in ids).lower()
        if (verdict.get(n) if verdict else _words_ok(text, hay)):
            accepted_ids.update(ids)
    # Nothing may go missing: every substantive message not covered by an accepted point
    # is listed word for word under the summary.
    rest = [r for i, r in enumerate(use, 1) if i not in accepted_ids and len(_clean(r["text"], 400)) >= 6]
    head = (f"*Group summary, {label}*: {len(rows)} messages, "
            f"{_hm(rows[0]['created_at'])} to {_hm(rows[-1]['created_at'])}\n")
    body = "\n".join(f"- {t} ({_hm(use[i - 1]['created_at'])})" for i, t in good)
    if rest:
        shown = rest[-14:]
        body += ("\n\n*Also in the chat (exact words):*\n" + "\n".join(_line(r, 150, False) for r in shown)
                 + (f"\n(+{len(rest) - len(shown)} earlier ones; ask for the exact messages)" if len(rest) > len(shown) else ""))
    foot = "\n".join(notes + ["The points were checked against the real messages; the lines under "
                              "\"Also in the chat\" are exactly what was said."])
    out = f"{head}\n{body}\n\n{foot}"
    return out if len(out) <= MAX_REPLY else out[:MAX_REPLY - 3] + "..."


# ---------- public -------------------------------------------------------
def is_group_summary_question(text: str) -> bool:
    t = (text or "").strip()
    return bool(t and len(t) < 220 and _GROUPWORD.search(t) and _ASK.search(t))


def build(text: str, group_ids, llm=None, now: datetime | None = None, llm_smart=None) -> str:
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
    if not _EXACT.search(text or ""):
        out = _summary(rows, label, notes, llm_smart or llm)
    if out is None and len(rows) <= VERBATIM_MAX_MSGS:
        out = _verbatim(rows, label, notes)
    if out is None:
        out = _topic_view(rows, label, notes, llm)
    if out is None:   # model unavailable / bad: the latest messages, honestly labelled
        tail = rows[-VERBATIM_MAX_MSGS:]
        out = _verbatim(tail, label + f" (latest {len(tail)} of {len(rows)})", notes) or \
            "Too many messages to show; ask for a shorter time range."
    return out


def try_answer(text: str, group_ids, llm=None, llm_smart=None) -> str | None:
    if not is_group_summary_question(text):
        return None
    try:
        return build(text, group_ids, llm, llm_smart=llm_smart)
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
