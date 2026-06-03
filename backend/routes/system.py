"""
System Blueprint
Routes: /api/health, /api/budget, /api/usage, /api/usage/export, /api/web-search, /api/routes
"""
import csv
import json
import logging
import os
import re
from datetime import datetime
from io import StringIO
from pathlib import Path

import requests
from flask import Blueprint, Response, jsonify, request, send_file

from budget_tracker import (check_budget_available, get_all_usage_logs,
                             get_usage_summary)
from model_router import get_all_routes

logger = logging.getLogger(__name__)
system_bp = Blueprint("system", __name__)


# ── DuckDuckGo helpers ────────────────────────────────────────────────────────

def _duckduckgo_instant(query: str) -> dict:
    """Public instant-answer JSON (no API key)."""
    q = (query or "").strip()[:240]
    if len(q) < 2:
        return {}
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": q, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=10,
            headers={"User-Agent": "SystemOfficeAssistant/1.0 (+https://localhost)"},
        )
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {}
    except Exception as e:
        logger.warning("DuckDuckGo instant search failed: %s", e)
        return {}


def _web_search_snippets(query: str, max_chars: int = 4000) -> str:
    """Format DDG instant results for injection into the user turn."""
    data = _duckduckgo_instant(query)
    if not data:
        return ""
    lines = []
    if data.get("Abstract"):
        lines.append(f"Summary: {data['Abstract']}")
    if data.get("Answer"):
        lines.append(f"Quick answer: {data['Answer']}")
    for r in data.get("RelatedTopics", [])[:6]:
        if isinstance(r, dict) and r.get("Text"):
            lines.append(f"- {r['Text']}")
    text = "\n".join(lines)
    return text[:max_chars] if text else ""


# ── Routes ────────────────────────────────────────────────────────────────────

@system_bp.route("/api/health", methods=["GET"])
def health():
    """Health check endpoint."""
    import importlib.util
    budget = get_usage_summary()
    pdf_ready = importlib.util.find_spec("reportlab") is not None
    if not pdf_ready:
        pdf_ready = importlib.util.find_spec("weasyprint") is not None
    return jsonify({
        "status": "ok",
        "service": "Agency Portal Assistant API",
        "budget_remaining": budget["remaining"],
        "budget_percent_used": budget["percent_used"],
        "pptx_export_ready": importlib.util.find_spec("pptx") is not None,
        "pdf_export_ready": pdf_ready,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })


@system_bp.route("/api/test-whatsapp", methods=["GET"])
def test_whatsapp():
    """Diagnose WhatsApp notification setup."""
    import requests as req
    from requests.auth import HTTPBasicAuth

    sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    frm   = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    to    = os.getenv("FOUNDER_WHATSAPP", "")

    cfg = {
        "TWILIO_ACCOUNT_SID_set": bool(sid),
        "TWILIO_AUTH_TOKEN_set":  bool(token),
        "TWILIO_WHATSAPP_FROM":   frm,
        "FOUNDER_WHATSAPP":       to,
        "to_has_prefix":          to.startswith("whatsapp:"),
    }

    if not sid or not token or not to:
        return jsonify({"configured": False, "config": cfg, "error": "Missing env vars"}), 200

    # Ensure prefix is correct
    if not to.startswith("whatsapp:"):
        to = "whatsapp:" + to

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    payload = {"From": frm, "To": to, "Body": "✅ Test message from Claude Office Assistant! WhatsApp notifications are working."}
    try:
        r = req.post(url, data=payload, auth=HTTPBasicAuth(sid, token), timeout=10)
        return jsonify({
            "configured": True,
            "config": cfg,
            "twilio_status": r.status_code,
            "twilio_response": r.json() if r.headers.get("content-type","").startswith("application/json") else r.text[:400],
            "success": r.status_code in (200, 201),
        })
    except Exception as e:
        return jsonify({"configured": True, "config": cfg, "error": str(e)}), 200


@system_bp.route("/api/web-search", methods=["GET"])
def web_search_api():
    """DuckDuckGo instant-answer lookup. Query param: q="""
    q = (request.args.get("q") or "").strip()[:240]
    if len(q) < 2:
        return jsonify({"error": "q must be at least 2 characters"}), 400
    snippet = _web_search_snippets(q, max_chars=8000)
    if not snippet:
        return jsonify({"query": q, "snippet": "", "note": "No instant results returned — try a different query."})
    return jsonify({"query": q, "snippet": snippet})


@system_bp.route("/api/routes", methods=["GET"])
def list_routes():
    """Returns the task → model routing table."""
    return jsonify({
        "routing_table": get_all_routes(),
        "models": {
            "haiku":  os.getenv("HAIKU_MODEL",  "claude-haiku-4-5"),
            "sonnet": os.getenv("SONNET_MODEL", "claude-sonnet-4-5"),
        }
    })


@system_bp.route("/api/budget", methods=["GET"])
def budget_status():
    """Returns current month budget usage + alert level forDashboard banner."""
    summary = get_usage_summary()
    pct = summary.get("percent_used", 0)
    alert_level = "critical" if pct >= 90 else "warning" if pct >= 70 else "ok"
    return jsonify({**summary, "alert_level": alert_level, "percent_used": pct})


@system_bp.route("/api/usage", methods=["GET"])
def usage_dashboard():
    """Full usageDashboard data — powers the cost monitoringDashboard."""
    all_flag = request.args.get("all", "false").lower() == "true"
    month = request.args.get("month")
    summary  = get_usage_summary(all_calls=all_flag, month_key=month)
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


@system_bp.route("/api/usage/export", methods=["GET"])
def export_usage():
    """Export API usage log as CSV."""
    calls = get_all_usage_logs()

    emp_map = {}
    try:
        emp_file = Path(__file__).parent.parent.parent / "config" / "employees.json"
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
    cw.writerow(["Timestamp", "User", "Task", "Model Tier", "Model Name",
                 "Input Tokens", "Output Tokens", "Cost (USD)", "Cost (INR)", "Month"])
    for c in reversed(calls):
        uid = c.get("user_id", "")
        cost_usd = c.get("cost_usd", 0)
        cost_inr = round(cost_usd * 83.5, 2) if cost_usd else 0.0
        cw.writerow([
            c.get("timestamp", ""),
            format_user(uid),
            c.get("task_type", ""),
            c.get("model_tier", ""),
            c.get("model_name", ""),
            c.get("input_tokens", 0),
            c.get("output_tokens", 0),
            cost_usd,
            cost_inr,
            c.get("month", ""),
        ])

    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=claude_api_usage.csv"}
    )


# ── DB Transfer Utilities (temporary admin endpoints) ─────────────────────────
# Protected by SECRET_KEY env var.  Remove these routes after transfer is done.

DB_PATH = Path(__file__).parent.parent.parent / "logs" / "app.db"
_SECRET = os.getenv("FLASK_SECRET_KEY") or os.getenv("SECRET_KEY", "")


def _check_secret():
    """Return True if the ?secret= query param matches SECRET_KEY."""
    provided = request.args.get("secret", "")
    if not _SECRET or not provided:
        return False
    # constant-time compare
    import hmac
    return hmac.compare_digest(provided, _SECRET)


@system_bp.route("/admin/download-db", methods=["GET"])
def download_db():
    """
    Download the SQLite database file.
    Usage: GET /admin/download-db?secret=<SECRET_KEY>
    REMOVE this endpoint after transfer is complete.
    """
    if not _check_secret():
        return jsonify({"error": "Unauthorized"}), 401
    if not DB_PATH.exists():
        return jsonify({"error": f"DB not found at {DB_PATH}"}), 404
    return send_file(
        str(DB_PATH),
        as_attachment=True,
        download_name="app.db",
        mimetype="application/octet-stream",
    )


@system_bp.route("/admin/upload-db", methods=["POST"])
def upload_db():
    """
    Upload a replacement SQLite database file.
    Usage: POST /admin/upload-db?secret=<SECRET_KEY>  with file field 'db'
    REMOVE this endpoint after transfer is complete.
    """
    if not _check_secret():
        return jsonify({"error": "Unauthorized"}), 401
    f = request.files.get("db")
    if not f:
        return jsonify({"error": "No file uploaded. Use field name 'db'"}), 400
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    backup = DB_PATH.with_suffix(".db.bak")
    if DB_PATH.exists():
        import shutil
        shutil.copy2(DB_PATH, backup)
        logger.info(f"Backed up existing DB to {backup}")
    f.save(str(DB_PATH))
    logger.info(f"DB uploaded successfully ({DB_PATH.stat().st_size} bytes)")
    return jsonify({"success": True, "message": "DB uploaded. Restart the service to apply.", "backup": str(backup) if backup.exists() else None})
