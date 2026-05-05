"""
Weekly Team Summary Generator
Run this script every Friday to compile a digest of how Claude is being used by the team.
"""

import os
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"
DB_PATH = LOG_DIR / "app.db"

def get_connection():
    return sqlite3.connect(DB_PATH)

def generate_weekly_digest():
    print("📊 Generating Weekly Claude Digest...")
    
    if not DB_PATH.exists():
        print("No database found.")
        return

    conn = get_connection()
    cursor = conn.cursor()
    
    # Calculate the date 7 days ago
    seven_days_ago = (datetime.utcnow() - timedelta(days=7)).isoformat() + "Z"
    
    # Fetch all logs
    cursor.execute("SELECT data FROM usage_logs")
    recent_logs = []
    
    for (data_str,) in cursor.fetchall():
        try:
            log = json.loads(data_str)
            if log.get("timestamp", "") >= seven_days_ago:
                recent_logs.append(log)
        except Exception:
            pass

    if not recent_logs:
        print("\nNo usage recorded in the last 7 days.")
        conn.close()
        return

    # Metrics
    total_calls = len(recent_logs)
    total_cost = sum(log.get("cost_usd", 0.0) for log in recent_logs)
    
    # Task breakdown
    tasks = {}
    for log in recent_logs:
        t = log.get("task_type", "general")
        tasks[t] = tasks.get(t, 0) + 1
    
    # Top users
    users = {}
    for log in recent_logs:
        u = log.get("user_id", "unknown")
        users[u] = users.get(u, 0) + 1
        
    top_tasks = sorted(tasks.items(), key=lambda x: x[1], reverse=True)[:3]
    top_users = sorted(users.items(), key=lambda x: x[1], reverse=True)[:3]
    
    # Build Digest
    digest = [
        "## 📈 Claude Weekly Team Digest",
        f"**Date:** {datetime.utcnow().strftime('%Y-%m-%d')}\n",
        f"**Total Prompts Answered:** {total_calls}",
        f"**Total API Cost this Week:** ${total_cost:.4f}\n",
        "### 🏆 Top 3 Most Used Task Types:",
    ]
    for i, (task, count) in enumerate(top_tasks, 1):
        digest.append(f"{i}. **{task.title()}** ({count} queries)")
        
    digest.append("\n### 🧑‍💻 Top 3 Most Active Employees:")
    for i, (user, count) in enumerate(top_users, 1):
        digest.append(f"{i}. **{user}** ({count} queries)")
        
    digest.append("\n_Tip: Notice a trend? Consider adding a custom system prompt or project context for your team's most frequent tasks!_")
    
    # Write to file
    digest_path = LOG_DIR / "weekly_digest.md"
    with open(digest_path, "w") as f:
        f.write("\n".join(digest))
        
    print(f"✅ Digest generated successfully at {digest_path.resolve()}")
    print("\n" + "\n".join(digest))
    
    conn.close()

if __name__ == "__main__":
    generate_weekly_digest()
