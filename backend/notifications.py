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
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TWILIO_SID    = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM   = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
FOUNDER_WA    = os.getenv("FOUNDER_WHATSAPP", "")


def _is_configured() -> bool:
    return bool(TWILIO_SID and TWILIO_TOKEN and FOUNDER_WA)


def send_whatsapp(message: str) -> bool:
    """
    Send a WhatsApp message to the founder via Twilio.
    Returns True on success, False on failure or if not configured.
    """
    if not _is_configured():
        logger.info("WhatsApp notifications not configured — skipping.")
        return False

    try:
        # Use requests directly to avoid requiring twilio SDK
        import requests
        from requests.auth import HTTPBasicAuth

        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
        payload = {
            "From": TWILIO_FROM,
            "To":   FOUNDER_WA,
            "Body": message,
        }
        r = requests.post(
            url,
            data=payload,
            auth=HTTPBasicAuth(TWILIO_SID, TWILIO_TOKEN),
            timeout=10,
        )
        if r.status_code in (200, 201):
            logger.info(f"WhatsApp sent: {message[:60]}...")
            return True
        else:
            logger.error(f"WhatsApp failed: {r.status_code} — {r.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"WhatsApp notification error: {e}")
        return False


# ── Notification helpers ──────────────────────────────────────────────────────

def notify_task_submitted(task_title: str, assignee: str, client_name: str) -> bool:
    """Sent when a team member submits a task for review."""
    msg = (
        f"⏳ *Review Request*\n"
        f"👤 *{assignee}* has submitted a task for your approval:\n\n"
        f"📋 *{task_title}*\n"
        f"📁 Client: {client_name}\n\n"
        f"Open the Project Board to approve or request changes."
    )
    return send_whatsapp(msg)


def notify_task_approved(task_title: str, assignee: str, client_name: str) -> bool:
    """Sent when a task is approved."""
    msg = (
        f"✅ *Task Approved*\n"
        f"📋 *{task_title}*\n"
        f"👤 Assigned to: {assignee}\n"
        f"📁 Client: {client_name}\n\n"
        f"Great work! The task has been marked as approved."
    )
    return send_whatsapp(msg)


def notify_task_changes_requested(task_title: str, assignee: str, client_name: str, note: str = "") -> bool:
    """Sent when changes are requested on a task."""
    msg = (
        f"↩️ *Changes Requested*\n"
        f"📋 *{task_title}*\n"
        f"👤 Assigned to: {assignee}\n"
        f"📁 Client: {client_name}\n"
    )
    if note:
        msg += f"\n📝 Note: {note}"
    return send_whatsapp(msg)


def notify_task_status_changed(task_title: str, assignee: str, client_name: str,
                                old_status: str, new_status: str) -> bool:
    """
    Generic status change notifier — fires for any status update via Kanban drag.
    Only notifies for meaningful transitions.
    """
    STATUS_EMOJI = {
        "not_started":    "📋 Not Started",
        "unlocked":       "🔓 Unlocked",
        "in_progress":    "🔄 In Progress",
        "pending_review": "⏳ In Review",
        "submitted":      "⏳ Submitted",
        "approved":       "✅ Approved",
        "rejected":       "↩️ Changes Requested",
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

    lines = [f"🌅 *Daily Digest — Overdue Tasks*\n"]
    for t in overdue_tasks[:10]:  # cap at 10
        lines.append(f"🔴 *{t['title']}* ({t.get('client', '—')})\n"
                     f"   👤 {t.get('assignee', '—')} · {t.get('days_overdue', 0)}d overdue\n")

    lines.append(f"\nTotal: {len(overdue_tasks)} overdue task(s). Check the Project Board.")
    return send_whatsapp("\n".join(lines))
