"""
Outbound email over SMTP. Used to tell HR about a new leave request.

Configured entirely by env vars (Railway):
  SMTP_HOST   e.g. smtp.gmail.com            (Google Workspace)
  SMTP_PORT   587 (STARTTLS, default) or 465 (SSL)
  SMTP_USER   the mailbox that sends, e.g. lumina@mmga.agency
  SMTP_PASS   that mailbox's app password (Google: Account > Security > App passwords)
  SMTP_FROM_NAME  optional display name, default "Lumina"

Unconfigured -> every send is a logged no-op, never an error, so nothing
that calls this can fail because email isn't set up.

Employee addresses are <first name>@mmga.agency (e.g. abhinav@mmga.agency),
unless employees.json gives the person an explicit "email" field.
"""
from __future__ import annotations

import logging
import os
import smtplib
import threading
from email.message import EmailMessage
from email.utils import formataddr

logger = logging.getLogger(__name__)

EMAIL_DOMAIN = "mmga.agency"
last_error = ""   # most recent send failure (shown by /api/companion/email-status)
HR_USER_ID = "emp009"


def is_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"))


def _employee(user_id: str) -> dict:
    try:
        from utils import _load_employees
        for e in _load_employees().get("employees", []):
            if e.get("id") == user_id:
                return e
    except Exception:
        logger.exception("mailer: could not read employees.json")
    return {}


def email_for(user_id: str) -> str:
    e = _employee(user_id)
    if e.get("email"):
        return str(e["email"]).strip()
    first = str(e.get("name") or "").strip().split(" ")[0].lower()
    first = "".join(ch for ch in first if ch.isalnum())
    return f"{first}@{EMAIL_DOMAIN}" if first else ""


def name_for(user_id: str) -> str:
    return str(_employee(user_id).get("name") or user_id)


def _send_now(to: list[str], subject: str, body: str, reply_to: str = "",
              from_name: str = "") -> bool:
    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT", "587") or 587)
    user = os.getenv("SMTP_USER", "")
    pwd = os.getenv("SMTP_PASS", "")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name or os.getenv("SMTP_FROM_NAME", "Lumina"), user))
    msg["To"] = ", ".join(to)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as srv:
                srv.login(user, pwd)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as srv:
                srv.starttls()
                srv.login(user, pwd)
                srv.send_message(msg)
        logger.info(f"mailer: sent '{subject}' to {to}")
        return True
    except Exception as e:
        global last_error
        last_error = f"{type(e).__name__}: {e}"[:400]
        logger.warning(f"mailer: send failed for '{subject}' to {to}: {e}")
        return False


def send(to, subject: str, body: str, reply_to: str = "", from_name: str = "",
         wait: bool = False) -> bool:
    """Send an email. Returns False (and logs) when SMTP isn't configured.
    By default sends on a background thread so a slow mail server never
    delays the HTTP request; wait=True sends inline and returns the result."""
    to = [t for t in ([to] if isinstance(to, str) else list(to or [])) if t]
    if not to:
        return False
    if not is_configured():
        logger.info(f"mailer: SMTP not configured, skipping '{subject}' to {to}")
        return False
    if wait:
        return _send_now(to, subject, body, reply_to, from_name)
    threading.Thread(target=_send_now, args=(to, subject, body, reply_to, from_name),
                     daemon=True).start()
    return True


def notify_leave_request(row: dict) -> bool:
    """Email HR about a new pending leave request. Reply-To is the applicant,
    so HR can answer them straight from the mail."""
    try:
        uid = row.get("user_id", "")
        who = name_for(uid)
        applicant = email_for(uid)
        hr = email_for(HR_USER_ID)
        sd, ed = row.get("start_date", ""), row.get("end_date", "")
        when = sd if sd == ed else f"{sd} to {ed}"
        kind = "Half day" if row.get("leave_type") == "half" else "Full day"
        reason = (row.get("reason") or "").strip() or "(none given)"
        base = (os.getenv("PUBLIC_BASE_URL") or "https://lumina.mmga.agency").rstrip("/")
        body = (
            f"{who} has applied for leave.\n\n"
            f"Dates:  {when}\n"
            f"Type:   {kind}\n"
            f"Reason: {reason}\n\n"
            f"Approve or reject it here: {base}/leave.html\n\n"
            f"Reply to this email to reach {who} directly."
        )
        return send(hr, f"Leave request: {who} ({when})", body,
                    reply_to=applicant, from_name=f"{who} via Lumina")
    except Exception:
        logger.exception("mailer: notify_leave_request failed")
        return False
