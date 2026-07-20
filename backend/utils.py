import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
CONFIG_DIR = Path(__file__).parent.parent / "config"
EMPLOYEES_DB = CONFIG_DIR / "employees.json"

def now_ist():
    return datetime.now(IST).strftime("%H:%M:%S")

def today_ist():
    return datetime.now(IST).strftime("%Y-%m-%d")

def _load_employees() -> dict:
    if EMPLOYEES_DB.exists():
        with open(EMPLOYEES_DB) as f:
            return json.load(f)
    return {"employees": []}

def _save_employees(data: dict):
    """Atomic write to avoid JSON corruption on concurrent check-ins."""
    import tempfile
    tmp = EMPLOYEES_DB.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, EMPLOYEES_DB)

def _is_admin(user_id: str) -> bool:
    """All employees have full admin access."""
    return bool(user_id)
