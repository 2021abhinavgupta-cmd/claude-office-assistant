"""
Flask Backend — Claude Office Assistant API
Routes:
  GET  /api/health                        — Health check
  GET  /api/budget                        — Current month budget
  GET  /api/usage                         — Full usage dashboard
  POST /api/chat                          — Single-turn chat (legacy)
  POST /api/html/generate                 — HTML generator
  POST /api/presentation                  — Slide generator
  GET  /api/conversations                 — List user conversations
  POST /api/conversations                 — Create conversation
  GET  /api/conversations/<id>            — Get conversation + messages
  DEL  /api/conversations/<id>            — Delete conversation
  POST /api/conversations/<id>/chat       — Multi-turn chat in conversation
  PATCH /api/conversations/<id>/title     — Rename conversation
  GET  /api/employees                     — List employees
  POST /api/employees/checkin             — Check-in/out
"""

import os
import re
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from flask import Flask, request, jsonify, render_template_string, Response, stream_with_context
from flask_cors import CORS
from flask_compress import Compress
from dotenv import load_dotenv
import anthropic

# Local modules
from model_router import get_model_for_task, calculate_cost, get_all_routes
from budget_tracker import check_budget_available, record_usage, get_usage_summary, get_all_usage_logs
from system_prompt import MASTER_SYSTEM_PROMPT
import kb_retriever
import conversation_store
import memory_store
import file_processor
import project_store
import task_scheduler
import notion_store

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Add a persistent file handler to log to the Railway volume
from logging.handlers import RotatingFileHandler
LOG_DIR = Path(__file__).parent.parent / "logs"
os.makedirs(LOG_DIR, exist_ok=True)
file_handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=2)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logger.addHandler(file_handler)

# ── Flask App ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
CORS(app, resources={r"/api/*": {"origins": "*"}})
Compress(app)

# ── Start background scheduler ────────────────────────────────────────────────
_scheduler = task_scheduler.init_scheduler(app)

# ── Frontend Static Files ──────────────────────────────────────────────────────
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

@app.route("/")
@app.route("/index.html")
def serve_index():
    from flask import send_from_directory
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.route("/<path:filename>")
def serve_frontend(filename):
    from flask import send_from_directory
    return send_from_directory(FRONTEND_DIR, filename)

# ── Anthropic Client ──────────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── Paths ─────────────────────────────────────────────────────────────────────
CONFIG_DIR   = Path(__file__).parent.parent / "config"
EMPLOYEES_DB = CONFIG_DIR / "employees.json"

# ── System Prompts per Task ───────────────────────────────────────────────────
SYSTEM_PROMPTS = {
    "coding": (
        "When writing code:\n"
        "- Always explain what the code does before writing it\n"
        "- Add comments for non-obvious logic only\n"
        "- Suggest edge cases the user might not have considered\n"
        "- Point out security issues if you spot any"
    ),
    "html_design": (
        "TASK-SPECIFIC OUTPUT: One complete, self-contained HTML page.\n\n"
        "TECHNICAL:\n"
        "- Raw HTML from <!DOCTYPE html> through </html>; all CSS in <style>, all JS in <script>\n"
        "- External resources: Google Fonts links only\n"
        "- Semantic HTML5; mobile-first with ≥2 breakpoints; valid, lint-clean code\n\n"
        "STRUCTURE (unless the user specifies otherwise):\n"
        "- Nav, hero, substantive sections (features/value/social proof as fits), CTA, footer\n"
        "- CSS variables for colours/spacing; transitions/hover states; meaningful motion (e.g. IntersectionObserver reveals)\n\n"
        "Follow the global HTML/CSS rules above (distinctive fonts, no generic AI templates, never truncate CSS/JS)."
    ),
    "presentations": (
        "TASK-SPECIFIC OUTPUT: Slide decks as markdown matching the global presentation rules.\n\n"
        "Every slide, exactly:\n"
        "## SLIDE N: [Statement headline — not a vague topic label]\n"
        "- Bullet or key point\n"
        "- Bullet or key point\n"
        "- Bullet or key point\n"
        "[NOTES: Short speaker notes]\n\n"
        "- Slide 1 = hook or tension (not a generic title slide unless the user asks)\n"
        "- One idea per slide; rule of three where it helps\n"
        "- Last slide = clear next step / CTA — not “Thank you” alone\n"
    ),
    "captions": (
        "You are a social media content expert. Write punchy, engaging captions "
        "optimized for each platform. Include relevant emojis and hashtags. "
        "Provide 3 variations: one for Instagram, one for LinkedIn, one for Twitter/X."
    ),
    "scripts": (
        "You are a professional scriptwriter. Write natural, conversational scripts "
        "with clear speaker cues and stage directions. Adapt tone to context. "
        "Format: [INTRO] ... [MAIN] ... [OUTRO] with timing marks."
    ),
    "general": (
        "You are an intelligent office assistant for a creative agency.\n\n"
        "RESPONSE STYLE:\n"
        "- Match response length to question complexity\n"
        "- Short questions get short answers\n"
        "- Never use filler phrases like \"Certainly!\" or \"Great question!\"\n"
        "- Get to the answer immediately\n"
        "- Use bullet points only when listing 3+ distinct items\n"
        "- Otherwise write in natural prose\n\n"
        "TONE:\n"
        "- Professional but warm\n"
        "- Direct and confident\n"
        "- Never robotic or overly formal\n\n"
        "THINKING:\n"
        "- If a request is ambiguous, make a reasonable assumption \n"
        "  and state it, rather than asking clarifying questions\n"
        "- If you spot a problem the user hasn't noticed, mention it\n"
        "- Always think about what the user actually needs, \n"
        "  not just what they literally asked\n\n"
        "FORMATTING:\n"
        "- Use markdown only when it genuinely helps readability\n"
        "- Never bold random words mid-sentence\n"
        "- Code always goes in code blocks\n"
        "- Keep responses scannable for busy professionals"
    ),
    "meetings": (
        "You are a professional Executive Assistant. Parse this meeting transcript or notes and output exactly four sections:\n"
        "1. Executive Summary (2-3 sentences)\n"
        "2. Key Decisions Made (Bullet points)\n"
        "3. Action Items (Use checkboxes [ ] and bold the owner's name)\n"
        "4. Suggested Agenda for Next Meeting\n"
        "Do not include any other commentary."
    ),
    "announcements": (
        "You are a professional Internal Communications Manager. "
        "Take the rough notes provided and format them into a polite, clear, and professional office announcement. "
        "Include a clear Subject line, proper greetings/sign-offs, and organized formatting. "
        "Make it ready to copy-paste into an email or WhatsApp group."
    ),
    "email": (
        "Write professional emails that are:\n"
        "- Clear and scannable with one main ask\n"
        "- Appropriate length (never longer than needed)\n"
        "- Culturally appropriate for Indian business context\n"
        "- Never starting with \"I hope this email finds you well\""
    ),
    "content": (
        "When writing content:\n"
        "- Match the brand voice of the client\n"
        "- SEO-aware without being keyword-stuffed\n"
        "- Always ask about target audience if not specified\n"
        "- Provide 2-3 headline options, not just one"
    ),
    "analysis": (
        "When analysing anything:\n"
        "- Lead with the conclusion, not the methodology\n"
        "- Use specific numbers and examples\n"
        "- Flag assumptions you're making\n"
        "- End with a clear recommended action"
    ),
}

DEFAULT_SYSTEM = SYSTEM_PROMPTS["general"]
MAX_TOKENS     = 4096


# ── Task Auto-Detection ───────────────────────────────────────────────────────
_TASK_KEYWORDS = {
    "coding":        ["code", "python", "javascript", "typescript", "function", "class",
                      "debug", "bug", "fix", "implement", "algorithm", "sql", "api",
                      "backend", "frontend", "database", "error", "exception"],
    "html_design":   ["html", "css", "webpage", "website", "design", "ui", "ux",
                      "layout", "component", "landing page", "interface", "tailwind"],
    "presentations": ["slide", "presentation", "deck", "powerpoint", "pitch", "keynote"],
    "captions":      ["caption", "instagram", "twitter", "linkedin", "social media",
                      "post", "hashtag", "tweet"],
    "scripts":       ["script", "dialogue", "scene", "screenplay", "video script",
                      "podcast", "voiceover"],
    "meetings":      ["meeting", "transcript", "notes", "agenda", "call recording", "discussion"],
    "announcements": ["announcement", "notice", "office update", "regarding:", "memo", "bulletin"],
    "email":         ["email", "draft email", "write email", "reply to", "compose email",
                      "subject line", "email template", "follow up email"],
    "content":       ["blog", "article", "content", "copywriting", "write about", "seo",
                      "product description", "tagline", "brand voice", "headline"],
    "data_analysis": ["analyse", "analyze", "analysis", "breakdown", "compare", "review",
                      "evaluate", "pros and cons", "research", "insights", "report"],
}

from functools import lru_cache

@lru_cache(maxsize=1000)
def _detect_task(message: str) -> str:
    """Detect task type from message keywords. Cached to save processing time."""
    lower = message.lower()
    for task, keywords in _TASK_KEYWORDS.items():
        if any(k in lower for k in keywords):
            return task
    return "general"

# ── Complexity Detection ──────────────────────────────────────────────────────
_COMPLEX_INDICATORS = [
    "analyse", "analyze", "compare", "evaluate", "strategy", "plan", "build",
    "design", "research", "write a full", "write a complete", "create a",
    "step by step", "in detail", "comprehensive", "explain how", "why does",
    "what are all", "pros and cons",
]

def _is_complex(message: str) -> bool:
    """Returns True if the message is likely a complex, multi-step request."""
    lower = message.lower()
    if len(lower.split()) < 8:
        return False
    return any(kw in lower for kw in _COMPLEX_INDICATORS)

# ── Uncertainty Calibration ────────────────────────────────────────────────
_UNCERTAINTY_TRIGGERS = [
    "recent events", "current prices", "latest version", "today", "right now",
    "live data", "stock price", "weather", "news", "this week", "this month",
    "right now", "currently",
]

def _needs_uncertainty_disclaimer(question: str) -> bool:
    lower = question.lower()
    return any(t in lower for t in _UNCERTAINTY_TRIGGERS)

# ── Quality Check ──────────────────────────────────────────────────────────────
_FILLER_PHRASES = [
    "Certainly!", "Great question!", "Of course!", "Absolutely!",
    "I'd be happy to", "I would be happy to", "Sure thing!",
    "Excellent question!", "That's a great",
]

def _quality_check(response: str, task_type: str, model_name: str) -> str:
    """Check response quality and fix issues using Haiku (cheap model)."""
    issues = []
    for filler in _FILLER_PHRASES:
        if filler in response:
            issues.append(f'Remove filler phrase: "{filler}"')
    if response.rstrip().endswith("...") or len(response.strip()) < 30:
        issues.append("Response appears incomplete — do not end with ellipsis, provide a full answer")
    if not issues:
        return response
    
    issue_list = "\n".join(f"- {i}" for i in issues)
    try:
        _haiku = os.getenv("HAIKU_MODEL", "claude-haiku-4-5-20251001")
        fix_resp = client.messages.create(
            model=_haiku,  # Always use cheapest model for cleanup — no reason to use Sonnet here
            max_tokens=2000,
            system="You are an editor. Fix the issues listed and return only the corrected response — nothing else.",
            messages=[{
                "role": "user",
                "content": f"Fix these issues in the response:\n{issue_list}\n\nOriginal response:\n{response}"
            }],
        )
        logger.info(f"Quality check fixed issues: {issues}")
        return _anthropic_response_text(fix_resp) or response
    except Exception as e:
        logger.warning(f"Quality check fix failed (non-fatal): {e}")
        return response

# ── Email Notification Helper (#4) ───────────────────────────────────────────
def _notify_email(subject: str, body: str):
    """
    Sends an email notification to the FOUNDER_EMAIL address via SMTP.
    Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, FOUNDER_EMAIL in .env.
    Non-fatal — silently logs if not configured.
    """
    import smtplib
    from email.mime.text import MIMEText

    smtp_host  = os.getenv("SMTP_HOST", "")
    smtp_port  = int(os.getenv("SMTP_PORT", 587))
    smtp_user  = os.getenv("SMTP_USER", "")
    smtp_pass  = os.getenv("SMTP_PASS", "")
    founder_email = os.getenv("FOUNDER_EMAIL", "")

    if not all([smtp_host, smtp_user, smtp_pass, founder_email]):
        logger.debug("Email not configured — skipping notification")
        return

    try:
        msg = MIMEText(body, "plain")
        msg["Subject"] = f"[Office Assistant] {subject}"
        msg["From"]    = smtp_user
        msg["To"]      = founder_email
        with smtplib.SMTP(smtp_host, smtp_port, timeout=8) as srv:
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.send_message(msg)
        logger.info(f"Email sent: {subject}")
    except Exception as e:
        logger.warning(f"Email notification failed (non-fatal): {e}")

# ── Smart File Context ───────────────────────────────────────────────────────────
def _smart_file_context(file_content: str, user_question: str, haiku_model: str) -> str:
    """For large files, extract only sections relevant to the user's question."""
    if len(file_content) < 3000:
        return file_content
    try:
        extract_resp = client.messages.create(
            model=haiku_model,
            max_tokens=1000,
            system="You are a document analyst. Extract only relevant sections from a document, verbatim.",
            messages=[{
                "role": "user",
                "content": (
                    f'From this document, extract only the sections relevant to: "{user_question}"\n\n'
                    f"Document:\n{file_content[:8000]}\n\n"
                    f"Return only the relevant excerpts, nothing else."
                )
            }],
        )
        logger.info(f"Smart file context: reduced {len(file_content)} chars for question")
        return _anthropic_response_text(extract_resp) or file_content[:4000]
    except Exception as e:
        logger.warning(f"Smart file context failed (non-fatal): {e}")
        return file_content[:4000]

# ── Conversation State Awareness ──────────────────────────────────────────────────
def _analyze_conversation_state(history: list, haiku_model: str) -> Optional[str]:
    """Detect if the user is frustrated or conversation is going in circles."""
    if len(history) < 6:
        return None
    try:
        recent = history[-6:]
        history_text = "\n".join(
            f"{m['role'].upper()}: {str(m.get('content',''))[:200]}" for m in recent
        )
        state_resp = client.messages.create(
            model=haiku_model,
            max_tokens=150,
            system="You are a conversation analyst. Be very terse.",
            messages=[{
                "role": "user",
                "content": (
                    f"Analyze this conversation in one line each:\n"
                    f"- Is the user frustrated? (yes/no)\n"
                    f"- Are we going in circles? (yes/no)\n"
                    f"- Core unresolved need (5 words max)\n\n"
                    f"History:\n{history_text}"
                )
            }],
        )
        return _anthropic_response_text(state_resp)
    except Exception as e:
        logger.warning(f"Conversation state analysis failed (non-fatal): {e}")
        return None

# ── Task Timeouts ─────────────────────────────────────────────────────────────────
TASK_TIMEOUTS = {
    "html_design":    120,  # 2 minutes
    "presentations":  180,  # 3 minutes
    "coding":          90,  # 90 seconds
    "analysis":        90,  # 90 seconds
    "general":         30,  # 30 seconds
    "email":           30,
    "content":         60,
    "captions":        30,
    "scripts":         60,
    "meetings":        45,
    "announcements":   30,
}



def _get_all_users_str() -> str:
    try:
        with open(EMPLOYEES_DB, "r") as f:
            data = json.load(f)
            return ", ".join([e.get("id") for e in data.get("employees", []) if e.get("id")])
    except Exception:
        return "api"

def _build_system_prompt(task_type: str, user_id: str, project_id: Optional[str] = None, message: str = "") -> list:
    base_prompt = SYSTEM_PROMPTS.get(task_type.lower().replace(" ", "_"), DEFAULT_SYSTEM)
    base_prompt += "\n\nBe concise and direct. No unnecessary preamble. No phrases like 'Certainly!' or 'Great question!'. Get to the answer immediately."
    
    # Auto-memory extraction instruction
    users_str = _get_all_users_str()
    base_prompt += f"""

## AUTO-MEMORY EXTRACTION
If the user shares new preferences, project details, or makes decisions you should remember, output a structured JSON profile update at the very end of your response inside EXACTLY this XML block:
<SAVE_MEMORY_PROFILE user="USER_ID">
{{
  "preferences": {{"communication_style": "...", "response_length": "..."}},
  "active_projects": ["...", "..."],
  "frequently_used": ["...", "..."],
  "past_decisions": [{{"date": "...", "decision": "...", "reason": "..."}}]
}}
</SAVE_MEMORY_PROFILE>

Valid USER_IDs you can assign memories to:
{users_str}
(If saving for the current user, use {user_id})"""

    # Uncertainty disclaimer for time-sensitive questions
    if message and _needs_uncertainty_disclaimer(message):
        base_prompt += (
            "\n\nIMPORTANT: This question may involve real-time or recent information. "
            "If you are not certain about any specific facts, explicitly say so. "
            "Do not present uncertain information as fact."
        )

    mem_ctx = memory_store.format_for_prompt(user_id)
    team_mem_ctx = memory_store.format_team_memories()
    
    sections = [base_prompt]
    if mem_ctx:
        sections.append(mem_ctx)
        
    # Shared team memories only (no HR tone profiles — aligns with Claude Projects + memory).
    if team_mem_ctx:
        sections.append("## Shared team memories\n" + team_mem_ctx)
    
    if project_id:
        project = project_store.get_project(project_id, user_id)
        if project:
            if project.get("custom_instructions"):
                sections.append(f"## Custom Instructions\n{project['custom_instructions']}")
            # Claude-Projects-like behavior: retrieve only relevant KB excerpts
            if message:
                matches = kb_retriever.search(project_id, user_id, message, limit=6)
                kb_ctx = kb_retriever.format_for_prompt(matches)
                if kb_ctx:
                    sections.append(kb_ctx)
    
    final_text = "\n\n".join(sections)
    # Same global behaviour stack as main chat (HTML/PPT/docs routes use this too).
    combined = MASTER_SYSTEM_PROMPT.rstrip() + "\n\n" + final_text
    return [
        {
            "type": "text",
            "text": combined,
            "cache_control": {"type": "ephemeral"}
        }
    ]

# ── Anthropic response parsing ─────────────────────────────────────────────────
def _anthropic_response_text(response, *, include_thinking: bool = False) -> str:
    """
    Concatenate text from a Messages API response. Skips tool_use blocks.
    When include_thinking is False (default), skips thinking blocks — use that for
    user-facing answers. Set True only for internal reasoning snippets.
    Models that emit extended thinking often put a non-text block first; using only
    content[0].text raises or returns empty.
    """
    skip_tool = frozenset({"tool_use", "server_tool_use"})
    parts: list = []
    for block in getattr(response, "content", None) or []:
        typ = getattr(block, "type", None)
        if typ in skip_tool:
            continue
        if typ == "thinking":
            if include_thinking:
                parts.append(getattr(block, "thinking", "") or "")
            continue
        if typ == "redacted_thinking":
            if include_thinking:
                parts.append("")
            continue
        if typ == "text":
            t = getattr(block, "text", None)
            if t:
                parts.append(t)
        elif hasattr(block, "text") and getattr(block, "text", None):
            parts.append(block.text)
    out = "".join(parts).strip()
    if out:
        return out
    blocks = getattr(response, "content", None) or []
    if len(blocks) == 1 and hasattr(blocks[0], "text") and getattr(blocks[0], "text", None):
        return (blocks[0].text or "").strip()
    return ""


# ── Helper: call Claude ───────────────────────────────────────────────────────
def call_claude(task_type: str, message: str, user_id: str = "api",
                max_tokens: int = MAX_TOKENS, force_tier: Optional[str] = None,
                project_id: Optional[str] = None) -> dict:
    """
    Shared helper to call Claude with budget check + usage logging.
    Returns dict with: success, response, model_used, model_tier, tokens, cost_usd, budget
    """
    budget = check_budget_available()
    if not budget["allowed"]:
        return {"success": False, "error": "Monthly budget limit reached", "budget": budget}

    model_config  = get_model_for_task(task_type) if not force_tier else _build_config(force_tier)
    model_name    = model_config["name"]
    model_tier    = model_config["tier"]
    system_prompt = _build_system_prompt(task_type, user_id, project_id, message=message)

    logger.info(f"Claude call | task={task_type} | model={model_tier} | user={user_id}")

    # ── Reasoning step for complex requests ─────────────────────────────────
    reasoning_context = ""
    if _is_complex(message) and task_type not in ("html_design", "presentations", "coding"):
        try:
            think_kwargs = {
                "model": model_name,
                "max_tokens": 600,
                "system": "You are a thinking assistant. Your job is to reason through requests briefly before they are answered.",
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Before answering this request, think through:\n"
                        f"1. What is the user actually trying to achieve?\n"
                        f"2. What information do I need?\n"
                        f"3. What is the best format for the answer?\n"
                        f"4. Are there any risks or issues to flag?\n\n"
                        f"Request: {message}\n\n"
                        f"Respond with your thinking ONLY — no final answer."
                    )
                }],
            }
            think_resp = client.messages.create(**think_kwargs)
            think_txt = _anthropic_response_text(think_resp, include_thinking=True)
            reasoning_context = f"\n\n[Thinking]\n{think_txt}\n\n[Now answer the original request]\n"
            logger.info(f"Reasoning step added for complex task | user={user_id}")
        except Exception as e:
            logger.warning(f"Reasoning step failed (non-fatal): {e}")
    # ── End reasoning step ───────────────────────────────────────────────────

    try:
        kwargs = {
            "model": model_name,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": reasoning_context + message}],
        }
        # Extended output & prompt caching headers
        kwargs["extra_headers"] = {"anthropic-beta": "prompt-caching-2024-07-31"}
        if max_tokens > 8192:
            if "claude-3-5" in model_name:
                kwargs["extra_headers"]["anthropic-beta"] += ",max-tokens-3-5-sonnet-2024-07-15"
            else:
                kwargs["extra_headers"]["anthropic-beta"] += ",output-128k-2025-02-19"

        response = client.messages.create(**kwargs)
        output_text = _anthropic_response_text(response)
        if not output_text:
            logger.error("Claude returned no text blocks (present?). content=%r", getattr(response, "content", None))
            return {"success": False, "error": "Claude returned an empty response. Try again or shorten the deck."}
        # ── Quality check (auto-fix filler phrases & incomplete responses) ──
        output_text = _quality_check(output_text, task_type, model_name)
        # ── End quality check ──
        input_tokens  = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost          = calculate_cost(model_tier, input_tokens, output_tokens)

        record_usage(
            task_type=task_type, model_tier=model_tier, model_name=model_name,
            input_tokens=input_tokens, output_tokens=output_tokens, cost=cost, user_id=user_id,
        )
        updated_budget = check_budget_available()
        return {
            "success":    True,
            "response":   output_text,
            "model_used": model_name,
            "model_tier": model_tier,
            "task_type":  task_type,
            "tokens": {"input": input_tokens, "output": output_tokens, "total": input_tokens + output_tokens},
            "cost_usd":   cost,
            "budget": {
                "spent":     updated_budget["spent"],
                "remaining": updated_budget["remaining"],
                "limit":     updated_budget["limit"],
            },
        }
    except anthropic.AuthenticationError:
        logger.error("Invalid Anthropic API key")
        return {"success": False, "error": "Invalid API key. Check ANTHROPIC_API_KEY in config/.env"}
    except anthropic.RateLimitError:
        logger.warning("Anthropic rate limit hit")
        return {"success": False, "error": "Rate limit reached. Please wait and retry."}
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return {"success": False, "error": f"Claude API error: {str(e)}"}
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return {"success": False, "error": "Internal server error"}


def _build_config(tier: str) -> dict:
    from model_router import MODEL_COSTS
    c = MODEL_COSTS.get(tier, MODEL_COSTS["haiku"]).copy()
    c["tier"] = tier
    return c


# ── Employee Helpers ──────────────────────────────────────────────────────────
def _load_employees() -> dict:
    if EMPLOYEES_DB.exists():
        with open(EMPLOYEES_DB) as f:
            return json.load(f)
    return {"employees": []}


def _save_employees(data: dict):
    with open(EMPLOYEES_DB, "w") as f:
        json.dump(data, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

# ── Health ─────────────────────────────────────────────────────────────────────
@app.route("/api/health", methods=["GET"])
def health():
    """Health check endpoint."""
    import importlib.util

    budget = get_usage_summary()
    return jsonify({
        "status": "ok",
        "service": "Claude Office Assistant API",
        "budget_remaining": budget["remaining"],
        "budget_percent_used": budget["percent_used"],
        "pptx_export_ready": importlib.util.find_spec("pptx") is not None,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })


# ── Routing Table ─────────────────────────────────────────────────────────────
@app.route("/api/routes", methods=["GET"])
def list_routes():
    """Returns the task → model routing table."""
    return jsonify({
        "routing_table": get_all_routes(),
        "models": {
            "haiku":  os.getenv("HAIKU_MODEL",  "claude-haiku-4-5"),
            "sonnet": os.getenv("SONNET_MODEL", "claude-sonnet-4-5"),
        }
    })




# ── Budget ────────────────────────────────────────────────────────────────────
@app.route("/api/budget", methods=["GET"])
def budget_status():
    """Returns current month budget usage + alert level for dashboard banner."""
    summary = get_usage_summary()
    pct = summary.get("percent_used", 0)
    alert_level = "critical" if pct >= 90 else "warning" if pct >= 70 else "ok"
    return jsonify({**summary, "alert_level": alert_level, "percent_used": pct})


# ── Usage Dashboard (Section 4.3) ─────────────────────────────────────────────
@app.route("/api/usage", methods=["GET"])
def usage_dashboard():
    """
    Full usage dashboard data — powers the cost monitoring dashboard.
    Returns aggregated stats, per-user breakdown, per-task breakdown, and all calls.
    """
    all_flag = request.args.get("all", "false").lower() == "true"
    summary  = get_usage_summary(all_calls=all_flag)

    return jsonify({
        **summary,
        "budget_alerts": {
            "warning_threshold":  round(summary["budget_limit"] * 0.80, 2),
            "critical_threshold": round(summary["budget_limit"] * 0.90, 2),
            "limit":              summary["budget_limit"],
            "current_spend":      summary["monthly_spend"],
            "alert_level": (
                "CRITICAL" if summary["percent_used"] >= 90 else
                "WARNING"  if summary["percent_used"] >= 80 else
                "OK"
            ),
        },
    })

@app.route("/api/usage/export", methods=["GET"])
def export_usage():
    import csv
    import re
    from io import StringIO
    calls = get_all_usage_logs()
    
    emp_map = {}
    try:
        emp_file = Path(__file__).parent.parent / "config" / "employees.json"
        with open(emp_file, "r") as f:
            emps = json.load(f).get("employees", [])
            for e in emps:
                emp_map[e["id"]] = e["name"]
                if e.get("whatsapp"):
                    emp_map[e["whatsapp"]] = e["name"]
                    emp_map[e["whatsapp"].replace('+', '')] = e["name"]
    except Exception:
        pass

    def format_user(uid):
        if uid in emp_map:
            return emp_map[uid]
        # Mask phone numbers like we do in the frontend
        if re.match(r"^\+?\d{10,15}$", uid):
            return f"WhatsApp ({uid[-4:]})"
        return uid

    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["Timestamp", "User", "Task", "Model Tier", "Model Name", "Input Tokens", "Output Tokens", "Cost (USD)", "Cost (INR)", "Month"])
    for c in reversed(calls):
        uid = c.get("user_id", "")
        user_name = format_user(uid)
        cost_usd = c.get("cost_usd", 0)
        cost_inr = round(cost_usd * 83.5, 2) if cost_usd else 0.0
        
        cw.writerow([
            c.get("timestamp"), user_name, c.get("task_type"),
            c.get("model_tier"), c.get("model_name"), c.get("input_tokens"),
            c.get("output_tokens"), cost_usd, cost_inr, c.get("month")
        ])
    
    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=claude_api_usage.csv"}
    )

# ── Main Chat ─────────────────────────────────────────────────────────────────
@app.route("/api/chat", methods=["POST"])
def chat():
    """
    Main chat endpoint.
    Body: { task_type, message, user_id? }
    """
    data      = request.get_json(silent=True) or {}
    task_type = data.get("task_type", "").strip()
    message   = " ".join(data.get("message", "").split())
    user_id   = data.get("user_id",   "anonymous")

    if not task_type:
        return jsonify({"error": "task_type is required"}), 400
    if not message:
        return jsonify({"error": "message is required"}), 400

    result = call_claude(task_type, message, user_id)

    if not result["success"]:
        err = result.get("error", "")
        if "Invalid API" in err:
            status = 401
        elif "Rate limit" in err or "budget" in err.lower():
            status = 429
        else:
            status = 500
        return jsonify({"error": err, **{k: v for k, v in result.items() if k != "error"}}), status

    return jsonify(result)


# ── HTML Generator (Advanced Feature — Days 10-11) ────────────────────────────
@app.route("/api/html/generate", methods=["POST"])
def html_generate():
    """
    Generates a complete, self-contained HTML page from a description.
    Body: { description, style_hints?, user_id? }
    Returns: { html_code, preview_url, ... }
    """
    data        = request.get_json(silent=True) or {}
    description = data.get("description", "").strip()
    style_hints = data.get(
        "style_hints",
        "modern, distinctive typography, CSS variables, strong colour direction — avoid glassmorphism",
    ).strip()
    user_id     = data.get("user_id", "anonymous")

    if not description:
        return jsonify({"error": "description is required"}), 400

    import random
    
    style_directions = [
        "Bold editorial — black background, strong typography, minimal color",
        "Warm luxury — cream backgrounds, serif fonts, gold accents",
        "Clean minimal — white space, thin lines, muted palette",
        "Vibrant modern — bright accent colors, geometric shapes, sans-serif",
        "Dark industrial — charcoal tones, monospace elements, sharp edges"
    ]
    
    font_pairs = [
        ("Bebas Neue", "DM Sans", "Bold display + clean body"),
        ("Playfair Display", "Source Sans Pro", "Elegant serif + modern body"),
        ("Syne", "Manrope", "Geometric display + readable UI body (avoid Inter as primary display)"),
        ("Cabinet Grotesk", "Lora", "Modern grotesk + literary body"),
        ("Clash Display", "Satoshi", "Contemporary display pair"),
    ]
    
    chosen_style = random.choice(style_directions)
    chosen_fonts = random.choice(font_pairs)

    prompt = (
        f"Build a complete, production-quality, self-contained HTML page for: {description}\n\n"
        f"Design aesthetic: {chosen_style}\n"
        f"Fonts: {chosen_fonts[0]} for all headings, {chosen_fonts[1]} for body text.\n"
        f"Additional style hints: {style_hints}\n\n"
        "OUTPUT RULES (CRITICAL — do not skip any):\n"
        "1. Output ONLY raw HTML starting with <!DOCTYPE html> and ending with </html>\n"
        "2. NO markdown fences, NO explanations, NO comments like '// rest of code'\n"
        "3. Write EVERY section in FULL — no abbreviations, no '...' placeholders\n\n"
        "REQUIRED SECTIONS (all must be fully implemented):\n"
        "- Navigation bar with logo and links, smooth scroll on click\n"
        "- Hero section with headline, subheadline, CTA button, and a visual element\n"
        "- Features / Services section (at least 3 items with icons or illustrations)\n"
        "- About or social proof section (testimonials, stats, or team)\n"
        "- Call to action section\n"
        "- Footer with links and copyright\n\n"
        "REQUIRED CSS:\n"
        "- CSS custom properties (variables) for all colors, fonts, and spacing\n"
        "- Hover effects and transitions on all interactive elements\n"
        "- Smooth scroll behavior on <html>\n"
        "- At least 2 responsive breakpoints (tablet + mobile)\n"
        "- Keyframe animations for entrance effects\n\n"
        "REQUIRED JAVASCRIPT:\n"
        "- IntersectionObserver for scroll-triggered animations on sections\n"
        "- Mobile hamburger menu toggle\n"
        "- Active nav link highlighting on scroll\n\n"
        "Target output: 500-900 lines of complete, dense, production-quality HTML/CSS/JS."
    )

    def validate_html_output(html):
        issues = []
        if "</html>" not in html.lower():
            issues.append("incomplete")
        if "<style>" in html.lower() and "</style>" not in html.lower():
            issues.append("css_truncated")
        if html.count("{") != html.count("}"):
            issues.append("css_syntax_error")
        return issues

    max_retries = 2
    for attempt in range(max_retries):
        result = call_claude("html_design", prompt, user_id, max_tokens=16000)
        if not result["success"]:
            return jsonify({"error": result.get("error", "Generation failed")}), 500

        html_code = result["response"]
        
        # Strip markdown code fences if Claude wrapped the output
        if html_code.strip().startswith("```"):
            lines = html_code.strip().split("\n")
            inner = lines[1:] if len(lines) > 1 else lines
            if inner and inner[-1].strip() == "```":
                inner = inner[:-1]
            html_code = "\n".join(inner).strip()
            
        issues = validate_html_output(html_code)
        if not issues or attempt == max_retries - 1:
            break

    return jsonify({
        **result,
        "html_code": html_code,
        "preview_endpoint": "/api/html/preview",
    })

@app.route("/api/html/generate/stream", methods=["POST"])
def html_generate_stream():
    """
    Streams the generation of a complete HTML page using Server-Sent Events.
    Body: { description, style_hints?, user_id? }
    """
    data        = request.get_json(silent=True) or {}
    description = data.get("description", "").strip()
    style_hints = data.get(
        "style_hints",
        "modern, distinctive typography, CSS variables, strong colour direction — avoid glassmorphism",
    ).strip()
    user_id     = data.get("user_id", "anonymous")

    if not description:
        return jsonify({"error": "description is required"}), 400

    import random
    
    style_directions = [
        "Bold editorial — black background, strong typography, minimal color",
        "Warm luxury — cream backgrounds, serif fonts, gold accents",
        "Clean minimal — white space, thin lines, muted palette",
        "Vibrant modern — bright accent colors, geometric shapes, sans-serif",
        "Dark industrial — charcoal tones, monospace elements, sharp edges"
    ]
    
    font_pairs = [
        ("Bebas Neue", "DM Sans", "Bold display + clean body"),
        ("Playfair Display", "Source Sans Pro", "Elegant serif + modern body"),
        ("Syne", "Manrope", "Geometric display + readable UI body (avoid Inter as primary display)"),
        ("Cabinet Grotesk", "Lora", "Modern grotesk + literary body"),
        ("Clash Display", "Satoshi", "Contemporary display pair"),
    ]
    
    chosen_style = random.choice(style_directions)
    chosen_fonts = random.choice(font_pairs)

    prompt = (
        f"Build a complete, production-quality, self-contained HTML page for: {description}\n\n"
        f"Design aesthetic: {chosen_style}\n"
        f"Fonts: {chosen_fonts[0]} for all headings, {chosen_fonts[1]} for body text.\n"
        f"Additional style hints: {style_hints}\n\n"
        "OUTPUT RULES (CRITICAL — do not skip any):\n"
        "1. Output ONLY raw HTML starting with <!DOCTYPE html> and ending with </html>\n"
        "2. NO markdown fences, NO explanations, NO comments like '// rest of code'\n"
        "3. Write EVERY section in FULL — no abbreviations, no '...' placeholders\n\n"
        "REQUIRED SECTIONS (all must be fully implemented):\n"
        "- Navigation bar with logo and links, smooth scroll on click\n"
        "- Hero section with headline, subheadline, CTA button, and a visual element\n"
        "- Features / Services section (at least 3 items with icons or illustrations)\n"
        "- About or social proof section (testimonials, stats, or team)\n"
        "- Call to action section\n"
        "- Footer with links and copyright\n\n"
        "REQUIRED CSS:\n"
        "- CSS custom properties (variables) for all colors, fonts, and spacing\n"
        "- Hover effects and transitions on all interactive elements\n"
        "- Smooth scroll behavior on <html>\n"
        "- At least 2 responsive breakpoints (tablet + mobile)\n"
        "- Keyframe animations for entrance effects\n\n"
        "REQUIRED JAVASCRIPT:\n"
        "- IntersectionObserver for scroll-triggered animations on sections\n"
        "- Mobile hamburger menu toggle\n"
        "- Active nav link highlighting on scroll\n\n"
        "Target output: 500-900 lines of complete, dense, production-quality HTML/CSS/JS."
    )

    budget = check_budget_available()
    if not budget["allowed"]:
        def _budget_err():
            yield f"data: {json.dumps({'type':'error','error':'Monthly budget limit reached'})}\n\n"
        return Response(stream_with_context(_budget_err()), mimetype="text/event-stream")

    model_config  = get_model_for_task("html_design")
    model_name    = model_config["name"]
    model_tier    = model_config["tier"]
    system_prompt = _build_system_prompt("html_design", user_id, None)

    def generate():
        full_response  = ""
        input_tokens   = 0
        output_tokens  = 0

        kwargs = {
            "model": model_name,
            "max_tokens": 16000,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Use correct extended-output and prompt caching beta per model family
        kwargs["extra_headers"] = {"anthropic-beta": "prompt-caching-2024-07-31"}
        if "claude-3-5" in model_name:
            kwargs["extra_headers"]["anthropic-beta"] += ",max-tokens-3-5-sonnet-2024-07-15"
        else:
            kwargs["extra_headers"]["anthropic-beta"] += ",output-128k-2025-02-19"

        logger.info(f"HTML Stream | model={model_tier} | user={user_id}")

        try:
            with client.messages.stream(**kwargs) as stream:
                for text_chunk in stream.text_stream:
                    full_response += text_chunk
                    yield f"data: {json.dumps({'type':'text','text':text_chunk})}\n\n"

                final_msg     = stream.get_final_message()
                input_tokens  = final_msg.usage.input_tokens
                output_tokens = final_msg.usage.output_tokens
                cost          = calculate_cost(model_tier, input_tokens, output_tokens)

                record_usage(
                    task_type="html_design",
                    model_tier=model_tier,
                    model_name=model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost=cost,
                    user_id=user_id,
                )

                yield f"data: {json.dumps({'type':'done','cost_usd':cost,'tokens':{'input':input_tokens,'output':output_tokens},'budget':check_budget_available()})}\n\n"
        except Exception as e:
            logger.error(f"HTML stream error: {e}")
            yield f"data: {json.dumps({'type':'error','error':str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/html/preview", methods=["POST"])
def html_preview():
    """Renders raw HTML for live preview in an iframe."""
    data = request.get_json(silent=True) or {}
    html = data.get("html", "")
    return html, 200, {"Content-Type": "text/html"}


# ── Presentation Creator (Advanced Feature) ───────────────────────────────────
@app.route("/api/presentation", methods=["POST"])
def create_presentation():
    """
    Generates structured slide content from a topic/outline.
    Body: { topic, slide_count?, audience?, tone?, user_id? }
    Returns: { slides: [{title, bullets, notes}], raw_markdown, ... }
    """
    data        = request.get_json(silent=True) or {}
    topic       = data.get("topic", "").strip()
    slide_count = data.get("slide_count", 10)
    audience    = data.get("audience", "general business audience")
    tone        = data.get("tone", "professional")
    user_id     = data.get("user_id", "anonymous")

    if not topic:
        return jsonify({"error": "topic is required"}), 400

    prompt = (
        f"Create a {slide_count}-slide presentation on: {topic}\n\n"
        f"Audience: {audience}\n"
        f"Tone: {tone}\n\n"
        "Follow your system instructions for slide structure (hook first slide, statement headlines, CTA last).\n"
        "Format each slide EXACTLY as:\n"
        "## SLIDE N: [Statement headline]\n"
        "- [Point]\n"
        "- [Point]\n"
        "- [Point]\n"
        "[NOTES: Speaker notes]\n\n"
        "If the user asked for images or visuals, add one optional line per slide with a real https URL, e.g. "
        "IMAGE: https://images.unsplash.com/... (pick relevant stock imagery).\n\n"
        f"Number slides 1–{slide_count}. Slide 1 must grab attention; slide {slide_count} must drive a specific action."
    )

    result = call_claude("presentations", prompt, user_id, max_tokens=25000)
    if not result["success"]:
        return jsonify({"success": False, "error": result.get("error", "Generation failed")}), 500

    # Parse slides from markdown
    try:
        slides = _parse_slides(result["response"])
    except Exception as e:
        logger.exception("Slide parse failed: %s", e)
        return jsonify({"success": False, "error": "Could not parse slide format from Claude response."}), 500

    return jsonify({
        **result,
        "slides": slides,
        "slide_count": len(slides),
        "raw_markdown": result["response"],
    })


def _parse_slides(markdown: str) -> list:
    """Parse ## SLIDE N: Title format into structured slide objects."""
    import re
    slides = []
    blocks = re.split(r"(?=##\s*SLIDE\s+\d+\s*:)", markdown, flags=re.IGNORECASE)
    hdr_re = re.compile(r"^##\s*SLIDE\s+\d+:\s*(.+)$", re.IGNORECASE)
    for block in blocks:
        if not block.strip():
            continue
        lines   = block.strip().split("\n")
        header  = lines[0]
        title_m = hdr_re.match(header.strip())
        title   = title_m.group(1).strip() if title_m else header.strip().lstrip("#").strip()

        bullets = []
        notes   = ""
        for line in lines[1:]:
            ls = line.strip()
            low = ls.lower()
            if low.startswith("[notes:") or low.startswith("[notes :"):
                inner = ls.split(":", 1)[-1].rstrip("]").strip()
                notes = inner
            elif ls.startswith("- "):
                bullets.append(ls[2:].strip())
            elif re.match(r"^\s*(?:IMAGE|IMG|img)\s*:", ls, re.I):
                continue

        slides.append({"title": title, "bullets": bullets, "notes": notes})
    return slides


# ── Employee Auth & Attendance ────────────────────────────────────────────────
# NOTE: /api/auth/login, /api/auth/verify, /api/auth/logout are defined further
# below (around line 1228) as the full token-session implementations.
# The old attendance-only stubs have been removed to prevent Flask startup errors.

def _attendance_conn():
    from db import get_connection
    return get_connection()

def _utc_now_parts():
    now = datetime.utcnow()
    return now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), now.isoformat() + "Z"

def _attendance_payload():
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return body
    raw = request.get_data(cache=False, as_text=True) or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}

def _attendance_checkin(user_id: str):
    date_utc, time_utc, timestamp_utc = _utc_now_parts()
    conn = _attendance_conn()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO daily_attendance (user_id, date, checkin_time) VALUES (?, ?, ?)",
            (user_id, date_utc, time_utc),
        )
        conn.execute(
            "UPDATE daily_attendance SET checkin_time=? WHERE user_id=? AND date=? AND checkin_time IS NULL",
            (time_utc, user_id, date_utc),
        )
        conn.execute(
            "INSERT INTO attendance (user_id, action, timestamp) VALUES (?, 'in', ?)",
            (user_id, timestamp_utc),
        )
    conn.close()
    return date_utc, time_utc

def _attendance_checkout(user_id: str):
    date_utc, time_utc, timestamp_utc = _utc_now_parts()
    conn = _attendance_conn()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO daily_attendance (user_id, date) VALUES (?, ?)",
            (user_id, date_utc),
        )
        conn.execute(
            "UPDATE daily_attendance SET checkout_time=? WHERE user_id=? AND date=?",
            (time_utc, user_id, date_utc),
        )
        conn.execute(
            "INSERT INTO attendance (user_id, action, timestamp) VALUES (?, 'out', ?)",
            (user_id, timestamp_utc),
        )
    conn.close()
    return date_utc, time_utc

@app.route("/api/attendance/checkin", methods=["POST"])
def attendance_checkin():
    body = _attendance_payload()
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    date_utc, checkin_time = _attendance_checkin(user_id)
    return jsonify({"success": True, "user_id": user_id, "date": date_utc, "checkin_time": checkin_time})

@app.route("/api/attendance/checkout", methods=["POST"])
def attendance_checkout():
    body = _attendance_payload()
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    date_utc, checkout_time = _attendance_checkout(user_id)
    return jsonify({"success": True, "user_id": user_id, "date": date_utc, "checkout_time": checkout_time})

@app.route("/api/attendance/summary", methods=["GET"])
def attendance_summary():
    user_id = request.args.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    conn = _attendance_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT date, checkin_time, checkout_time FROM daily_attendance WHERE user_id=? ORDER BY date DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return jsonify({
        "user_id": user_id,
        "records": [
            {"date": r[0], "checkin_time": r[1], "checkout_time": r[2]}
            for r in rows
        ],
    })

@app.route("/api/attendance/today", methods=["GET"])
def attendance_today():
    date_utc = datetime.utcnow().strftime("%Y-%m-%d")
    conn = _attendance_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, checkin_time, checkout_time FROM daily_attendance WHERE date=?",
        (date_utc,),
    )
    rows = cur.fetchall()
    conn.close()
    records = [
        {"user_id": r[0], "date": date_utc, "checkin_time": r[1], "checkout_time": r[2]}
        for r in rows
    ]
    return jsonify({"date": date_utc, "records": records})

@app.route("/api/auth/change_pin", methods=["POST"])
def auth_change_pin():
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    old_pin = body.get("old_pin")
    new_pin = body.get("new_pin")
    
    data = _load_employees()
    found = False
    for emp in data.get("employees", []):
        if emp.get("id") == user_id:
            if str(emp.get("pin")) != str(old_pin):
                return jsonify({"error": "Incorrect old PIN"}), 401
            emp["pin"] = new_pin
            found = True
            break
            
    if not found:
        return jsonify({"error": "Employee not found"}), 404
        
    try:
        emp_file = Path(__file__).parent.parent / "config" / "employees.json"
        with open(emp_file, "w") as f:
            json.dump(data, f, indent=2)
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Failed to save new PIN: {e}")
        return jsonify({"error": "Could not save PIN"}), 500

@app.route("/api/attendance/logs", methods=["GET"])
def attendance_logs():
    admin_id = request.args.get("user_id")
    # Vidit (emp001), Abhinav (emp003), Kshitij (emp004) are admins
    if admin_id not in ["emp001", "emp003", "emp004"]:
        return jsonify({"error": "Unauthorized"}), 403
        
    from db import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, action, timestamp FROM attendance ORDER BY timestamp DESC LIMIT 500")
    logs = [{"id": r[0], "user_id": r[1], "action": r[2], "timestamp": r[3]} for r in cursor.fetchall()]
    conn.close()
    
    return jsonify({"logs": logs})

@app.route("/api/attendance/export", methods=["GET"])
def attendance_export():
    admin_id = request.args.get("user_id")
    if admin_id not in ["emp001", "emp003", "emp004"]:
        return "Unauthorized", 403
        
    import csv
    import re
    from io import StringIO
    from db import get_connection
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, action, timestamp FROM attendance ORDER BY timestamp DESC")
    rows = cursor.fetchall()
    conn.close()
    
    emp_map = {}
    try:
        emp_file = Path(__file__).parent.parent / "config" / "employees.json"
        with open(emp_file, "r") as f:
            emps = json.load(f).get("employees", [])
            for e in emps:
                emp_map[e["id"]] = e["name"]
                if e.get("whatsapp"):
                    emp_map[e["whatsapp"]] = e["name"]
                    emp_map[e["whatsapp"].replace('+', '')] = e["name"]
    except Exception:
        pass

    def format_user(uid):
        if uid in emp_map:
            return emp_map[uid]
        if re.match(r"^\+?\d{10,15}$", uid):
            return f"WhatsApp ({uid[-4:]})"
        return uid

    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["Timestamp", "Employee", "Action"])
    for r in rows:
        uid = r[0]
        user_name = format_user(uid)
        cw.writerow([r[2], user_name, r[1].upper()])
        
    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=claude_attendance_logs.csv"}
    )

# ══════════════════════════════════════════════════════════════════════════════
# AUTH — Server-side session tokens (#1)
# ══════════════════════════════════════════════════════════════════════════════
import secrets
from datetime import timedelta

def _sessions_conn():
    from db import get_connection
    return get_connection()

def _create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat() + "Z"
    conn = _sessions_conn()
    with conn:
        conn.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
                     (token, user_id, expires))
    conn.close()
    return token

def _verify_session(token: str) -> Optional[str]:
    """Returns user_id if token is valid and not expired, else None."""
    if not token:
        return None
    conn = _sessions_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, expires_at FROM sessions WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    user_id, expires_at = row
    if datetime.utcnow().isoformat() + "Z" > expires_at:
        return None  # Expired
    return user_id

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """PIN login — returns a server-side session token."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "").strip()
    pin     = body.get("pin", "").strip()

    if not user_id or not pin:
        return jsonify({"error": "user_id and pin required"}), 400

    data = _load_employees()
    emp = next((e for e in data.get("employees", []) if e["id"] == user_id), None)
    if not emp:
        return jsonify({"error": "Employee not found"}), 404

    if emp.get("pin", "0000") != pin:
        return jsonify({"error": "Incorrect PIN"}), 401

    token = _create_session(user_id)
    return jsonify({
        "success": True,
        "token":   token,
        "user":    {"id": emp["id"], "name": emp["name"], "role": emp["role"],
                    "is_admin": _is_admin(user_id)},
    })

@app.route("/api/auth/verify", methods=["GET"])
def auth_verify():
    """Verify a session token. Used by frontend auth guard."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "") or request.headers.get("X-Session-Token", "") or request.args.get("token", "")
    user_id = _verify_session(token)
    if not user_id:
        return jsonify({"valid": False}), 401
    data = _load_employees()
    emp = next((e for e in data.get("employees", []) if e["id"] == user_id), {})
    return jsonify({"valid": True, "user_id": user_id, "name": emp.get("name",""),
                    "role": emp.get("role",""), "is_admin": _is_admin(user_id)})

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """Invalidate a session token."""
    body  = request.get_json(silent=True) or {}
    token = body.get("token", "") or request.headers.get("Authorization", "").replace("Bearer ", "") or request.headers.get("X-Session-Token", "")
    if token:
        user_id = _verify_session(token)
        if user_id:
            _attendance_checkout(user_id)
        conn = _sessions_conn()
        with conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.close()
    return jsonify({"success": True})

# ── Employee Tracking (WhatsApp Bot Support) ──────────────────────────────────
@app.route("/api/employees", methods=["GET"])
def get_employees():
    """Returns all employees with their current status."""
    data = _load_employees()
    return jsonify(data)


@app.route("/api/employees/checkin", methods=["POST"])
def employee_checkin():
    """
    Records an employee check-in or check-out.
    Body: { emp_id OR whatsapp, action: 'in'|'out', notes? }
    emp_id is preferred for the admin dashboard.
    """
    body     = request.get_json(silent=True) or {}
    emp_id   = body.get("emp_id", "").strip()
    whatsapp = body.get("whatsapp", "").strip()
    action   = body.get("action", "in").strip()
    notes    = body.get("notes", "")

    if not emp_id and not whatsapp:
        return jsonify({"error": "emp_id or whatsapp required"}), 400

    data = _load_employees()
    found = None
    for emp in data["employees"]:
        if emp_id and emp.get("id") == emp_id:
            found = emp
            break
        if whatsapp and emp.get("whatsapp") == whatsapp:
            found = emp
            break

    if not found:
        return jsonify({"error": "Employee not found"}), 404

    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "action":    action,
        "notes":     notes,
    }
    found.setdefault("checkins", []).append(entry)
    found["last_action"] = action
    found["last_seen"]   = entry["timestamp"]

    _save_employees(data)
    if action == "out":
        date_utc, time_utc = _attendance_checkout(found.get("id", ""))
    else:
        date_utc, time_utc = _attendance_checkin(found.get("id", ""))
    logger.info(f"Employee {found['name']} checked {action} at {entry['timestamp']}")

    return jsonify({
        "success":  True,
        "employee": found["name"],
        "action":   action,
        "time":     entry["timestamp"],
        "date":     date_utc,
        "time_utc": time_utc,
    })


@app.route("/api/employees/summary", methods=["GET"])
def employee_summary():
    """Returns today's attendance summary."""
    data  = _load_employees()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = _attendance_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, checkin_time, checkout_time FROM daily_attendance WHERE date=?",
        (today,),
    )
    attendance_map = {r[0]: {"checkin_time": r[1], "checkout_time": r[2]} for r in cur.fetchall()}
    conn.close()

    summary = []
    for emp in data["employees"]:
        daily = attendance_map.get(emp.get("id", ""), {})
        checkin_time = daily.get("checkin_time")
        checkout_time = daily.get("checkout_time")
        if checkin_time and not checkout_time:
            status = "in"
        elif checkin_time and checkout_time:
            status = "out"
        else:
            status = "not checked in"
        summary.append({
            "emp_id":     emp.get("id", ""),
            "name":       emp["name"],
            "role":       emp["role"],
            "department": emp["department"],
            "status":     status,
            "checkin_time": checkin_time,
            "checkout_time": checkout_time,
            "today_logs": [],
        })

    return jsonify({"date": today, "employees": summary, "total": len(summary)})


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-TURN CONVERSATION ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def call_claude_with_context(task_type: str, messages: list,
                             user_id: str = "api",
                             attachments: list = None,
                             project_id: str = None,
                             model_override: str = None) -> dict:
    """
    Call Claude with full conversation history + optional file attachments.
    Memories are automatically injected into the system prompt.
    attachments: [{type:'image'|'document', ...}] from file_processor
    """
    budget = check_budget_available()
    if not budget["allowed"]:
        return {"success": False, "error": "Monthly budget limit reached", "budget": budget}

    if model_override and model_override != "auto":
        model_name = model_override
        model_tier = "sonnet" if "sonnet" in model_name.lower() else "haiku" if "haiku" in model_name.lower() else "opus"
    else:
        model_config  = get_model_for_task(task_type)
        model_name    = model_config["name"]
        model_tier    = model_config["tier"]

    # Inject user memories and project context into system prompt
    haiku_model = get_model_for_task("general")["name"]
    system_prompt = _build_system_prompt(task_type, user_id, project_id, message=messages[-1].get("content", "") if messages else "")
    
    # ── Conversation state awareness ──
    conv_state = _analyze_conversation_state(messages, haiku_model)
    if conv_state and "frustrated: yes" in conv_state.lower():
        system_prompt = list(system_prompt)  # copy
        system_prompt[0] = {
            **system_prompt[0],
            "text": system_prompt[0]["text"] + "\n\nNOTE: The user seems frustrated. Be extra clear, concise, and directly address their core need. Acknowledge if previous responses may not have been helpful."
        }

    logger.info(f"Multi-turn | task={task_type} | model={model_tier} | turns={len(messages)} | files={len(attachments or [])} | user={user_id}")

    # Build message list — attach files to the last user message if any
    api_messages = list(messages)
    if attachments and api_messages and api_messages[-1]["role"] == "user":
        last_text = api_messages[-1]["content"]
        content_blocks = [{"type": "text", "text": last_text}]
        for att in attachments:
            if att.get("type") == "image":
                content_blocks.append({
                    "type": "image",
                    "source": {"type": "base64",
                               "media_type": att["media_type"],
                               "data": att["data"]},
                })
            elif att.get("type") == "document":
                # Smart file context — only extract relevant sections for large files
                user_question = last_text
                file_content  = att.get('content', '')
                smart_content = _smart_file_context(file_content, user_question, haiku_model)
                content_blocks[0]["text"] += (
                    f"\n\n---\nAttached file: {att['filename']}\n"
                    f"{smart_content}\n---"
                )
        api_messages = api_messages[:-1] + [{"role": "user", "content": content_blocks}]

    try:
        # Coding tasks need more room — full functions/modules can exceed 4096 tokens
        effective_max = 8192 if task_type in ("coding", "html_design") else MAX_TOKENS
        
        headers = {"anthropic-beta": "prompt-caching-2024-07-31"}
        if effective_max > 8192:
            if "claude-3-5" in model_name:
                headers["anthropic-beta"] += ",max-tokens-3-5-sonnet-2024-07-15"
            else:
                headers["anthropic-beta"] += ",output-128k-2025-02-19"
                
        response      = client.messages.create(
            model=model_name, max_tokens=effective_max,
            system=system_prompt, messages=api_messages,
            extra_headers=headers
        )
        output_text = _anthropic_response_text(response)
        if not output_text:
            logger.error("Claude returned no text (conversation path). content=%r", getattr(response, "content", None))
            return {"success": False, "error": "Claude returned an empty response. Try again."}
        input_tokens  = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost          = calculate_cost(model_tier, input_tokens, output_tokens)

        record_usage(task_type=task_type, model_tier=model_tier, model_name=model_name,
                     input_tokens=input_tokens, output_tokens=output_tokens,
                     cost=cost, user_id=user_id)
        updated_budget = check_budget_available()
        return {
            "success":    True,
            "response":   output_text,
            "model_used": model_name,
            "model_tier": model_tier,
            "task_type":  task_type,
            "tokens":     {"input": input_tokens, "output": output_tokens,
                           "total": input_tokens + output_tokens},
            "cost_usd":   cost,
            "budget":     {"spent":     updated_budget["spent"],
                           "remaining": updated_budget["remaining"],
                           "limit":     updated_budget["limit"]},
        }
    except anthropic.AuthenticationError:
        return {"success": False, "error": "Invalid API key. Check ANTHROPIC_API_KEY in config/.env"}
    except anthropic.RateLimitError:
        return {"success": False, "error": "Rate limit reached. Please wait and retry."}
    except anthropic.APIError as e:
        return {"success": False, "error": f"Claude API error: {str(e)}"}
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return {"success": False, "error": "Internal server error"}


# ── List conversations ────────────────────────────────────────────────────────
@app.route("/api/conversations", methods=["GET"])
def list_conversations():
    """GET /api/conversations?user_id=xxx"""
    user_id = request.args.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id query param is required"}), 400
    convs = conversation_store.list_conversations(user_id)
    return jsonify({"conversations": convs, "total": len(convs)})


# ── Create conversation ───────────────────────────────────────────────────────
@app.route("/api/conversations", methods=["POST"])
def create_conversation():
    """POST /api/conversations  body: {user_id, user_name, task_type?, project_id?}"""
    data      = request.get_json(silent=True) or {}
    user_id   = data.get("user_id",   "").strip()
    user_name = data.get("user_name", "Anonymous").strip()
    task_type = data.get("task_type")
    project_id = data.get("project_id")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    conv = conversation_store.create_conversation(user_id, user_name, task_type, project_id)
    return jsonify(conv), 201


# ── Get conversation ──────────────────────────────────────────────────────────
@app.route("/api/conversations/<conv_id>", methods=["GET"])
def get_conversation(conv_id):
    """GET /api/conversations/<id>  — returns full conversation with messages"""
    conv = conversation_store.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify(conv)


# ── Delete conversation ───────────────────────────────────────────────────────
@app.route("/api/conversations/<conv_id>", methods=["DELETE"])
def delete_conversation(conv_id):
    deleted = conversation_store.delete_conversation(conv_id)
    if not deleted:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify({"success": True})


# ── Rename conversation ───────────────────────────────────────────────────────
@app.route("/api/conversations/<conv_id>/title", methods=["PATCH"])
def rename_conversation(conv_id):
    data  = request.get_json(silent=True) or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    conv = conversation_store.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404
    conversation_store.update_title(conv_id, title)
    return jsonify({"success": True, "title": title})


# ── Multi-turn chat in a conversation ─────────────────────────────────────────
@app.route("/api/conversations/<conv_id>/chat", methods=["POST"])
def conversation_chat(conv_id):
    """
    Send a message in a conversation. Maintains full multi-turn context.
    Body: { message, task_type? }
    Returns: { response, model_used, model_tier, cost_usd, tokens, budget,
               conv_id, task_type, title }
    """
    conv = conversation_store.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    data        = request.get_json(silent=True) or {}
    message     = " ".join(data.get("message", "").split())
    attachments = data.get("attachments", [])  # [{type,filename,content|data,media_type}]
    if not message:
        return jsonify({"error": "message is required"}), 400

    # Determine task type: request override > conv stored > auto-detect
    task_type = (
        data.get("task_type")
        or conv.get("task_type")
        or _detect_task(message)
    )

    # Save user message to conversation
    conversation_store.add_message(conv_id, "user", message)

    # Lock in task_type if not already set
    if not conv.get("task_type"):
        conversation_store.update_task_type(conv_id, task_type)

    # Build context for Claude (full history including message just saved)
    context = conversation_store.get_context_messages(conv_id)

    # Call Claude with full conversation context + any file attachments
    result = call_claude_with_context(task_type, context, conv.get("user_id", "api"), attachments=attachments, project_id=conv.get("project_id"), model_override=data.get("model_override"))

    if not result["success"]:
        err = result.get("error", "")
        if "Invalid API" in err:  status = 401
        elif "Rate limit" in err or "budget" in err.lower(): status = 429
        else: status = 500
        return jsonify({"error": err, **{k: v for k, v in result.items() if k != "error"}}), status

    # Save assistant reply with metadata
    conversation_store.add_message(conv_id, "assistant", result["response"], {
        "model_tier": result["model_tier"],
        "model_used": result["model_used"],
        "cost_usd":   result["cost_usd"],
        "task_type":  task_type,
    })

    updated_conv = conversation_store.get_conversation(conv_id)
    return jsonify({
        **result,
        "conv_id":   conv_id,
        "task_type": task_type,
        "title":     updated_conv["title"] if updated_conv else "",
    })


# ── Streaming chat ──────────────────────────────────────────────────────────
@app.route("/api/conversations/<conv_id>/stream", methods=["POST"])
def conversation_stream(conv_id):
    """
    POST /api/conversations/<id>/stream
    Server-Sent Events: yields text chunks as Claude generates them.
    Events: {type:"text",text:"..."} | {type:"done",...stats} | {type:"error",error:"..."}
    """
    conv = conversation_store.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    data        = request.get_json(silent=True) or {}
    message     = " ".join(data.get("message", "").split())
    attachments = data.get("attachments", [])
    if not message:
        return jsonify({"error": "message is required"}), 400

    task_type = (data.get("task_type") or conv.get("task_type") or _detect_task(message))
    user_id   = conv.get("user_id", "api")

    truncate_idx = data.get("truncate_from_index")
    if truncate_idx is not None:
        conversation_store.truncate_messages(conv_id, int(truncate_idx))

    # Save user message and update task type
    conversation_store.add_message(conv_id, "user", message)
    if not conv.get("task_type"):
        conversation_store.update_task_type(conv_id, task_type)

    # Build context + attachments
    context = conversation_store.get_context_messages(conv_id)
    model_override = data.get("model_override")

    if model_override and model_override != "auto":
        model_name = model_override
        model_tier = "sonnet" if "sonnet" in model_name.lower() or "pro" in model_name.lower() else "haiku"
    else:
        model_config = get_model_for_task(task_type)
        model_name   = model_config["name"]
        model_tier   = model_config["tier"]

    mem_blocks = _build_system_prompt(task_type, user_id, conv.get("project_id"), message=message)
    final_system = mem_blocks[0]["text"] if mem_blocks else MASTER_SYSTEM_PROMPT

    system_prompt = [{
        "type": "text",
        "text": final_system,
        "cache_control": {"type": "ephemeral"}
    }]

    api_messages = list(context)
    if attachments and api_messages and api_messages[-1]["role"] == "user":
        last_text      = api_messages[-1]["content"]
        content_blocks = [{"type": "text", "text": last_text}]
        for att in attachments:
            if att.get("type") == "image":
                content_blocks.append({"type": "image",
                    "source": {"type": "base64", "media_type": att["media_type"], "data": att["data"]}})
            elif att.get("type") == "document":
                content_blocks[0]["text"] += f"\n\n---\nAttached: {att['filename']}\n{att.get('content','')}\n---"
        api_messages = api_messages[:-1] + [{"role": "user", "content": content_blocks}]

    # Check budget before opening stream
    budget = check_budget_available()
    if not budget["allowed"]:
        def _budget_err():
            yield f"data: {json.dumps({'type':'error','error':'Monthly budget limit reached'})}\n\n"
        return Response(stream_with_context(_budget_err()), mimetype="text/event-stream")

    def should_think(prompt: str, t_type: str) -> bool:
        THINKING_TASKS = ["coding", "data_analysis", "html_design", "presentations", "analysis"]
        THINKING_KEYWORDS = ["why", "how", "explain", "analyse", "analyze", "compare", "difference", "best way", "should i", "help me think", "strategy", "plan", "review", "feedback", "debug", "fix"]
        if t_type in THINKING_TASKS: return True
        if len(prompt.split()) > 30: return True
        if any(k in prompt.lower() for k in THINKING_KEYWORDS): return True
        return False

    def generate():
        full_response  = ""
        input_tokens   = 0
        output_tokens  = 0
        stream_error   = None
        model_used_for_call = model_name
        sent_thinking_start = False
        sent_thinking_end   = False

        logger.info(f"Stream | task={task_type} | model={model_tier} | user={user_id}")

        try:
            use_thinking = should_think(message, task_type)
            stream_kwargs = {
                "system": system_prompt,
                "messages": api_messages,
                "extra_headers": {"anthropic-beta": "prompt-caching-2024-07-31"}
            }
            if use_thinking:
                # Thinking is only available on Sonnet-class models. We pin to the
                # same Sonnet model shown in the UI for clarity/consistency.
                stream_kwargs["model"] = "claude-sonnet-4-6"
                stream_kwargs["max_tokens"] = 16000
                stream_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8000}
                model_used_for_call = stream_kwargs["model"]
            else:
                stream_kwargs["model"] = model_name
                stream_kwargs["max_tokens"] = MAX_TOKENS
                model_used_for_call = stream_kwargs["model"]

            with client.messages.stream(**stream_kwargs) as stream:
                for event in stream:
                    if hasattr(event, "type"):
                        if event.type == "content_block_start":
                            if hasattr(event, "content_block"):
                                if event.content_block.type == "thinking":
                                    sent_thinking_start = True
                                    yield "data: {\"type\":\"thinking_start\"}\n\n"
                                elif event.content_block.type == "text":
                                    sent_thinking_end = True
                                    yield "data: {\"type\":\"thinking_end\"}\n\n"
                        elif event.type == "content_block_delta":
                            if hasattr(event, "delta") and event.delta.type == "text_delta":
                                full_response += event.delta.text
                                yield f"data: {json.dumps({'type':'text','text':event.delta.text})}\n\n"

                final_msg     = stream.get_final_message()
                input_tokens  = final_msg.usage.input_tokens
                output_tokens = final_msg.usage.output_tokens

        except anthropic.AuthenticationError:
            stream_error = "Invalid API key — check ANTHROPIC_API_KEY"
        except anthropic.RateLimitError:
            stream_error = "Rate limit reached — please retry shortly"
        except anthropic.APIError as e:
            stream_error = f"Claude API error: {e}"
        except Exception as e:
            logger.exception(f"Streaming error: {e}")
            stream_error = "Internal server error during stream"

        if stream_error:
            # Ensure the UI doesn't get stuck in a "thinking" state.
            if sent_thinking_start and not sent_thinking_end:
                yield "data: {\"type\":\"thinking_end\"}\n\n"
            yield f"data: {json.dumps({'type':'error','error':stream_error})}\n\n"
            return

        # Parse auto-memory extraction
        import re
        memory_matches = re.finditer(r'<SAVE_MEMORY_PROFILE\s+user="([^"]+)">([\s\S]*?)</SAVE_MEMORY_PROFILE>', full_response)
        for match in memory_matches:
            target_user = match.group(1)
            mem_content = match.group(2).strip()
            memory_store.update_profile(target_user, mem_content)
            logger.info(f"Auto-saved memory profile for {target_user}")

        # Strip the memory tags from the final saved message so they don't pollute the chat history
        clean_response = re.sub(r'<SAVE_MEMORY_PROFILE\s+user="[^"]+">[\s\S]*?</SAVE_MEMORY_PROFILE>', '', full_response).strip()

        # Persist assistant reply + record usage
        cost = calculate_cost(model_tier, input_tokens, output_tokens)
        record_usage(task_type=task_type, model_tier=model_tier, model_name=model_used_for_call,
                     input_tokens=input_tokens, output_tokens=output_tokens,
                     cost=cost, user_id=user_id)
        conversation_store.add_message(conv_id, "assistant", clean_response, {
            "model_tier": model_tier, "model_used": model_used_for_call,
            "cost_usd": cost, "task_type": task_type,
        })

        updated_conv   = conversation_store.get_conversation(conv_id)
        updated_budget = check_budget_available()
        yield f"data: {json.dumps({'type':'done','model_tier':model_tier,'model_used':model_used_for_call,'cost_usd':cost,'task_type':task_type,'title':updated_conv['title'] if updated_conv else '','budget':{'spent':updated_budget['spent'],'remaining':updated_budget['remaining'],'limit':updated_budget['limit']}})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Prompt Optimizer ───────────────────────────────────────────────────────────
OPTIMIZER_SYSTEM = """You are an expert prompt engineer. Rewrite the user's prompt to make it clearer, more specific, and more effective for an AI assistant.

Rules:
- Keep the exact same intent and topic
- Add specificity: format, length, tone, structure, context where helpful
- Fix grammar and spelling
- For code requests: mention language, what it should do, edge cases
- For writing requests: mention audience, length, style
- Return ONLY the improved prompt — no explanation, no preamble
- Keep it concise (under 200 words)"""

@app.route("/api/optimize-prompt", methods=["POST"])
def optimize_prompt():
    """
    POST /api/optimize-prompt  body: {prompt, task_type?}
    Uses Claude Haiku to rewrite a rough prompt into a precise one.
    Cost: ~$0.00008 per call (Haiku, minimal tokens).
    """
    data      = request.get_json(silent=True) or {}
    prompt    = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400
    if len(prompt) > 2000:
        return jsonify({"error": "Prompt too long (max 2000 chars)"}), 400

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            system=OPTIMIZER_SYSTEM,
            messages=[{"role": "user", "content": f"Optimize this prompt:\n\n{prompt}"}],
        )
        optimized = (_anthropic_response_text(response) or "").strip()
        input_tokens  = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost          = calculate_cost("haiku", input_tokens, output_tokens)
        record_usage(
            task_type="general", model_tier="haiku", model_name="claude-haiku-4-5",
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost=cost, user_id=data.get("user_id", "anonymous"),
        )
        logger.info(f"Prompt optimized | {input_tokens}→{output_tokens} tokens | ${cost:.6f}")
        return jsonify({
            "success":   True,
            "original":  prompt,
            "optimized": optimized,
            "cost_usd":  cost,
            "tokens":    {"input": input_tokens, "output": output_tokens},
        })
    except anthropic.AuthenticationError:
        return jsonify({"error": "Invalid API key — check ANTHROPIC_API_KEY"}), 401
    except anthropic.RateLimitError:
        return jsonify({"error": "Rate limit — please retry in a moment"}), 429
    except anthropic.APIError as e:
        return jsonify({"error": f"Claude API error: {e}"}), 502
    except Exception as e:
        logger.exception(f"Optimize prompt error: {e}")
        return jsonify({"error": "Server error"}), 500


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """
    POST /api/upload   multipart: file=<file>
    Returns processed content ready to attach to a message.
    Images:    {type, filename, media_type, data (base64), size_bytes}
    Documents: {type, filename, content (str), pages?}
    """
    if 'file' not in request.files:
        return jsonify({"error": "No file field in request"}), 400
    f = request.files['file']
    if not f or f.filename == '':
        return jsonify({"error": "No file selected"}), 400

    file_bytes = f.read()
    MAX_SIZE   = 20 * 1024 * 1024  # 20 MB
    if len(file_bytes) > MAX_SIZE:
        return jsonify({"error": "File too large (max 20 MB)"}), 413

    result = file_processor.process_file(file_bytes, f.filename, f.content_type or '')
    if result['type'] == 'error':
        return jsonify({"error": result['error'], "filename": result['filename']}), 422

    logger.info(f"File uploaded: {f.filename} ({result['type']}, {len(file_bytes)} bytes)")
    return jsonify({"success": True, **result})


# ── Memory Routes ───────────────────────────────────────────────────────────
@app.route("/api/memory/<user_id>", methods=["GET"])
def get_memory(user_id):
    """GET /api/memory/<user_id>  — list all memories for a user"""
    mems = memory_store.get_memories(user_id)
    return jsonify({"memories": mems, "total": len(mems)})


@app.route("/api/memory/<user_id>", methods=["POST"])
def add_memory(user_id):
    """POST /api/memory/<user_id>  body: {content, source?}"""
    data    = request.get_json(silent=True) or {}
    content = data.get("content", "").strip()
    source  = data.get("source", "manual")
    if not content:
        return jsonify({"error": "content is required"}), 400
    mem = memory_store.add_memory(user_id, content, source)
    return jsonify({"success": True, "memory": mem}), 201


@app.route("/api/memory/<user_id>/<memory_id>", methods=["DELETE"])
def delete_memory(user_id, memory_id):
    """DELETE /api/memory/<user_id>/<memory_id>"""
    deleted = memory_store.delete_memory(user_id, memory_id)
    if not deleted:
        return jsonify({"error": "Memory not found"}), 404
    return jsonify({"success": True})


# ── Projects ──────────────────────────────────────────────────────────────────
@app.route("/api/projects", methods=["GET"])
def list_projects():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    return jsonify({"projects": project_store.get_projects(user_id)})

@app.route("/api/projects", methods=["POST"])
def create_project():
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    name = data.get("name", "New Project")
    custom_instructions = data.get("custom_instructions", "")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    p = project_store.create_project(user_id, name, custom_instructions)
    return jsonify(p), 201

@app.route("/api/projects/<project_id>", methods=["GET"])
def get_project(project_id):
    user_id = request.args.get("user_id")
    p = project_store.get_project(project_id, user_id)
    if not p: return jsonify({"error": "Not found"}), 404
    return jsonify(p)

@app.route("/api/projects/<project_id>", methods=["PATCH"])
def update_project(project_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    name = data.get("name")
    custom_instructions = data.get("custom_instructions")
    p = project_store.update_project(project_id, user_id, name, custom_instructions)
    if not p: return jsonify({"error": "Not found"}), 404
    return jsonify(p)

@app.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id") or request.args.get("user_id")
    if project_store.delete_project(project_id, user_id):
        return jsonify({"success": True})
    return jsonify({"error": "Not found"}), 404

@app.route("/api/projects/<project_id>/knowledge", methods=["POST"])
def add_project_knowledge(project_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    filename = data.get("filename", "untitled.txt")
    content = data.get("content", "")
    doc = project_store.add_knowledge_base_doc(project_id, user_id, filename, content)
    if not doc: return jsonify({"error": "Project not found"}), 404
    # Index for retrieval (non-fatal if FTS is unavailable)
    try:
        kb_retriever.index_doc(project_id, user_id, doc["id"], filename, content)
    except Exception:
        pass
    return jsonify(doc), 201

@app.route("/api/projects/<project_id>/knowledge/<doc_id>", methods=["DELETE"])
def delete_project_knowledge(project_id, doc_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id") or request.args.get("user_id")
    if project_store.delete_knowledge_base_doc(project_id, user_id, doc_id):
        try:
            kb_retriever.delete_doc_index(project_id, user_id, doc_id)
        except Exception:
            pass
        return jsonify({"success": True})
    return jsonify({"error": "Not found"}), 404


# ══════════════════════════════════════════════════════════════════════════════
# PROJECT TRACKER ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def _pt_conn():
    from db import get_connection
    return get_connection()

def _calc_progress(task: dict) -> int:
    """Return progress 0-100 based on task state events."""
    if task["status"] == "approved":
        return 100
    if task.get("submission_file"):
        return 80
    count = task.get("submission_count", 0)
    if count >= 2:
        return 60
    if count == 1:
        return 30
    if task.get("opened_at"):
        return 10
    return 0

def _is_admin(user_id: str) -> bool:
    return user_id in ("emp001", "emp003", "emp004")

def _task_row_to_dict(row) -> dict:
    keys = ["id","client_id","title","description","assigned_to","status",
            "progress","due_date","submission_note","submission_file",
            "rejection_note","submission_count","opened_at","created_at"]
    return dict(zip(keys, row))

def _client_row_to_dict(row) -> dict:
    keys = ["id","name","contact","requirements","deadline","status","created_at"]
    return dict(zip(keys, row))

# ── GET /api/projects ─────────────────────────────────────────────────────────
@app.route("/api/projects", methods=["GET"])
def get_projects():
    """All clients with their tasks. Admins see everything; others see only their tasks."""
    user_id = request.args.get("user_id", "")
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,name,contact,requirements,deadline,status,created_at FROM clients ORDER BY created_at DESC")
    clients = [_client_row_to_dict(r) for r in cur.fetchall()]
    
    for c in clients:
        cur.execute("""SELECT id,client_id,title,description,assigned_to,status,progress,
                       due_date,submission_note,submission_file,rejection_note,
                       submission_count,opened_at,created_at
                       FROM tasks WHERE client_id=? ORDER BY created_at""", (c["id"],))
        tasks = [_task_row_to_dict(r) for r in cur.fetchall()]
        # Recalculate progress
        for t in tasks:
            t["progress"] = _calc_progress(t)
        # Non-admins only see their own tasks
        if not _is_admin(user_id):
            tasks = [t for t in tasks if t["assigned_to"] == user_id]
        c["tasks"] = tasks
    conn.close()
    return jsonify({"clients": clients})

# ── POST /api/clients ─────────────────────────────────────────────────────────
@app.route("/api/clients", methods=["POST"])
def create_client():
    """Create a new client. Admin only."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    conn = _pt_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO clients (name,contact,requirements,deadline,status) VALUES (?,?,?,?,?)",
            (name, body.get("contact",""), body.get("requirements",""),
             body.get("deadline",""), body.get("status","active"))
        )
        client_id = cur.lastrowid
    conn.close()
    return jsonify({"success": True, "client_id": client_id}), 201

# ── GET /api/clients/<id>/tasks ───────────────────────────────────────────────
@app.route("/api/clients/<int:client_id>/tasks", methods=["GET"])
def get_client_tasks(client_id):
    user_id = request.args.get("user_id", "")
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("""SELECT id,client_id,title,description,assigned_to,status,progress,
                   due_date,submission_note,submission_file,rejection_note,
                   submission_count,opened_at,created_at
                   FROM tasks WHERE client_id=? ORDER BY created_at""", (client_id,))
    tasks = [_task_row_to_dict(r) for r in cur.fetchall()]
    for t in tasks:
        t["progress"] = _calc_progress(t)
        # Fetch dependencies
        cur.execute("SELECT depends_on_task_id FROM dependencies WHERE task_id=?", (t["id"],))
        t["depends_on"] = [r[0] for r in cur.fetchall()]
    conn.close()
    if not _is_admin(user_id):
        tasks = [t for t in tasks if t["assigned_to"] == user_id]
    return jsonify({"tasks": tasks})

# ── POST /api/tasks (create task) ─────────────────────────────────────────────
@app.route("/api/tasks", methods=["POST"])
def create_task():
    """Create a task for a client. Admin only."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    title = body.get("title", "").strip()
    client_id = body.get("client_id")
    if not title or not client_id:
        return jsonify({"error": "title and client_id are required"}), 400
    conn = _pt_conn()
    with conn:
        cur = conn.execute(
            """INSERT INTO tasks (client_id,title,description,assigned_to,due_date,status,progress)
               VALUES (?,?,?,?,?,'not_started',0)""",
            (client_id, title, body.get("description",""),
             body.get("assigned_to",""), body.get("due_date",""))
        )
        task_id = cur.lastrowid
        # Dependencies
        for dep_id in body.get("depends_on", []):
            conn.execute("INSERT OR IGNORE INTO dependencies VALUES (?,?)", (task_id, dep_id))
    conn.close()
    return jsonify({"success": True, "task_id": task_id}), 201

# ── POST /api/tasks/<id>/open ─────────────────────────────────────────────────
@app.route("/api/tasks/<int:task_id>/open", methods=["POST"])
def open_task(task_id):
    """Log when a team member first opens a task → 10% progress."""
    body = request.get_json(silent=True) or {}
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT opened_at, assigned_to FROM tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Task not found"}), 404
    if not row[0]:  # Only set once
        with conn:
            conn.execute("UPDATE tasks SET opened_at=?, status='in_progress', progress=10 WHERE id=?",
                         (datetime.utcnow().isoformat()+"Z", task_id))
    conn.close()
    return jsonify({"success": True, "progress": 10})

# ── POST /api/tasks/<id>/submit ───────────────────────────────────────────────
@app.route("/api/tasks/<int:task_id>/submit", methods=["POST"])
def submit_task(task_id):
    """Submit work for a task. Progress jumps to 30/60/80 based on real actions."""
    body = request.get_json(silent=True) or {}
    note     = body.get("note", "").strip()
    file_url = body.get("file_url", "").strip()

    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT submission_count, assigned_to FROM tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Task not found"}), 404

    # Validate file URL is actually reachable (#3)
    url_valid = False
    if file_url:
        try:
            import urllib.request
            req = urllib.request.Request(file_url, method="HEAD")
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=5) as resp:
                url_valid = resp.status < 400
        except Exception:
            url_valid = False  # Unreachable URL — don't credit the +20%

    new_count    = (row[0] or 0) + 1
    new_progress = 80 if url_valid else (60 if new_count >= 2 else 30)
    new_status   = "pending_review"

    with conn:
        conn.execute("""UPDATE tasks SET submission_note=?, submission_file=?,
                        submission_count=?, status=?, progress=?, rejection_note=NULL
                        WHERE id=?""",
                     (note, file_url or None, new_count, new_status, new_progress, task_id))
    conn.close()

    # Notify founder via email that a submission needs review (#4)
    _notify_email(
        subject=f"[Review Needed] Task submission #{new_count}",
        body=f"Task ID {task_id} was submitted.\n\nNote: {note}\n\nFile: {file_url or 'None'}"
    )
    return jsonify({"success": True, "progress": new_progress, "status": new_status, "url_verified": url_valid})

# ── POST /api/tasks/<id>/approve ──────────────────────────────────────────────
@app.route("/api/tasks/<int:task_id>/approve", methods=["POST"])
def approve_task(task_id):
    """Approve a task submission. Founder only. Unlocks dependent tasks."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    conn = _pt_conn()
    with conn:
        conn.execute("UPDATE tasks SET status='approved', progress=100 WHERE id=?", (task_id,))
        # Unlock dependent tasks: remove the blocker if all dependencies approved
        cur = conn.cursor()
        cur.execute("SELECT task_id FROM dependencies WHERE depends_on_task_id=?", (task_id,))
        unblocked = []
        for (dependent_id,) in cur.fetchall():
            cur2 = conn.cursor()
            cur2.execute("""SELECT COUNT(*) FROM dependencies d
                            JOIN tasks t ON d.depends_on_task_id = t.id
                            WHERE d.task_id=? AND t.status != 'approved'""", (dependent_id,))
            remaining_blockers = cur2.fetchone()[0]
            if remaining_blockers == 0:
                conn.execute("UPDATE tasks SET status='unlocked' WHERE id=? AND status='not_started'",
                             (dependent_id,))
                unblocked.append(dependent_id)
    conn.close()
    return jsonify({"success": True, "unblocked_tasks": unblocked})

# ── POST /api/tasks/<id>/reject ───────────────────────────────────────────────
@app.route("/api/tasks/<int:task_id>/reject", methods=["POST"])
def reject_task(task_id):
    """Reject a submission and send it back with notes. Founder only."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    rejection_note = body.get("note", "Needs revision.").strip()

    conn = _pt_conn()
    with conn:
        conn.execute("""UPDATE tasks SET status='rejected', rejection_note=?,
                        submission_file=NULL WHERE id=?""",
                     (rejection_note, task_id))
    conn.close()
    return jsonify({"success": True})

# ── GET /api/dashboard/founder ────────────────────────────────────────────────
@app.route("/api/dashboard/founder", methods=["GET"])
def founder_dashboard():
    """Full founder view — all clients, tasks, and blockers."""
    user_id = request.args.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,name,contact,requirements,deadline,status,created_at FROM clients ORDER BY created_at DESC")
    clients = [_client_row_to_dict(r) for r in cur.fetchall()]

    pending_review = []
    blocked_tasks  = []
    overdue_tasks  = []
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    for c in clients:
        cur.execute("""SELECT id,client_id,title,description,assigned_to,status,progress,
                       due_date,submission_note,submission_file,rejection_note,
                       submission_count,opened_at,created_at
                       FROM tasks WHERE client_id=?""", (c["id"],))
        tasks = [_task_row_to_dict(r) for r in cur.fetchall()]
        for t in tasks:
            t["progress"] = _calc_progress(t)
            t["client_name"] = c["name"]
            cur2 = conn.cursor()
            cur2.execute("SELECT depends_on_task_id FROM dependencies WHERE task_id=?", (t["id"],))
            t["depends_on"] = [r[0] for r in cur2.fetchall()]
            if t["status"] == "pending_review":
                pending_review.append(t)
            if t["depends_on"] and t["status"] == "not_started":
                blocked_tasks.append(t)
            if t.get("due_date") and t["due_date"] < today_str and t["status"] not in ("approved",):
                overdue_tasks.append(t)
        c["tasks"] = tasks
    conn.close()

    return jsonify({
        "clients": clients,
        "summary": {
            "pending_review": pending_review,
            "blocked_tasks": blocked_tasks,
            "overdue_tasks": overdue_tasks,
        }
    })

# ── Service → Default Task Templates ─────────────────────────────────────────
# Maps service type checkboxes → auto-generated tasks with assignees + dependencies
SERVICE_TASK_TEMPLATES = {
    "content": [
        {"title": "Content Brief & Research",     "assigned_to": "emp006", "order": 1},
        {"title": "Write Copy / Content Draft",    "assigned_to": "emp006", "order": 2},
        {"title": "Content Review & Approval",     "assigned_to": "emp004", "order": 3},
    ],
    "video": [
        {"title": "Video Script Writing",          "assigned_to": "emp006", "order": 4},
        {"title": "Video Shoot / Production",      "assigned_to": "emp005", "order": 5},
        {"title": "Video Editing & Post",          "assigned_to": "emp005", "order": 6},
        {"title": "AI Video Enhancements",         "assigned_to": "emp008", "order": 7},
    ],
    "design": [
        {"title": "Design Brief & Moodboard",      "assigned_to": "emp002", "order": 8},
        {"title": "UI/UX Design — Wireframes",     "assigned_to": "emp002", "order": 9},
        {"title": "Final Design Handoff",          "assigned_to": "emp002", "order": 10},
    ],
    "website": [
        {"title": "Website Architecture Plan",     "assigned_to": "emp001", "order": 11},
        {"title": "Frontend Development",          "assigned_to": "emp003", "order": 12},
        {"title": "Backend / Integrations",        "assigned_to": "emp001", "order": 13},
        {"title": "QA & Launch",                   "assigned_to": "emp003", "order": 14},
    ],
    "social": [
        {"title": "Social Media Strategy",         "assigned_to": "emp006", "order": 15},
        {"title": "Content Calendar",              "assigned_to": "emp006", "order": 16},
        {"title": "Graphics & Templates",          "assigned_to": "emp002", "order": 17},
    ],
    "accounts": [
        {"title": "Invoice & Payment Setup",       "assigned_to": "emp007", "order": 18},
        {"title": "Monthly Reporting",             "assigned_to": "emp007", "order": 19},
    ],
}

# ── POST /api/clients/<id>/auto-tasks ─────────────────────────────────────────
@app.route("/api/clients/<int:client_id>/auto-tasks", methods=["POST"])
def auto_generate_tasks(client_id):
    """Auto-generate tasks from selected service types. Admin only."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    services = body.get("services", [])  # e.g. ["content","video","website"]
    due_date = body.get("due_date", "")
    if not services:
        return jsonify({"error": "No services selected"}), 400

    conn = _pt_conn()
    created_ids = []
    ordered_tasks = []
    for svc in services:
        templates = SERVICE_TASK_TEMPLATES.get(svc, [])
        ordered_tasks.extend(templates)

    # Sort by order field so dependencies chain correctly
    ordered_tasks.sort(key=lambda x: x["order"])

    with conn:
        for tmpl in ordered_tasks:
            cur = conn.execute(
                """INSERT INTO tasks (client_id,title,assigned_to,due_date,status,progress)
                   VALUES (?,?,?,?,'not_started',0)""",
                (client_id, tmpl["title"], tmpl["assigned_to"], due_date)
            )
            created_ids.append(cur.lastrowid)

        # Wire sequential dependencies (each task depends on the one before it within service)
        for svc in services:
            svc_tasks = [t for t in ordered_tasks if t in SERVICE_TASK_TEMPLATES.get(svc, [])]
            svc_ids = []
            for tmpl in SERVICE_TASK_TEMPLATES.get(svc, []):
                idx = ordered_tasks.index(tmpl) if tmpl in ordered_tasks else -1
                if idx >= 0:
                    svc_ids.append(created_ids[idx])
            for i in range(1, len(svc_ids)):
                conn.execute("INSERT OR IGNORE INTO dependencies VALUES (?,?)",
                             (svc_ids[i], svc_ids[i-1]))

    conn.close()
    return jsonify({"success": True, "tasks_created": len(created_ids), "task_ids": created_ids})

# ── GET /api/blockers ─────────────────────────────────────────────────────────
@app.route("/api/blockers", methods=["GET"])
def get_blockers():
    """Get the full dependency blocker chain across all active tasks."""
    user_id = request.args.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    conn = _pt_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT t.id, t.title, t.assigned_to, t.status, t.due_date,
               c.name as client_name
        FROM tasks t
        JOIN clients c ON t.client_id = c.id
        WHERE t.status NOT IN ('approved')
    """)
    all_tasks = {r[0]: {"id":r[0],"title":r[1],"assigned_to":r[2],
                         "status":r[3],"due_date":r[4],"client_name":r[5]} for r in cur.fetchall()}

    cur.execute("SELECT task_id, depends_on_task_id FROM dependencies")
    deps = cur.fetchall()
    conn.close()

    EMP_NAMES = {"emp001":"Vidit","emp002":"Nupur","emp003":"Abhinav",
                 "emp004":"Kshitij","emp005":"Raj","emp006":"Mohit",
                 "emp007":"Tanaya","emp008":"Happy"}

    blockers = []
    for (task_id, blocker_id) in deps:
        if blocker_id not in all_tasks or task_id not in all_tasks:
            continue
        blocker = all_tasks[blocker_id]
        blocked  = all_tasks[task_id]
        if blocker["status"] not in ("approved",):
            days_overdue = 0
            if blocker.get("due_date") and blocker["due_date"] < today_str:
                from datetime import date
                d1 = date.fromisoformat(blocker["due_date"])
                days_overdue = (date.today() - d1).days
            blockers.append({
                "blocker_task": {**blocker, "name": EMP_NAMES.get(blocker["assigned_to"], blocker["assigned_to"])},
                "blocked_task": {**blocked, "name": EMP_NAMES.get(blocked["assigned_to"], blocked["assigned_to"])},
                "days_overdue": days_overdue,
            })

    blockers.sort(key=lambda x: x["days_overdue"], reverse=True)
    return jsonify({"blockers": blockers})

# ── POST /api/tasks/<id>/done ─────────────────────────────────────────────────
@app.route("/api/tasks/<int:task_id>/done", methods=["POST"])
def quick_done(task_id):
    """Quick 'Mark Done' — sets task to pending_review at 80%."""
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM tasks WHERE id=?", (task_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({"error": "Task not found"}), 404
    with conn:
        conn.execute("UPDATE tasks SET status='pending_review', progress=80 WHERE id=?", (task_id,))
    conn.close()
    return jsonify({"success": True})

# ══════════════════════════════════════════════════════════════════════════════
# DAILY STANDUP ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def _su_conn():
    from db import get_connection
    return get_connection()

# ── POST /api/standup ─────────────────────────────────────────────────────────
@app.route("/api/standup", methods=["POST"])
def submit_standup():
    """Submit daily standup. One per user per day."""
    body = request.get_json(silent=True) or {}
    user_id  = body.get("user_id", "").strip()
    yesterday = body.get("yesterday", "").strip()
    today_txt = body.get("today", "").strip()
    blockers  = body.get("blockers", "").strip()

    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    if not yesterday and not today_txt:
        return jsonify({"error": "Provide at least one update field"}), 400

    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    conn = _su_conn()
    try:
        with conn:
            conn.execute("""
                INSERT INTO standups (user_id, date, yesterday, today, blockers, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, date) DO UPDATE SET
                    yesterday=excluded.yesterday,
                    today=excluded.today,
                    blockers=excluded.blockers,
                    submitted_at=excluded.submitted_at
            """, (user_id, date_str, yesterday, today_txt, blockers, datetime.utcnow().isoformat()+"Z"))
    finally:
        conn.close()
    return jsonify({"success": True, "date": date_str})

# ── GET /api/standup/today ────────────────────────────────────────────────────
@app.route("/api/standup/today", methods=["GET"])
def get_standups_today():
    """Get all standups submitted today. Founder view."""
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    user_id  = request.args.get("user_id", "")
    conn = _su_conn()
    cur = conn.cursor()
    if user_id:
        cur.execute("SELECT user_id,date,yesterday,today,blockers,submitted_at FROM standups WHERE date=? AND user_id=?", (date_str, user_id))
    else:
        cur.execute("SELECT user_id,date,yesterday,today,blockers,submitted_at FROM standups WHERE date=? ORDER BY submitted_at", (date_str,))
    rows = cur.fetchall()
    conn.close()
    EMP_NAMES = {"emp001":"Vidit","emp002":"Nupur","emp003":"Abhinav",
                 "emp004":"Kshitij","emp005":"Raj","emp006":"Mohit",
                 "emp007":"Tanaya","emp008":"Happy"}
    standups = [{"user_id":r[0],"name":EMP_NAMES.get(r[0],r[0]),"date":r[1],
                 "yesterday":r[2],"today":r[3],"blockers":r[4],"submitted_at":r[5]} for r in rows]
    return jsonify({"standups": standups, "date": date_str})

# ── GET /api/standup/history ──────────────────────────────────────────────────
@app.route("/api/standup/history", methods=["GET"])
def get_standup_history():
    """Get standup history for a user (last 7 days)."""
    user_id = request.args.get("user_id", "")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("""SELECT user_id,date,yesterday,today,blockers,submitted_at
                   FROM standups WHERE user_id=? ORDER BY date DESC LIMIT 7""", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return jsonify({"history": [{"date":r[1],"yesterday":r[2],"today":r[3],"blockers":r[4],"submitted_at":r[5]} for r in rows]})

# ── GET /api/alerts ───────────────────────────────────────────────────────────
@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    """Return AT_RISK and CRITICAL tasks for founder dashboard."""
    user_id = request.args.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    alerts = task_scheduler.get_all_alerts()
    return jsonify({"alerts": alerts})

# ── POST /api/alerts/run-check ────────────────────────────────────────────────
@app.route("/api/alerts/run-check", methods=["POST"])
def run_alert_check():
    """Manually trigger the overdue check. Admin only."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    fired = task_scheduler.check_overdue_tasks()
    return jsonify({"success": True, "alerts_fired": len(fired), "details": fired})

# ══════════════════════════════════════════════════════════════════════════════
# NOTION INTEGRATION ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/notion/status", methods=["GET"])
def notion_status():
    """Returns whether Notion is configured and reachable."""
    configured = notion_store.is_configured()
    return jsonify({
        "configured": configured,
        "message": "Notion is connected" if configured else (
            "Set NOTION_TOKEN, NOTION_CLIENTS_DB_ID, NOTION_TASKS_DB_ID in config/.env"
        ),
    })


@app.route("/api/notion/clients", methods=["GET"])
def notion_list_clients():
    """
    List all clients from Notion.
    Query params: status (active|completed|paused)
    """
    status_filter = request.args.get("status", "")
    clients = notion_store.list_clients(status_filter=status_filter)
    return jsonify({"clients": clients, "count": len(clients)})


@app.route("/api/notion/clients", methods=["POST"])
def notion_create_client():
    """
    Create a new client in Notion AND auto-generate tasks.
    Body: { name, contact?, requirements?, deadline?, budget?, notes?, services[] }
    """
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    if not notion_store.is_configured():
        return jsonify({"error": "Notion is not configured. Add NOTION_TOKEN and DB IDs to config/.env"}), 503

    # 1. Create client page in Notion
    client = notion_store.create_client(
        name=name,
        contact=body.get("contact", ""),
        requirements=body.get("requirements", ""),
        deadline=body.get("deadline", ""),
        budget=body.get("budget", ""),
        notes=body.get("notes", ""),
        status="active",
    )
    if not client:
        return jsonify({"error": "Failed to create client in Notion"}), 500

    # 2. Create tasks — use custom list from frontend if provided, else auto-generate from services
    EMP_NAMES = {
        "emp001": "Vidit", "emp002": "Nupur", "emp003": "Abhinav",
        "emp004": "Kshitij", "emp005": "Raj", "emp006": "Mohit",
        "emp007": "Tanaya", "emp008": "Happy",
    }
    SVC_TASKS = {
        "content":  [("Content Brief & Research", "emp006"), ("Write Copy / Content Draft", "emp006"), ("Content Review & Approval", "emp004")],
        "video":    [("Video Script Writing", "emp006"), ("Video Shoot / Production", "emp005"), ("Video Editing & Post", "emp005"), ("AI Video Enhancements", "emp008")],
        "design":   [("Design Brief & Moodboard", "emp002"), ("UI/UX Design — Wireframes", "emp002"), ("Final Design Handoff", "emp002")],
        "website":  [("Website Architecture Plan", "emp001"), ("Frontend Development", "emp003"), ("Backend / Integrations", "emp001"), ("QA & Launch", "emp003")],
        "social":   [("Social Media Strategy", "emp006"), ("Content Calendar", "emp006"), ("Graphics & Templates", "emp002")],
        "accounts": [("Invoice & Payment Setup", "emp007"), ("Monthly Reporting", "emp007")],
    }

    deadline = body.get("deadline", "")
    tasks_created = 0

    # Frontend sends edited `tasks` array → use it directly
    custom_tasks = body.get("tasks", [])
    if custom_tasks:
        for t in custom_tasks:
            task_title = t.get("title", "").strip()
            emp_id     = t.get("who", "emp001")
            if not task_title:
                continue
            emp_name = EMP_NAMES.get(emp_id, emp_id)
            # Guess service from emp_id for categorisation
            svc_map = {"emp001":"website","emp002":"design","emp003":"website",
                       "emp004":"accounts","emp005":"video","emp006":"content",
                       "emp007":"accounts","emp008":"video"}
            result = notion_store.create_task(
                title=task_title,
                client_name=name,
                client_notion_id=client["notion_id"],
                assigned_to=emp_name,
                due_date=deadline,
                status="not_started",
                service=svc_map.get(emp_id, "general"),
            )
            if result:
                tasks_created += 1
    else:
        # Fallback: auto-generate from services[]
        for svc in body.get("services", []):
            for (task_title, emp_id) in SVC_TASKS.get(svc, []):
                result = notion_store.create_task(
                    title=task_title,
                    client_name=name,
                    client_notion_id=client["notion_id"],
                    assigned_to=EMP_NAMES.get(emp_id, emp_id),
                    due_date=deadline,
                    status="not_started",
                    service=svc,
                )
                if result:
                    tasks_created += 1

    logger.info(f"Notion: onboarded client '{name}' with {tasks_created} tasks")
    return jsonify({
        "success": True,
        "notion_id": client["notion_id"],
        "name": name,
        "tasks_created": tasks_created,
        "notion_url": f"https://notion.so/{client['notion_id'].replace('-', '')}",
    })


@app.route("/api/notion/tasks", methods=["GET"])
def notion_list_tasks():
    """
    List tasks from Notion.
    Query params: assigned_to (employee name), client_id (notion page id), status
    """
    assigned_to      = request.args.get("assigned_to", "")
    client_notion_id = request.args.get("client_id", "")
    status_filter    = request.args.get("status", "")

    tasks = notion_store.list_tasks(
        assigned_to=assigned_to,
        client_notion_id=client_notion_id,
        status_filter=status_filter,
    )
    return jsonify({"tasks": tasks, "count": len(tasks)})


@app.route("/api/notion/tasks/<string:notion_id>", methods=["PATCH"])
def notion_update_task(notion_id: str):
    """
    Update a task's status, progress, and/or submission note in Notion.
    Body: { status?, progress?, submission_note? }
    """
    body   = request.get_json(silent=True) or {}
    result = notion_store.update_task(
        notion_id=notion_id,
        status=body.get("status"),
        progress=body.get("progress"),
        submission_note=body.get("submission_note"),
    )
    if result:
        return jsonify({"success": True})
    return jsonify({"error": "Failed to update task in Notion"}), 500


@app.route("/api/notion/dashboard", methods=["GET"])
def notion_dashboard():
    """
    Returns all clients with their tasks for the project board.
    Powers projects.html when Notion mode is enabled.
    """
    data = notion_store.get_dashboard_data()
    return jsonify(data)


# ── Document Export ───────────────────────────────────────────────────────────

@app.route("/api/export", methods=["POST"])
def export_document():
    """
    Convert AI-generated markdown to a downloadable DOCX, PDF, or PPTX file.
    Body: { content: str, format: "docx"|"pdf"|"pptx", title: str }
    """
    from flask import send_file
    from document_exporter import export_docx, export_pdf, export_pptx

    body   = request.get_json(silent=True) or {}
    content = body.get("content", "").strip()
    fmt     = body.get("format", "pdf").lower()
    title   = body.get("title", "Claude Export")[:120]

    if not content:
        return jsonify({"error": "No content provided"}), 400
    if fmt not in ("docx", "pdf", "pptx"):
        return jsonify({"error": "Unsupported format. Use docx, pdf, or pptx."}), 400

    try:
        if fmt == "docx":
            buf      = export_docx(content, title=title)
            mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ext      = "docx"
        elif fmt == "pdf":
            buf      = export_pdf(content, title=title)
            mimetype = "application/pdf"
            ext      = "pdf"
        else:  # pptx
            buf      = export_pptx(content, title=title)
            mimetype = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ext      = "pptx"

        safe_name = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_") or "export"
        return send_file(
            buf,
            mimetype=mimetype,
            as_attachment=True,
            download_name=f"{safe_name}.{ext}"
        )

    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.exception("Export failed")
        return jsonify({"error": f"Export failed: {str(e)}"}), 500


# ── Entry Point ───────────────────────────────────────────────────────────────


if __name__ == "__main__":

    port  = int(os.getenv("FLASK_PORT", 5000))
    debug = os.getenv("FLASK_ENV", "development") == "development"
    logger.info(f"Starting Claude Office Assistant API on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
