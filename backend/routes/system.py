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
from flask import Blueprint, Response, jsonify, request

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
            headers={"User-Agent": "ClaudeOfficeAssistant/1.0 (+https://localhost)"},
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
        "service": "Claude Office Assistant API",
        "budget_remaining": budget["remaining"],
        "budget_percent_used": budget["percent_used"],
        "pptx_export_ready": importlib.util.find_spec("pptx") is not None,
        "pdf_export_ready": pdf_ready,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })


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
    """Returns current month budget usage + alert level for dashboard banner."""
    summary = get_usage_summary()
    pct = summary.get("percent_used", 0)
    alert_level = "critical" if pct >= 90 else "warning" if pct >= 70 else "ok"
    return jsonify({**summary, "alert_level": alert_level, "percent_used": pct})


@system_bp.route("/api/usage", methods=["GET"])
def usage_dashboard():
    """Full usage dashboard data — powers the cost monitoring dashboard."""
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
