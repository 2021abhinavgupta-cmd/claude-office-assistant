"""
Model Router — Routes tasks to Claude Haiku (cheap) or Sonnet (powerful)
Task complexity determines which model is used.
"""

import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

# Task → Model Routing Table
# Haiku: Fast, cheap — good for simple/structured tasks
# Sonnet: Powerful, precise — good for creative/complex tasks
TASK_ROUTING = {
    # Haiku tasks — fast & cheap
    "captions":        "haiku",
    "scripts":         "haiku",
    "general":         "haiku",
    "summarization":   "haiku",
    "translation":     "haiku",
    "email_drafting":  "haiku",
    "data_extraction": "haiku",
    "qa":              "haiku",
    "analysis":        "haiku",
    "research":        "haiku",
    "legal":           "haiku",
    # Sonnet tasks — complex & creative
    "coding":          "sonnet",
    "html_design":     "sonnet",
    "presentations":   "sonnet",
    "data_analysis":   "sonnet",
}

# Cost per 1M tokens (USD) — Claude 4 pricing (May 2025)
MODEL_COSTS = {
    "haiku": {
        "input":  0.80,   # $0.80 / 1M input tokens  (claude-haiku-4-5-20251001)
        "output": 4.00,   # $4.00 / 1M output tokens
        "name":   os.getenv("HAIKU_MODEL", "claude-haiku-4-5-20251001"),
    },
    "sonnet": {
        "input":  3.00,   # $3.00 / 1M input tokens  (claude-sonnet-4-6)
        "output": 15.00,  # $15.00 / 1M output tokens
        "name":   os.getenv("SONNET_MODEL", "claude-sonnet-4-6"),
    }
}

def get_model_for_task(task_type: str) -> dict:
    """
    Returns the model config dict for the given task type.
    Falls back to Haiku if task is unknown.
    """
    task_key = task_type.lower().replace(" ", "_")
    tier = TASK_ROUTING.get(task_key, "haiku")
    config = MODEL_COSTS[tier].copy()
    config["tier"] = tier
    config["task"] = task_type
    return config

def calculate_cost(model_tier: str, input_tokens: int, output_tokens: int) -> float:
    """
    Calculate USD cost for a given API call.
    Returns cost in dollars (float).
    """
    rates = MODEL_COSTS.get(model_tier, MODEL_COSTS["haiku"])
    input_cost  = (input_tokens  / 1_000_000) * rates["input"]
    output_cost = (output_tokens / 1_000_000) * rates["output"]
    return round(input_cost + output_cost, 6)

def get_all_routes() -> dict:
    """Returns the full routing table — useful for frontend display."""
    return {task: TASK_ROUTING[task] for task in TASK_ROUTING}
