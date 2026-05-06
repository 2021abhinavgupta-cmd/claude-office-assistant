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
import conversation_store
import memory_store
import file_processor
import project_store

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
        "You are an expert software engineer. Write clean, well-commented, "
        "production-ready code. Always explain your approach briefly before the code block."
    ),
    "html_design": (
        "You are a senior UI/UX designer and front-end developer. "
        "Generate beautiful, responsive, self-contained HTML/CSS/JS in a SINGLE file. "
        "Use glassmorphism, gradients, Google Fonts, and smooth animations. "
        "Include ALL CSS inside <style> and ALL JS inside <script> tags. "
        "Return ONLY the complete HTML document, nothing else."
    ),
    "presentations": (
        "You are a professional presentation strategist. Create clear, engaging slide content. "
        "Format each slide as:\n## SLIDE N: Title\n**Key Point 1**\n**Key Point 2**\n**Key Point 3**\n"
        "Include a title slide and summary slide. Use compelling headlines and concise bullet points."
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
        "You are Claude, an expert AI office assistant. Help the user with any task "
        "they need — analysis, writing, research, planning, brainstorming, math, or anything else. "
        "Be concise, accurate, and genuinely helpful. Ask clarifying questions when needed."
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



def _get_user_tone_profile(user_id: str) -> Optional[str]:
    try:
        with open(EMPLOYEES_DB, "r") as f:
            data = json.load(f)
            for emp in data.get("employees", []):
                if emp.get("id") == user_id:
                    return emp.get("tone_profile")
    except Exception:
        pass
    return None

def _get_all_users_str() -> str:
    try:
        with open(EMPLOYEES_DB, "r") as f:
            data = json.load(f)
            return ", ".join([e.get("id") for e in data.get("employees", []) if e.get("id")])
    except Exception:
        return "api"

def _get_team_context() -> str:
    """Returns a directory of all team members and their default tone profiles."""
    try:
        with open(EMPLOYEES_DB, "r") as f:
            data = json.load(f)
            lines = []
            for emp in data.get("employees", []):
                role = emp.get('role', 'Staff')
                tone = emp.get('tone_profile', 'Standard professional')
                lines.append(f"- {emp.get('name')} ({role}): {tone}")
            return "\n".join(lines)
    except Exception:
        pass
    return ""

def _build_system_prompt(task_type: str, user_id: str, project_id: Optional[str] = None) -> list:
    base_prompt = SYSTEM_PROMPTS.get(task_type.lower().replace(" ", "_"), DEFAULT_SYSTEM)
    base_prompt += "\n\nBe concise and direct. No unnecessary preamble. No phrases like 'Certainly!' or 'Great question!'. Get to the answer immediately."
    
    # Auto-memory extraction instruction
    users_str = _get_all_users_str()
    base_prompt += f"\n\n## AUTO-MEMORY EXTRACTION\nIf the user tells you a preference, rule, or fact about themselves or another user that you should remember for future conversations, output the following EXACT XML block at the very end of your response:\n<SAVE_MEMORY user=\"USER_ID\">The memory content</SAVE_MEMORY>\n\nValid USER_IDs you can assign memories to:\n{users_str}\n(If saving for the current user, use {user_id})"

    tone_profile = _get_user_tone_profile(user_id)
    if tone_profile:
        base_prompt += f"\n\nCRITICAL OUTPUT STYLE REQUIREMENT: You MUST strictly adhere to the following writing style and tone for this user: {tone_profile}"
    
    mem_ctx = memory_store.format_for_prompt(user_id)
    team_mem_ctx = memory_store.format_team_memories()
    team_directory = _get_team_context()
    
    sections = [base_prompt]
    if mem_ctx:
        sections.append(mem_ctx)
        
    if team_directory or team_mem_ctx:
        team_section = "## TEAM DIRECTORY & SHARED STYLES\n(If the user asks you to write like someone else on the team, use the profiles and memories below.)"
        if team_directory:
            team_section += f"\n\n### Team Tone Profiles:\n{team_directory}"
        if team_mem_ctx:
            team_section += f"\n{team_mem_ctx}"
        sections.append(team_section)
    
    if project_id:
        project = project_store.get_project(project_id, user_id)
        if project:
            if project.get("custom_instructions"):
                sections.append(f"## Custom Instructions\n{project['custom_instructions']}")
            if project.get("knowledge_base"):
                kb_text = "\n\n---\n\n".join(
                    f"### {doc['filename']}\n{doc['content']}"
                    for doc in project["knowledge_base"]
                )
                sections.append(f"## Project Knowledge Base\nThe following documents have been provided for context:\n\n{kb_text}")
    
    final_text = "\n\n".join(sections)
    return [
        {
            "type": "text",
            "text": final_text,
            "cache_control": {"type": "ephemeral"}
        }
    ]

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
    system_prompt = _build_system_prompt(task_type, user_id, project_id)

    logger.info(f"Claude call | task={task_type} | model={model_tier} | user={user_id}")

    try:
        response = client.messages.create(
            model=model_name,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": message}],
        )
        output_text   = response.content[0].text
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
    budget = get_usage_summary()
    return jsonify({
        "status": "ok",
        "service": "Claude Office Assistant API",
        "budget_remaining": budget["remaining"],
        "budget_percent_used": budget["percent_used"],
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
    """Returns current month budget usage."""
    return jsonify(get_usage_summary())


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
    style_hints = data.get("style_hints", "modern, dark mode, glassmorphism").strip()
    user_id     = data.get("user_id", "anonymous")

    if not description:
        return jsonify({"error": "description is required"}), 400

    prompt = (
        f"Create a complete, beautiful, self-contained HTML page for: {description}\n\n"
        f"Style requirements: {style_hints}\n\n"
        "Requirements:\n"
        "- Single HTML file with embedded CSS and JS\n"
        "- Mobile responsive\n"
        "- Modern design with animations\n"
        "- No external dependencies except Google Fonts\n"
        "- Return ONLY the HTML code, starting with <!DOCTYPE html>"
    )

    result = call_claude("html_design", prompt, user_id, max_tokens=4096)
    if not result["success"]:
        return jsonify({"error": result.get("error", "Generation failed")}), 500

    html_code = result["response"]
    # Strip markdown code fences if Claude wrapped the output (handles ```html, ```xml, etc.)
    if html_code.strip().startswith("```"):
        lines = html_code.strip().split("\n")
        # Remove first line (the fence + optional lang tag) and last line (closing fence)
        inner = lines[1:] if len(lines) > 1 else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        html_code = "\n".join(inner).strip()

    return jsonify({
        **result,
        "html_code": html_code,
        "preview_endpoint": "/api/html/preview",
    })


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
        "Format each slide EXACTLY as:\n"
        "## SLIDE N: [Title]\n"
        "- [Bullet 1]\n"
        "- [Bullet 2]\n"
        "- [Bullet 3]\n"
        "[NOTES: Speaker notes here]\n\n"
        f"Include: Title slide (#1), {slide_count-2} content slides, Summary/CTA slide (#{slide_count})."
    )

    result = call_claude("presentations", prompt, user_id, max_tokens=4096)
    if not result["success"]:
        return jsonify({"error": result.get("error", "Generation failed")}), 500

    # Parse slides from markdown
    slides = _parse_slides(result["response"])

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
    blocks = re.split(r"(?=## SLIDE \d+:)", markdown)
    for block in blocks:
        if not block.strip():
            continue
        lines   = block.strip().split("\n")
        header  = lines[0]
        title_m = re.match(r"## SLIDE \d+:\s*(.+)", header)
        title   = title_m.group(1).strip() if title_m else header.strip()

        bullets = []
        notes   = ""
        for line in lines[1:]:
            if line.startswith("[NOTES:"):
                notes = line.replace("[NOTES:", "").rstrip("]").strip()
            elif line.startswith("- "):
                bullets.append(line[2:].strip())

        slides.append({"title": title, "bullets": bullets, "notes": notes})
    return slides


# ── Employee Auth & Attendance ────────────────────────────────────────────────
@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    try:
        body = request.get_json(silent=True) or {}
        user_id = body.get("user_id")
        pin = body.get("pin")
        
        data = _load_employees()
        found = None
        for emp in data.get("employees", []):
            if emp.get("id") == user_id:
                found = emp
                break
                
        if not found:
            return jsonify({"error": "Employee not found"}), 404
            
        if str(found.get("pin")) != str(pin):
            return jsonify({"error": "Invalid PIN"}), 401
            
        # Log punch-in
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, action TEXT, timestamp TEXT)")
            conn.execute("INSERT INTO attendance (user_id, action, timestamp) VALUES (?, ?, ?)",
                         (user_id, "in", datetime.utcnow().isoformat() + "Z"))
        conn.close()
        
        return jsonify({"success": True})
    except Exception as e:
        import traceback
        return jsonify({"error": "Server error", "details": str(e), "trace": traceback.format_exc()}), 500

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    try:
        body = request.get_json(silent=True) or {}
        user_id = body.get("user_id")
        
        if user_id:
            from db import get_connection
            conn = get_connection()
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, action TEXT, timestamp TEXT)")
                conn.execute("INSERT INTO attendance (user_id, action, timestamp) VALUES (?, ?, ?)",
                             (user_id, "out", datetime.utcnow().isoformat() + "Z"))
            conn.close()
        
        return jsonify({"success": True})
    except Exception as e:
        import traceback
        return jsonify({"error": "Server error", "details": str(e), "trace": traceback.format_exc()}), 500

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
    # Only Abhinav (emp003) and Kshitij (emp004) are admins
    if admin_id not in ["emp003", "emp004"]:
        return jsonify({"error": "Unauthorized"}), 403
        
    from db import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, action, timestamp FROM attendance ORDER BY timestamp DESC LIMIT 500")
    logs = [{"id": r[0], "user_id": r[1], "action": r[2], "timestamp": r[3]} for r in cursor.fetchall()]
    conn.close()
    
    return jsonify({"logs": logs})

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
    Body: { whatsapp, action: 'in'|'out', notes? }
    """
    body     = request.get_json(silent=True) or {}
    whatsapp = body.get("whatsapp", "").strip()
    action   = body.get("action", "in").strip()
    notes    = body.get("notes", "")

    if not whatsapp:
        return jsonify({"error": "whatsapp number required"}), 400

    data = _load_employees()
    found = None
    for emp in data["employees"]:
        if emp.get("whatsapp") == whatsapp:
            found = emp
            break

    if not found:
        return jsonify({"error": "Employee not found", "whatsapp": whatsapp}), 404

    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "action":    action,
        "notes":     notes,
    }
    found.setdefault("checkins", []).append(entry)
    found["last_action"] = action
    found["last_seen"]   = entry["timestamp"]

    _save_employees(data)
    logger.info(f"Employee {found['name']} checked {action} at {entry['timestamp']}")

    return jsonify({
        "success":  True,
        "employee": found["name"],
        "action":   action,
        "time":     entry["timestamp"],
    })


@app.route("/api/employees/summary", methods=["GET"])
def employee_summary():
    """Returns today's attendance summary."""
    data  = _load_employees()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    summary = []
    for emp in data["employees"]:
        today_checks = [
            c for c in emp.get("checkins", [])
            if c["timestamp"].startswith(today)
        ]
        summary.append({
            "name":       emp["name"],
            "role":       emp["role"],
            "department": emp["department"],
            "status":     emp.get("last_action", "not checked in"),
            "last_seen":  emp.get("last_seen", "—"),
            "today_logs": today_checks,
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
    system_prompt = _build_system_prompt(task_type, user_id, project_id)

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
                # Append document text inside the user message
                content_blocks[0]["text"] += (
                    f"\n\n---\nAttached file: {att['filename']}\n"
                    f"{att.get('content', '')}\n---"
                )
        api_messages = api_messages[:-1] + [{"role": "user", "content": content_blocks}]

    try:
        response      = client.messages.create(
            model=model_name, max_tokens=MAX_TOKENS,
            system=system_prompt, messages=api_messages,
        )
        output_text   = response.content[0].text
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
        model_tier = "sonnet" if "sonnet" in model_name.lower() else "haiku"
    else:
        model_config = get_model_for_task(task_type)
        model_name   = model_config["name"]
        model_tier   = model_config["tier"]
    system_prompt = _build_system_prompt(task_type, user_id, conv.get("project_id"))

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

    def generate():
        full_response  = ""
        input_tokens   = 0
        output_tokens  = 0
        stream_error   = None

        logger.info(f"Stream | task={task_type} | model={model_tier} | user={user_id}")

        try:
            with client.messages.stream(
                model=model_name, max_tokens=MAX_TOKENS,
                system=system_prompt, messages=api_messages,
            ) as stream:
                for text_chunk in stream.text_stream:
                    full_response += text_chunk
                    yield f"data: {json.dumps({'type':'text','text':text_chunk})}\n\n"

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
            yield f"data: {json.dumps({'type':'error','error':stream_error})}\n\n"
            return

        # Parse auto-memory extraction
        import re
        memory_matches = re.finditer(r'<SAVE_MEMORY\s+user="([^"]+)">([\s\S]*?)</SAVE_MEMORY>', full_response)
        for match in memory_matches:
            target_user = match.group(1)
            mem_content = match.group(2).strip()
            memory_store.add_memory(target_user, mem_content, source="auto")
            logger.info(f"Auto-saved memory for {target_user}: {mem_content}")

        # Strip the memory tags from the final saved message so they don't pollute the chat history
        clean_response = re.sub(r'<SAVE_MEMORY\s+user="[^"]+">[\s\S]*?</SAVE_MEMORY>', '', full_response).strip()

        # Persist assistant reply + record usage
        cost = calculate_cost(model_tier, input_tokens, output_tokens)
        record_usage(task_type=task_type, model_tier=model_tier, model_name=model_name,
                     input_tokens=input_tokens, output_tokens=output_tokens,
                     cost=cost, user_id=user_id)
        conversation_store.add_message(conv_id, "assistant", clean_response, {
            "model_tier": model_tier, "model_used": model_name,
            "cost_usd": cost, "task_type": task_type,
        })

        updated_conv   = conversation_store.get_conversation(conv_id)
        updated_budget = check_budget_available()
        yield f"data: {json.dumps({'type':'done','model_tier':model_tier,'model_used':model_name,'cost_usd':cost,'task_type':task_type,'title':updated_conv['title'] if updated_conv else '','budget':{'spent':updated_budget['spent'],'remaining':updated_budget['remaining'],'limit':updated_budget['limit']}})}\n\n"

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
        optimized     = response.content[0].text.strip()
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
    return jsonify(doc), 201

@app.route("/api/projects/<project_id>/knowledge/<doc_id>", methods=["DELETE"])
def delete_project_knowledge(project_id, doc_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id") or request.args.get("user_id")
    if project_store.delete_knowledge_base_doc(project_id, user_id, doc_id):
        return jsonify({"success": True})
    return jsonify({"error": "Not found"}), 404


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port  = int(os.getenv("FLASK_PORT", 5000))
    debug = os.getenv("FLASK_ENV", "development") == "development"
    logger.info(f"Starting Claude Office Assistant API on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
