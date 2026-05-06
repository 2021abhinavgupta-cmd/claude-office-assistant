"""
Budget Tracker — Tracks monthly Claude API spend, enforces $20/month cap.
Usage data is stored in SQLite via db.py
"""

import os
import json
import logging
from datetime import datetime
from dotenv import load_dotenv

from db import get_connection

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

BUDGET_LIMIT = float(os.getenv("MONTHLY_BUDGET_LIMIT", 20.0))

logger = logging.getLogger(__name__)

def get_current_month_key() -> str:
    """Returns YYYY-MM string for current month."""
    return datetime.utcnow().strftime("%Y-%m")

def get_monthly_spend() -> float:
    """Returns total spend for the current calendar month (USD)."""
    conn = get_connection()
    cursor = conn.cursor()
    month_key = get_current_month_key()
    cursor.execute("SELECT total_cost FROM budget WHERE period=?", (month_key,))
    row = cursor.fetchone()
    conn.close()
    return float(row[0]) if row else 0.0

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
    month_key = get_current_month_key()
    
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
    
    conn = get_connection()
    with conn:
        # Update monthly total
        cursor = conn.cursor()
        cursor.execute("SELECT total_cost FROM budget WHERE period=?", (month_key,))
        row = cursor.fetchone()
        current_total = float(row[0]) if row else 0.0
        new_total = current_total + cost
        conn.execute("INSERT OR REPLACE INTO budget (period, total_cost) VALUES (?, ?)", (month_key, new_total))
        
        # Insert log
        conn.execute("INSERT INTO usage_logs (data) VALUES (?)", (json.dumps(entry),))
        
        # Also update "all_time" budget entry
        cursor.execute("SELECT total_cost FROM budget WHERE period='all_time'")
        row = cursor.fetchone()
        current_all = float(row[0]) if row else 0.0
        conn.execute("INSERT OR REPLACE INTO budget (period, total_cost) VALUES (?, ?)", ("all_time", current_all + cost))
        
    conn.close()
    logger.info(f"Usage recorded: {task_type} | {model_tier} | ${cost:.6f} | Total this month: ${new_total:.4f}")
    return entry

def get_all_usage_logs() -> list:
    """Return all historical usage logs across all months."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM usage_logs")
    all_logs = []
    for (data_str,) in cursor.fetchall():
        try:
            all_logs.append(json.loads(data_str))
        except: pass
    conn.close()
    return all_logs

def get_usage_summary(all_calls: bool = False) -> dict:
    """Returns full usage summary for dashboard display."""
    month_key = get_current_month_key()
    monthly_spend = get_monthly_spend()
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get total spent ever
    cursor.execute("SELECT total_cost FROM budget WHERE period='all_time'")
    row = cursor.fetchone()
    total_spent_ever = float(row[0]) if row else monthly_spend # fallback
    
    # Get calls for this month
    cursor.execute("SELECT data FROM usage_logs")
    all_logs = []
    for (data_str,) in cursor.fetchall():
        try:
            log = json.loads(data_str)
            if log.get("month") == month_key:
                all_logs.append(log)
        except: pass
    conn.close()
    
    haiku_calls  = sum(1 for c in all_logs if c.get("model_tier") == "haiku")
    sonnet_calls = sum(1 for c in all_logs if c.get("model_tier") == "sonnet")

    # Per-user spend breakdown
    user_spend = {}
    for c in all_logs:
        uid = c.get("user_id", "unknown")
        user_spend[uid] = round(user_spend.get(uid, 0.0) + c.get("cost_usd", 0.0), 6)
    top_users = sorted(
        [{"user_id": u, "cost_usd": v, "calls": sum(1 for c in all_logs if c.get("user_id") == u)}
         for u, v in user_spend.items()],
        key=lambda x: x["cost_usd"], reverse=True
    )

    # Task breakdown
    task_breakdown = {}
    for c in all_logs:
        t = c.get("task_type", "general")
        if t not in task_breakdown:
            task_breakdown[t] = {"calls": 0, "cost": 0.0}
        task_breakdown[t]["calls"] += 1
        task_breakdown[t]["cost"] = round(task_breakdown[t]["cost"] + c.get("cost_usd", 0.0), 6)

    # Return all calls or just the most recent for the dashboard table
    all_logs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    calls_to_return = all_logs if all_calls else all_logs[:100]

    return {
        "month":            month_key,
        "monthly_spend":    round(monthly_spend, 4),
        "budget_limit":     BUDGET_LIMIT,
        "remaining":        round(BUDGET_LIMIT - monthly_spend, 4),
        "percent_used":     round((monthly_spend / BUDGET_LIMIT) * 100, 1),
        "total_calls":      len(all_logs),
        "haiku_calls":      haiku_calls,
        "sonnet_calls":     sonnet_calls,
        "total_spent_ever": round(total_spent_ever, 4),
        "recent_calls":     calls_to_return,
        "top_users":        top_users,
        "task_breakdown":   task_breakdown,
    }

def get_all_calls_csv() -> str:
    """Return all call records as a CSV string for export."""
    import csv, io
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM usage_logs ORDER BY id DESC")
    calls = []
    for (data_str,) in cursor.fetchall():
        try: calls.append(json.loads(data_str))
        except: pass
    conn.close()
    
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "timestamp", "user_id", "task_type", "model_tier", "model_name",
        "input_tokens", "output_tokens", "cost_usd", "month"
    ], extrasaction="ignore")
    writer.writeheader()
    writer.writerows(calls)
    return output.getvalue()
