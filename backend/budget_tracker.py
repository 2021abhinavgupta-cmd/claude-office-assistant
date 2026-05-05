"""
Budget Tracker — Tracks monthly Claude API spend, enforces $20/month cap.
Usage data is stored in logs/usage.json
"""

import os
import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

BUDGET_LIMIT = float(os.getenv("MONTHLY_BUDGET_LIMIT", 20.0))
LOG_DIR      = Path(__file__).parent.parent / "logs"
USAGE_FILE   = LOG_DIR / "usage.json"
MAX_CALLS    = 5000  # Cap the calls log to prevent unbounded file growth

logger = logging.getLogger(__name__)


def _load_usage() -> dict:
    """Load usage data from JSON file. Creates it if missing or corrupt."""
    LOG_DIR.mkdir(exist_ok=True)
    if not USAGE_FILE.exists():
        return {"monthly": {}, "total_spent": 0.0, "calls": []}
    try:
        with open(USAGE_FILE, "r") as f:
            data = json.load(f)
        # Ensure expected keys exist (handles partial/old format files)
        data.setdefault("monthly", {})
        data.setdefault("total_spent", 0.0)
        data.setdefault("calls", [])
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"usage.json is corrupt or unreadable ({e}) — resetting to empty state")
        return {"monthly": {}, "total_spent": 0.0, "calls": []}


def _save_usage(data: dict):
    """Persist usage data atomically — avoids file corruption on crash."""
    LOG_DIR.mkdir(exist_ok=True)
    # Write to a temp file in the same directory, then rename (atomic on POSIX)
    fd, tmp_path = tempfile.mkstemp(dir=LOG_DIR, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, USAGE_FILE)
    except Exception as e:
        logger.error(f"Failed to save usage data: {e}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def get_current_month_key() -> str:
    """Returns YYYY-MM string for current month."""
    return datetime.utcnow().strftime("%Y-%m")


def get_monthly_spend() -> float:
    """Returns total spend for the current calendar month (USD)."""
    data = _load_usage()
    month_key = get_current_month_key()
    return data["monthly"].get(month_key, 0.0)


def check_budget_available(estimated_cost: float = 0.0) -> dict:
    """
    Check if there's budget remaining for a call.
    Returns dict with: allowed (bool), spent, remaining, limit
    """
    spent = get_monthly_spend()
    remaining = BUDGET_LIMIT - spent
    allowed = (spent + estimated_cost) <= BUDGET_LIMIT

    return {
        "allowed": allowed,
        "spent": round(spent, 4),
        "remaining": round(remaining, 4),
        "limit": BUDGET_LIMIT,
        "month": get_current_month_key(),
    }


def record_usage(task_type: str, model_tier: str, model_name: str,
                 input_tokens: int, output_tokens: int, cost: float,
                 user_id: str = "anonymous"):
    """
    Record a completed API call to the usage log.
    Updates monthly spend total.
    """
    data = _load_usage()
    month_key = get_current_month_key()

    # Update monthly total
    data["monthly"][month_key] = data["monthly"].get(month_key, 0.0) + cost
    data["total_spent"] = data.get("total_spent", 0.0) + cost

    # Append call log entry
    entry = {
        "timestamp":     datetime.utcnow().isoformat() + "Z",
        "user_id":       user_id,
        "task_type":     task_type,
        "model_tier":    model_tier,
        "model_name":    model_name,
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "cost_usd":      round(cost, 6),
        "month":         month_key,
    }
    data.setdefault("calls", []).append(entry)

    # Trim call log to avoid unbounded file growth
    if len(data["calls"]) > MAX_CALLS:
        data["calls"] = data["calls"][-MAX_CALLS:]

    _save_usage(data)
    logger.info(f"Usage recorded: {task_type} | {model_tier} | ${cost:.6f} | Total this month: ${data['monthly'][month_key]:.4f}")
    return entry


def get_usage_summary(all_calls: bool = False) -> dict:
    """Returns full usage summary for dashboard display."""
    data = _load_usage()
    month_key = get_current_month_key()
    monthly_spend = data["monthly"].get(month_key, 0.0)

    # Count calls per model tier this month
    month_calls = [c for c in data.get("calls", []) if c.get("month") == month_key]
    haiku_calls  = sum(1 for c in month_calls if c.get("model_tier") == "haiku")
    sonnet_calls = sum(1 for c in month_calls if c.get("model_tier") == "sonnet")

    # Per-user spend breakdown
    user_spend = {}
    for c in month_calls:
        uid = c.get("user_id", "unknown")
        user_spend[uid] = round(user_spend.get(uid, 0.0) + c.get("cost_usd", 0.0), 6)
    top_users = sorted(
        [{"user_id": u, "cost_usd": v, "calls": sum(1 for c in month_calls if c.get("user_id") == u)}
         for u, v in user_spend.items()],
        key=lambda x: x["cost_usd"], reverse=True
    )

    # Task breakdown
    task_breakdown = {}
    for c in month_calls:
        t = c.get("task_type", "general")
        if t not in task_breakdown:
            task_breakdown[t] = {"calls": 0, "cost": 0.0}
        task_breakdown[t]["calls"] += 1
        task_breakdown[t]["cost"] = round(task_breakdown[t]["cost"] + c.get("cost_usd", 0.0), 6)

    # Return all calls or just the most recent for the dashboard table
    calls_to_return = list(reversed(month_calls)) if all_calls else list(reversed(month_calls[:100]))

    return {
        "month":            month_key,
        "monthly_spend":    round(monthly_spend, 4),
        "budget_limit":     BUDGET_LIMIT,
        "remaining":        round(BUDGET_LIMIT - monthly_spend, 4),
        "percent_used":     round((monthly_spend / BUDGET_LIMIT) * 100, 1),
        "total_calls":      len(month_calls),
        "haiku_calls":      haiku_calls,
        "sonnet_calls":     sonnet_calls,
        "total_spent_ever": round(data.get("total_spent", 0.0), 4),
        "recent_calls":     calls_to_return,
        "top_users":        top_users,
        "task_breakdown":   task_breakdown,
    }


def get_all_calls_csv() -> str:
    """Return all call records as a CSV string for export."""
    import csv, io
    data = _load_usage()
    calls = data.get("calls", [])
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "timestamp", "user_id", "task_type", "model_tier", "model_name",
        "input_tokens", "output_tokens", "cost_usd", "month"
    ], extrasaction="ignore")
    writer.writeheader()
    writer.writerows(calls)
    return output.getvalue()
