"""
whatsapp_standup.py -- WhatsApp Daily Standup
==============================================
Lets employees run their daily standup from WhatsApp: see today's tasks,
mark them done/blocked, add new ones. Deterministic command parsing only --
no AI-guessed edits (see docs/superpowers/specs/2026-08-25-whatsapp-standup-design.md
"Non-goals" and CLAUDE.md gotchas #45/#51/#69/#73 for why).
"""
import re
import logging
from typing import Optional

from utils import _load_employees

logger = logging.getLogger(__name__)

_DONE_RE = re.compile(r"^(all|\d+(?:\s*,\s*\d+)*)\s+done$", re.IGNORECASE)
_ADD_RE = re.compile(r"^add\s+(.+)$", re.IGNORECASE | re.DOTALL)
_BLOCKED_RE = re.compile(r"^blocked\s*:?\s*(\d+)\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _normalize_phone(raw: str) -> str:
    """Digits-only, last 10 digits -- handles a stored number with/without
    '+', country code, or a 'whatsapp:' prefix from either side."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def find_employee_by_whatsapp(sender: str) -> Optional[dict]:
    """Match an inbound WhatsApp sender number against employees.json's
    'whatsapp' field. Read live every call (utils._load_employees()) --
    never cache/hardcode this, see CLAUDE.md gotcha #63."""
    sender_norm = _normalize_phone(sender)
    if not sender_norm:
        return None
    try:
        data = _load_employees()
    except Exception:
        logger.exception("Failed to load employees.json for WhatsApp identification")
        return None
    for emp in data.get("employees", []):
        wa = emp.get("whatsapp", "")
        if wa and _normalize_phone(wa) == sender_norm:
            return emp
    return None


def parse_standup_command(text: str) -> dict:
    """Deterministic command grammar -- see module docstring for why this
    is never AI-classified. Checked in order; first match wins."""
    stripped = (text or "").strip()

    m = _DONE_RE.match(stripped)
    if m:
        raw = m.group(1).strip()
        if raw.lower() == "all":
            return {"type": "done", "numbers": "all"}
        numbers = [int(n.strip()) for n in raw.split(",") if n.strip()]
        return {"type": "done", "numbers": numbers}

    m = _BLOCKED_RE.match(stripped)
    if m:
        return {"type": "blocked", "number": int(m.group(1)), "reason": m.group(2).strip()}

    m = _ADD_RE.match(stripped)
    if m:
        return {"type": "add", "title": m.group(1).strip()}

    return {"type": "none"}
