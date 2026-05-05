"""
WhatsApp Bot — Employee tracking & auto-responses via Twilio/Interakt
Handles incoming WhatsApp messages and routes them to Claude
"""

import os
import logging
from dotenv import load_dotenv

# Load env FIRST so budget_tracker and model_router pick it up on import
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

from flask import Flask, request, abort
import anthropic
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from budget_tracker import check_budget_available, record_usage
from model_router import calculate_cost
from memory_store import get_memories, format_for_prompt

logger = logging.getLogger(__name__)

PROVIDER = os.getenv("WHATSAPP_PROVIDER", "twilio").lower()
client   = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
HAIKU    = os.getenv("HAIKU_MODEL", "claude-haiku-4-5")

# Keyword → task routing for WhatsApp
WA_TASK_KEYWORDS = {
    "code":         "coding",
    "html":         "html_design",
    "design":       "html_design",
    "slide":        "presentations",
    "presentation": "presentations",
    "caption":      "captions",
    "script":       "scripts",
}

WA_SYSTEM = (
    "You are a concise office assistant responding via WhatsApp. "
    "Keep replies short (max 3 sentences unless code is needed). "
    "Use simple formatting — no markdown headers, minimal bullet points."
)

app = Flask(__name__)


def detect_task(message: str) -> str:
    """Detect task type from message keywords. Default: general."""
    lower = message.lower()
    for keyword, task in WA_TASK_KEYWORDS.items():
        if keyword in lower:
            return task
    return "general"


def ask_claude(message: str, user_id: str = "whatsapp_user") -> dict:
    """Send a WhatsApp message to Claude with user memories injected."""
    budget = check_budget_available()
    if not budget["allowed"]:
        return {"reply": "⚠️ Monthly AI budget limit reached. Please contact admin.", "success": False}

    # Build system prompt with user's persistent memories
    memory_context = format_for_prompt(user_id)
    full_system = WA_SYSTEM + memory_context

    try:
        response = client.messages.create(
            model=HAIKU,
            max_tokens=512,
            system=full_system,
            messages=[{"role": "user", "content": message}]
        )
        text   = response.content[0].text
        cost   = calculate_cost("haiku", response.usage.input_tokens, response.usage.output_tokens)
        record_usage(
            task_type=detect_task(message),
            model_tier="haiku",
            model_name=HAIKU,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost=cost,
            user_id=user_id,
        )
        return {"reply": text, "success": True, "cost": cost}
    except Exception as e:
        logger.error(f"WhatsApp Claude error: {e}")
        return {"reply": "Sorry, I encountered an error. Please try again.", "success": False}


# ── Twilio Webhook ────────────────────────────────────────────────────────────
@app.route("/whatsapp/twilio", methods=["POST"])
def twilio_webhook():
    """Handles incoming Twilio WhatsApp messages with signature validation."""
    from twilio.twiml.messaging_response import MessagingResponse
    from twilio.request_validator import RequestValidator

    # Validate the request is genuinely from Twilio
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if auth_token:
        validator  = RequestValidator(auth_token)
        url        = request.url
        post_vars  = request.form.to_dict()
        signature  = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(url, post_vars, signature):
            logger.warning("Rejected request: invalid Twilio signature")
            abort(403)

    body    = request.form.get("Body", "").strip()
    from_   = request.form.get("From", "unknown")
    user_id = from_.replace("whatsapp:", "").replace("+", "")

    logger.info(f"WhatsApp [Twilio] from={from_}: {body[:80]}")

    result = ask_claude(body, user_id=user_id)
    twiml  = MessagingResponse()
    twiml.message(result["reply"])
    return str(twiml), 200, {"Content-Type": "text/xml"}


# ── Interakt Webhook ──────────────────────────────────────────────────────────
@app.route("/whatsapp/interakt", methods=["POST"])
def interakt_webhook():
    """Handles incoming Interakt WhatsApp messages."""
    import requests as req

    data  = request.get_json(silent=True) or {}

    # Interakt payload varies by version — try all known paths
    msg_obj = data.get("message", {})
    message = (
        msg_obj.get("message", {}).get("text")      # Interakt v1 nested
        or msg_obj.get("text")                       # Interakt v2 flat
        or data.get("text", "")                      # Top-level fallback
    )
    message = (message or "").strip()
    phone   = data.get("customer", {}).get("phone_number", "unknown")

    logger.info(f"WhatsApp [Interakt] from={phone}: {message[:80]}")

    result = ask_claude(message, user_id=phone)

    # Send reply via Interakt API
    interakt_key = os.getenv("INTERAKT_API_KEY", "")
    if interakt_key:
        req.post(
            "https://api.interakt.ai/v1/public/message/",
            headers={"Authorization": f"Basic {interakt_key}", "Content-Type": "application/json"},
            json={
                "countryCode": "+91",
                "phoneNumber": phone,
                "type": "Text",
                "data": {"message": result["reply"]}
            }
        )

    return {"status": "ok"}, 200


if __name__ == "__main__":
    port = int(os.getenv("WHATSAPP_PORT", 5001))
    logger.info(f"WhatsApp bot running on port {port} | Provider: {PROVIDER}")
    app.run(host="0.0.0.0", port=port, debug=False)
