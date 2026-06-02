"""
notifications.py — WhatsApp Notifications via Twilio
=====================================================
Sends WhatsApp messages to the founder when task statuses change.

Setup:
  1. Sign up at https://www.twilio.com (free)
  2. Go to Messaging → Try it out → Send a WhatsApp message
  3. Join the sandbox by WhatsApp: send "join <code>" to +1 415 523 8886
  4. Copy your Account SID and Auth Token from the Twilio Console
  5. Set these in your .env file:
       TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
       TWILIO_AUTH_TOKEN=your_auth_token
       TWILIO_WHATSAPP_FROM=whatsapp:+14155238886   # Twilio sandbox number
       FOUNDER_WHATSAPP=whatsapp:+91XXXXXXXXXX       # Your WhatsApp (with country code)

If Twilio is not configured, all notifications are silently skipped (no crash).
"""

import os
import time
import logging
import requests
from requests.auth import HTTPBasicAuth
from typing import Optional

logger = logging.getLogger(__name__)

def _get_twilio_creds():
    return {
        "sid": os.getenv("TWILIO_ACCOUNT_SID", ""),
        "token": os.getenv("TWILIO_AUTH_TOKEN", ""),
        "from": os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
        "to": os.getenv("FOUNDER_WHATSAPP", ""),
    }

def _is_configured(creds) -> bool:
    return bool(creds["sid"] and creds["token"] and creds["to"])


def send_whatsapp(message: str) -> bool:
    """
    Send a WhatsApp message to the founder via Twilio.
    Retries up to 3 times with exponential backoff.
    Returns True on success, False on failure or if not configured.
    """
    creds = _get_twilio_creds()
    if not _is_configured(creds):
        logger.info("WhatsApp notifications not configured — skipping.")
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{creds['sid']}/Messages.json"
    payload = {"From": creds["from"], "To": creds["to"], "Body": message}
    auth = HTTPBasicAuth(creds["sid"], creds["token"])

    for attempt in range(1, 4):  # 3 attempts: 0s, 2s, 4s
        try:
            r = requests.post(url, data=payload, auth=auth, timeout=10)
            if r.status_code in (200, 201):
                logger.info(f"WhatsApp sent (attempt {attempt}): {message[:60]}...")
                return True
            elif r.status_code in (429, 503):  # rate limit or service unavailable
                wait = 2 ** (attempt - 1)
                logger.warning(f"WhatsApp rate-limited ({r.status_code}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"WhatsApp failed: {r.status_code} — {r.text[:200]}")
                return False
        except requests.exceptions.Timeout:
            logger.warning(f"WhatsApp timeout on attempt {attempt}")
            time.sleep(2 ** (attempt - 1))
        except Exception:
            logger.exception("WhatsApp notification error")
            return False

    logger.error("WhatsApp failed after 3 attempts.")
    return False


# ── Notification helpers ──────────────────────────────────────────────────────

def notify_task_submitted(task_title: str, assignee: str, client_name: str) -> bool:
    """Sent when a team member submits a task for review."""
    msg = (
        f"⏳ *Review Request*\n"
        f" *{assignee}* has submitted a task for your approval:\n\n"
        f" *{task_title}*\n"
        f" Client: {client_name}\n\n"
        f"Open the Project Board to approve or request changes."
    )
    return send_whatsapp(msg)


def notify_task_approved(task_title: str, assignee: str, client_name: str) -> bool:
    """Sent when a task is approved."""
    msg = (
        f" *Task Approved*\n"
        f" *{task_title}*\n"
        f" Assigned to: {assignee}\n"
        f" Client: {client_name}\n\n"
        f"Great work! The task has been marked as approved."
    )
    return send_whatsapp(msg)


def notify_task_changes_requested(task_title: str, assignee: str, client_name: str, note: str = "") -> bool:
    """Sent when changes are requested on a task."""
    msg = (
        f"↩ *Changes Requested*\n"
        f" *{task_title}*\n"
        f" Assigned to: {assignee}\n"
        f" Client: {client_name}\n"
    )
    if note:
        msg += f"\n Note: {note}"
    return send_whatsapp(msg)


def notify_task_status_changed(task_title: str, assignee: str, client_name: str,
                                old_status: str, new_status: str) -> bool:
    """
    Generic status change notifier — fires for any status update via Kanban drag.
    Only notifies for meaningful transitions.
    """
    STATUS_EMOJI = {
        "not_started":    " Not Started",
        "unlocked":       " Unlocked",
        "in_progress":    " In Progress",
        "pending_review": "⏳ In Review",
        "submitted":      "⏳ Submitted",
        "approved":       " Approved",
        "rejected":       "↩ Changes Requested",
    }

    # Only notify for important transitions (not every tiny change)
    NOTIFY_ON = {"pending_review", "submitted", "approved", "rejected"}
    if new_status not in NOTIFY_ON:
        return False

    if new_status in ("pending_review", "submitted"):
        return notify_task_submitted(task_title, assignee, client_name)
    elif new_status == "approved":
        return notify_task_approved(task_title, assignee, client_name)
    elif new_status == "rejected":
        return notify_task_changes_requested(task_title, assignee, client_name)

    return False


def notify_daily_digest(overdue_tasks: list) -> bool:
    """
    Send a morning digest of all overdue tasks.
    overdue_tasks: list of { title, assignee, client, days_overdue }
    """
    if not overdue_tasks:
        return False

    lines = [f" *Daily Digest — Overdue Tasks*\n"]
    for t in overdue_tasks[:10]:  # cap at 10
        lines.append(f" *{t['title']}* ({t.get('client', '—')})\n"
                     f"    {t.get('assignee', '—')} · {t.get('days_overdue', 0)}d overdue\n")

    lines.append(f"\nTotal: {len(overdue_tasks)} overdue task(s). Check the Project Board.")
    return send_whatsapp("\n".join(lines))
