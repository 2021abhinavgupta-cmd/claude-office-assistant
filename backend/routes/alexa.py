"""Alexa skill endpoint: talk to the Lumina assistant by voice.

POST /api/alexa                     -- Alexa's webhook (signature + skill id + timestamp checked)
GET  /api/alexa/interaction-model   -- the JSON to paste into the Alexa console (JSON Editor)

What you say goes to the SAME whatsapp_agent.handle_message() the WhatsApp bot
uses (same tools, data, memory, fast paths), as the linked employee. Alexa
only waits ~8 s, so a reply that isn't ready in WAIT_SECONDS is finished in
the background: Alexa says "give me a moment", the answer waits for "what's
the answer", and it is also sent to the person's WhatsApp. Anything too long
to read out is shortened and the full text goes to WhatsApp too.

Alexa gives no transcript of what was said, only intents + slots. A catch-all
AMAZON.SearchQuery slot needs a leading "carrier" word, and Alexa drops that
word from the slot value. So every carrier ("what", "show me", "add", "mere"
...) is its own intent and we put the word back in front of the slot text.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

import alexa_link
import alexa_verify
from extensions import limiter

logger = logging.getLogger(__name__)

alexa_bp = Blueprint("alexa", __name__)

DEFAULT_SKILL_ID = "amzn1.ask.skill.412f64ef-efbb-4137-ae60-d08b9baa9f8e"
INVOCATION = "lumina"
WAIT_SECONDS = 7.0        # Alexa gives up at ~8 s; leave room for the round trip
MAX_SKEW_SECONDS = 150    # Amazon's rule for the request timestamp
SPEECH_MAX = 650          # characters read aloud before "the rest is on WhatsApp"
LIST_MAX = 5              # list lines read aloud before "and N more"

LAST: dict = {}           # last request / error, for /api/companion/alexa-status

# Carrier phrases. Each becomes intent Q_<slug> with the sample "<phrase> {q}";
# the phrase is put back in front of what Alexa heard.
CARRIERS = [
    "what", "what's", "whats", "who", "who's", "how", "how's", "when", "where", "why", "which",
    "is", "are", "am i", "do", "does", "did", "can", "can you", "could", "should", "will", "would",
    "have", "has", "tell me", "show me", "give me", "get me", "list", "read", "check", "find",
    "search", "look up", "add", "put", "create", "make", "mark", "set", "move", "delay", "postpone",
    "push", "reschedule", "assign", "hand over", "give", "remind", "send", "post", "message",
    "call", "approve", "reject", "cancel", "delete", "remove", "draft", "write", "summarise",
    "summarize", "explain", "help me", "i need", "i want", "i have", "i finished", "i've",
    "i'm", "i am", "we need", "please", "let's", "any", "anything", "my", "our", "the",
    "update", "plan", "compare", "suggest", "recommend", "calculate", "translate", "convert",
    "complete", "finish", "done with", "start", "open", "how many", "how much",
    # Hinglish (en-IN recognises these when spoken)
    "mere", "meri", "mera", "mujhe", "kya", "kaun", "kab", "kitne", "kitna", "kaise", "kahan",
    "aaj", "kal", "batao", "bata", "abhi", "humara", "hamara", "sab",
]
ANSWER_SAMPLES = ["for the answer", "the answer", "give me the answer", "what's the answer",
                  "is it ready", "is my answer ready", "answer please", "for my answer",
                  "what did you find", "jawab", "jawab batao"]
CODE_SAMPLES = ["code {code}", "my code is {code}", "the code is {code}", "link code {code}",
                "link {code}", "code is {code}"]


def _slug(phrase: str) -> str:
    return "Q_" + re.sub(r"[^a-z]+", "_", phrase.lower()).strip("_")


_INTENT_PREFIX = {_slug(c): c for c in CARRIERS}


def interaction_model() -> dict:
    intents = [{"name": "LinkIntent", "slots": [{"name": "code", "type": "AMAZON.NUMBER"}],
                "samples": CODE_SAMPLES},
               {"name": "AnswerIntent", "slots": [], "samples": ANSWER_SAMPLES}]
    for c in CARRIERS:
        intents.append({"name": _slug(c), "slots": [{"name": "q", "type": "AMAZON.SearchQuery"}],
                        "samples": [f"{c} {{q}}"]})
    for b in ("AMAZON.CancelIntent", "AMAZON.HelpIntent", "AMAZON.StopIntent",
              "AMAZON.NavigateHomeIntent", "AMAZON.FallbackIntent", "AMAZON.YesIntent",
              "AMAZON.NoIntent"):
        intents.append({"name": b, "samples": []})
    return {"interactionModel": {"languageModel": {
        "invocationName": INVOCATION, "intents": intents, "types": []}}}


@alexa_bp.route("/api/alexa/interaction-model", methods=["GET"])
def alexa_interaction_model():
    return jsonify(interaction_model())


# ---------- responses ----------------------------------------------------
def _resp(text: str, end: bool = False, reprompt: str | None = None, card: str | None = None) -> dict:
    out = {"version": "1.0", "response": {
        "outputSpeech": {"type": "PlainText", "text": (text or "")[:7900]},
        "shouldEndSession": bool(end)}}
    if reprompt and not end:
        out["response"]["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    if card:
        out["response"]["card"] = {"type": "Simple", "title": "Lumina", "content": card[:7900]}
    return out


_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_URL = re.compile(r"https?://\S+|www\.\S+")
_LIST_LINE = re.compile(r"^\s*(?:\d{1,3}[.)]|[-•*])\s+")


def to_speech(text: str) -> tuple[str, bool]:
    """(speakable text, shortened?) -- strips WhatsApp formatting, reads lists
    as short sentences and caps the length."""
    t = str(text or "")
    t = t.replace("✅", " done: ").replace("❌", " not done: ").replace("❗", "")
    t = _URL.sub("a link", t)
    t = _EMOJI.sub("", t)
    t = re.sub(r"[*_~`#>]", "", t)
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    out, n_list, shortened = [], 0, False
    for l in lines:
        if _LIST_LINE.match(l):
            n_list += 1
            if n_list > LIST_MAX:
                continue
            l = _LIST_LINE.sub("", l)
        l = re.sub(r"\s+", " ", l).strip(" -:")
        if l and l[-1] not in ".!?:":
            l += "."
        out.append(l)
    if n_list > LIST_MAX:
        out.append(f"And {n_list - LIST_MAX} more.")
        shortened = True
    speech = re.sub(r"\s+", " ", " ".join(out)).strip()
    speech = re.sub(r"\s+([.,!?])", r"\1", speech).replace(":.", ".")
    if len(speech) > SPEECH_MAX:
        cut = speech[:SPEECH_MAX]
        dot = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        speech = cut[:dot + 1] if dot > 200 else cut.rsplit(" ", 1)[0] + "."
        shortened = True
    return speech or "Done.", shortened


# ---------- who is talking -----------------------------------------------
def _keys(body: dict) -> list:
    sysx = (body.get("context") or {}).get("System") or {}
    person = (sysx.get("person") or {}).get("personId")
    user = (sysx.get("user") or (body.get("session") or {}).get("user") or {}).get("userId")
    keys = []
    if person:
        keys.append("person:" + person)
    if user:
        keys.append("user:" + user)
    return keys


def _employee(emp_id: str | None) -> dict | None:
    if not emp_id:
        return None
    try:
        import utils
        for e in utils._load_employees().get("employees", []):
            if e.get("id") == emp_id:
                if str(e.get("status", "active")).strip().lower() in (
                        "inactive", "disabled", "left", "removed", "archived", "former"):
                    return None
                return e
    except Exception:
        logger.exception("alexa: employee lookup failed")
    return None


def _send_whatsapp(emp: dict, text: str) -> bool:
    try:
        import wa_outbox
        jid = wa_outbox.wa_jid(emp.get("whatsapp", ""))
        return bool(jid) and wa_outbox.enqueue(jid, text)
    except Exception:
        logger.exception("alexa: WhatsApp copy failed")
        return False


# ---------- asking the assistant -----------------------------------------
_ANSWER_Q = re.compile(r"^\s*(?:what'?s |what is |give me |for )?(?:the |my )?(?:answer|jawab)\b|"
                       r"^\s*is (?:it|my answer) ready|^\s*what did you find", re.I)
_NUMWORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
             "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def _digits_from(text: str) -> str:
    words = re.findall(r"[a-z]+|\d+", (text or "").lower())
    return "".join(_NUMWORDS.get(w, w if w.isdigit() else "") for w in words)


def _reply_text(r) -> str:
    if isinstance(r, dict):
        r = r.get("reply")
    return str(r or "").strip()


def _ask(emp: dict, text: str, deadline: float) -> dict:
    """Run the assistant; answer now if it's quick, else defer."""
    import whatsapp_agent
    phone = emp.get("whatsapp", "")
    if not re.sub(r"\D", "", phone or ""):
        return _resp("Your WhatsApp number isn't set up in Lumina, so I can't look anything up for you.",
                     end=True)
    state = {"done": False, "deferred": False, "reply": ""}
    lock = threading.Lock()

    def work():
        try:
            reply = _reply_text(whatsapp_agent.handle_message(phone, text, channel="voice"))
        except Exception:
            logger.exception("alexa: assistant failed")
            reply = "Sorry, something went wrong on my side. Try again in a minute."
        with lock:
            state["reply"], state["done"] = reply, True
            deferred = state["deferred"]
        if deferred:
            alexa_link.finish_pending(emp["id"], reply)
            _send_whatsapp(emp, f"*Your Alexa question:* {text}\n\n{reply}")

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(max(0.5, deadline - time.time()))
    with lock:
        if not state["done"]:
            alexa_link.start_pending(emp["id"], text)   # before the worker can finish it
            state["deferred"] = True
            reply = None
        else:
            reply = state["reply"]
    if reply is None:
        LAST["deferred"] = LAST.get("deferred", 0) + 1
        return _resp("That needs a bit of thinking. Give me a moment, then say: what's the answer. "
                     "I'll also send it to your WhatsApp.",
                     reprompt="Say: what's the answer, when you're ready.")
    speech, short = to_speech(reply)
    if short:
        _send_whatsapp(emp, f"*Your Alexa question:* {text}\n\n{reply}")
        speech += " The full answer is on your WhatsApp."
    return _resp(speech, reprompt="Anything else?", card=reply)


def _collect(emp: dict) -> dict:
    p = alexa_link.get_pending(emp["id"])
    if not p:
        return _resp("Nothing is waiting for you right now. What do you need?",
                     reprompt="What do you need?")
    if p["status"] != "done":
        return _resp("Still working on it. Ask again in a few seconds.",
                     reprompt="Say: what's the answer.")
    alexa_link.clear_pending(emp["id"])
    speech, short = to_speech(p["answer"])
    if short:
        speech += " The full answer is on your WhatsApp."
    return _resp(speech, reprompt="Anything else?", card=p["answer"])


_HOW_TO_LINK = ("I don't know who you are yet. On WhatsApp, send: link my alexa, to the Lumina bot. "
                "It gives you a six digit code. Then say: code, and the six digits, one by one.")
_HELP = ("You can say things like: what's on my standup, add shoot edit to my standup, mark the reel "
         "done, what's happening with Mellow, what needs my approval, summarise the group, or ask me "
         "anything. For a long question I'll say give me a moment, then you say: what's the answer.")


def _do_link(keys: list, code: str) -> dict:
    if not keys:
        return _resp("I couldn't read your Alexa account, so I can't link it.", end=True)
    status, emp_id = alexa_link.link(keys[0], code)
    if status == "locked":
        return _resp("Too many wrong codes. Please wait an hour and get a new code on WhatsApp.", end=True)
    if status != "ok":
        return _resp("That code didn't work. Codes last ten minutes. Send link my alexa on WhatsApp "
                     "for a new one, then say: code, and the six digits.",
                     reprompt="Say: code, and the six digits.")
    emp = _employee(emp_id) or {}
    return _resp(f"Done, you're linked as {emp.get('name', 'you')}. What do you need?",
                 reprompt="What do you need?")


# ---------- the webhook ----------------------------------------------------
def _skill_id() -> str:
    return os.getenv("ALEXA_SKILL_ID", "").strip() or DEFAULT_SKILL_ID


@alexa_bp.route("/api/alexa", methods=["POST"])
@limiter.limit("60 per minute")
def alexa_webhook():
    started = time.time()
    raw = request.get_data(cache=True) or b""
    ok, why = alexa_verify.verify(request.headers, raw)
    if not ok:
        LAST.update(error=f"rejected: {why}", at=datetime.now(timezone.utc).isoformat())
        logger.warning("alexa: request rejected (%s)", why)
        return jsonify({"error": "bad request"}), 400
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return jsonify({"error": "bad json"}), 400
    app_id = (((body.get("session") or {}).get("application") or {}).get("applicationId")
              or (((body.get("context") or {}).get("System") or {}).get("application") or {}).get("applicationId"))
    if app_id != _skill_id():
        LAST.update(error=f"rejected: wrong skill id {app_id}", at=datetime.now(timezone.utc).isoformat())
        return jsonify({"error": "wrong skill"}), 400
    req = body.get("request") or {}
    try:
        ts = datetime.fromisoformat(str(req.get("timestamp", "")).replace("Z", "+00:00"))
        if abs((datetime.now(timezone.utc) - ts).total_seconds()) > MAX_SKEW_SECONDS:
            raise ValueError("stale")
    except Exception:
        LAST.update(error="rejected: timestamp too old or missing", at=datetime.now(timezone.utc).isoformat())
        return jsonify({"error": "stale request"}), 400

    rtype = req.get("type", "")
    intent = (req.get("intent") or {}).get("name", "")
    LAST.update(at=datetime.now(timezone.utc).isoformat(), type=rtype, intent=intent, error=None)
    if rtype == "SessionEndedRequest":
        if req.get("error"):
            LAST["error"] = f"session ended: {req.get('error')}"
        return jsonify({"version": "1.0", "response": {}})

    keys = _keys(body)
    emp = _employee(alexa_link.employee_for(keys))
    slots = (req.get("intent") or {}).get("slots") or {}

    if rtype == "IntentRequest" and intent == "LinkIntent":
        return jsonify(_do_link(keys, (slots.get("code") or {}).get("value", "")))
    if rtype == "IntentRequest" and intent in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
        return jsonify(_resp("Okay, bye.", end=True))
    if not emp:
        if rtype == "IntentRequest" and intent in _INTENT_PREFIX:
            heard = (slots.get("q") or {}).get("value", "")
            said = f"{_INTENT_PREFIX[intent]} {heard}"
            if re.search(r"\bcode\b", said, re.I) and len(_digits_from(heard)) >= 6:
                return jsonify(_do_link(keys, _digits_from(heard)[-6:]))
        return jsonify(_resp(_HOW_TO_LINK, reprompt="Say: code, and the six digits."))

    first = str(emp.get("name") or "").split(" ")[0] or "there"
    if rtype == "LaunchRequest":
        return jsonify(_resp(f"Hi {first}, what do you need?",
                             reprompt="You can say: what are my tasks, or ask me anything."))
    if rtype != "IntentRequest":
        return jsonify(_resp("Sorry, I didn't get that.", reprompt="What do you need?"))
    if intent in ("AMAZON.HelpIntent", "AMAZON.NavigateHomeIntent"):
        return jsonify(_resp(_HELP, reprompt="What do you need?"))
    if intent == "AMAZON.FallbackIntent":
        return jsonify(_resp("Sorry, I didn't catch that. Start with words like what, show me, add, or "
                             "tell me.", reprompt="What do you need?"))
    if intent == "AnswerIntent":
        return jsonify(_collect(emp))

    deadline = started + WAIT_SECONDS
    if intent == "AMAZON.YesIntent":
        return jsonify(_ask(emp, "yes", deadline))
    if intent == "AMAZON.NoIntent":
        return jsonify(_ask(emp, "no", deadline))
    if intent in _INTENT_PREFIX:
        heard = (slots.get("q") or {}).get("value", "").strip()
        text = f"{_INTENT_PREFIX[intent]} {heard}".strip()
        if _ANSWER_Q.search(text) and alexa_link.get_pending(emp["id"]):
            return jsonify(_collect(emp))
        out = _ask(emp, text, deadline)
        LAST["took_s"] = round(time.time() - started, 1)
        return jsonify(out)
    return jsonify(_resp("Sorry, I didn't get that.", reprompt="What do you need?"))
