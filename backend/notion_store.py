"""
notion_store.py — Notion API Integration
=========================================
Reads/writes clients and tasks to Notion databases.

Setup:
  1. Go to https://www.notion.so/my-integrations and create an Internal Integration.
  2. Copy the "Internal Integration Token" → set as NOTION_TOKEN in config/.env
  3. Create two Notion databases:
       - Clients DB  → share with integration → copy DB ID → set NOTION_CLIENTS_DB_ID
       - Tasks DB    → share with integration → copy DB ID → set NOTION_TASKS_DB_ID
  4. See ARCHITECTURE.md for required database property schemas.

If NOTION_TOKEN is not set, all functions return empty results gracefully (no crash).
"""

import os
import logging
import requests
from typing import Optional

try:
    from notifications import notify_task_status_changed
except ImportError:
    notify_task_status_changed = None

logger = logging.getLogger(__name__)

NOTION_TOKEN      = os.getenv("NOTION_TOKEN", "")
CLIENTS_DB_ID     = os.getenv("NOTION_CLIENTS_DB_ID", "")
TASKS_DB_ID       = os.getenv("NOTION_TASKS_DB_ID", "")
NOTION_VERSION    = "2022-06-28"

# ── Notion is configured? ────────────────────────────────────────────────────

def is_configured() -> bool:
    return bool(NOTION_TOKEN and CLIENTS_DB_ID and TASKS_DB_ID)


def _headers() -> dict:
    return {
        "Authorization":  f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type":   "application/json",
    }


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _text(value: str) -> dict:
    """Notion rich_text property value."""
    return {"rich_text": [{"text": {"content": str(value or "")}}]}


def _title(value: str) -> dict:
    """Notion title property value."""
    return {"title": [{"text": {"content": str(value or "")}}]}


def _select(value: str) -> dict:
    """Notion select property value."""
    return {"select": {"name": str(value or "")}}


def _date(value: str) -> dict:
    """Notion date property value (ISO date string or empty)."""
    if value:
        return {"date": {"start": value}}
    return {"date": None}


def _number(value) -> dict:
    return {"number": int(value) if value is not None else 0}


def _get_text(prop: dict) -> str:
    """Extract plain text from a Notion rich_text or title property."""
    items = prop.get("rich_text") or prop.get("title") or []
    return "".join(t.get("plain_text", "") for t in items)


def _get_select(prop: dict) -> str:
    sel = prop.get("select") or {}
    return sel.get("name", "")


def _get_date(prop: dict) -> str:
    d = prop.get("date") or {}
    return d.get("start", "")


def _get_number(prop: dict) -> int:
    return prop.get("number") or 0


def _get_relation_ids(prop: dict) -> list:
    return [r["id"] for r in prop.get("relation", [])]


# ══════════════════════════════════════════════════════════════════════════════
# CLIENTS
# ══════════════════════════════════════════════════════════════════════════════

def create_client(name: str, contact: str = "", requirements: str = "",
                  deadline: str = "", budget: str = "", notes: str = "",
                  status: str = "active") -> Optional[dict]:
    """
    Creates a new page in the Clients Notion DB.
    Returns: { notion_id, name } or None on failure.

    Required DB properties:
      Name (title), Contact (rich_text), Requirements (rich_text),
      Deadline (date), Budget (rich_text), Notes (rich_text), Status (select)
    """
    if not is_configured():
        logger.warning("Notion not configured — skipping create_client")
        return None

    payload = {
        "parent": {"database_id": CLIENTS_DB_ID},
        "properties": {
            "Client":       _title(name),
            "Contact":      _text(contact),
            "Requirements": _text(requirements),
            "Deadline":     _date(deadline),
            "Budget":       _text(budget),
            "Notes":        _text(notes),
            "Status":       _select(status),
        },
    }

    try:
        r = requests.post(
            "https://api.notion.com/v1/pages",
            headers=_headers(),
            json=payload,
            timeout=10,
        )
        r.raise_for_status()
        page = r.json()
        logger.info(f"Notion: created client '{name}' — page {page['id']}")
        return {"notion_id": page["id"], "name": name}
    except Exception as e:
        logger.error(f"Notion create_client failed: {e}")
        return None


def list_clients(status_filter: str = "") -> list:
    """
    Returns all clients from Notion Clients DB.
    Each dict: { notion_id, name, contact, requirements, deadline, budget, notes, status }
    """
    if not is_configured():
        return []

    payload: dict = {"page_size": 100}
    if status_filter:
        payload["filter"] = {
            "property": "Status",
            "select": {"equals": status_filter},
        }

    clients = []
    try:
        has_more = True
        while has_more:
            r = requests.post(
                f"https://api.notion.com/v1/databases/{CLIENTS_DB_ID}/query",
                headers=_headers(),
                json=payload,
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            pages = data.get("results", [])
            for p in pages:
                props = p.get("properties", {})
                clients.append({
                    "notion_id":    p["id"],
                    "name":         _get_text(props.get("Client", {})),
                    "contact":      _get_text(props.get("Contact", {})),
                    "requirements": _get_text(props.get("Requirements", {})),
                    "deadline":     _get_date(props.get("Deadline", {})),
                    "budget":       _get_text(props.get("Budget", {})),
                    "notes":        _get_text(props.get("Notes", {})),
                    "status":       _get_select(props.get("Status", {})),
                    "url":          p.get("url", ""),
                })
            
            has_more = data.get("has_more", False)
            if has_more:
                payload["start_cursor"] = data.get("next_cursor")
                
        return clients
    except Exception as e:
        logger.error(f"Notion list_clients failed: {e}")
        return []


def update_client_status(notion_id: str, status: str) -> bool:
    """Update the Status property of a client page."""
    if not is_configured():
        return False
    try:
        r = requests.patch(
            f"https://api.notion.com/v1/pages/{notion_id}",
            headers=_headers(),
            json={"properties": {"Status": _select(status)}},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Notion update_client_status failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TASKS
# ══════════════════════════════════════════════════════════════════════════════

def create_task(title: str, client_name: str, client_notion_id: str,
                assigned_to: str = "", due_date: str = "",
                status: str = "not_started", progress: int = 0,
                service: str = "") -> Optional[dict]:
    """
    Creates a new page in the Tasks Notion DB.
    Returns: { notion_id, title } or None on failure.

    Required DB properties:
      Title (title), Client (rich_text), ClientID (rich_text),
      AssignedTo (select), DueDate (date), Status (select),
      Progress (number), Service (select)
    """
    if not is_configured():
        return None

    payload = {
        "parent": {"database_id": TASKS_DB_ID},
        "properties": {
            "Task":          _title(title),
            "Customer Name": _text(client_name),
            "Client ID":     _text(client_notion_id),
            "Assigned To":   _select(assigned_to),
            "Due Date":      _date(due_date),
            "Status":        _select(status),
            "Progress":      _number(progress),
            "Task Type":     _select(service),
        },
    }

    try:
        r = requests.post(
            "https://api.notion.com/v1/pages",
            headers=_headers(),
            json=payload,
            timeout=10,
        )
        r.raise_for_status()
        page = r.json()
        logger.info(f"Notion: created task '{title}' for client '{client_name}'")
        return {"notion_id": page["id"], "title": title}
    except Exception as e:
        logger.error(f"Notion create_task failed: {e}")
        return None


def list_tasks(assigned_to: str = "", client_notion_id: str = "",
               status_filter: str = "") -> list:
    """
    Returns tasks from Notion Tasks DB.
    Filter by assigned_to (employee id/name), client_notion_id, or status.
    """
    if not is_configured():
        return []

    filters = []
    if assigned_to:
        filters.append({"property": "Assigned To", "select": {"equals": assigned_to}})
    if client_notion_id:
        filters.append({"property": "Client ID", "rich_text": {"equals": client_notion_id}})
    if status_filter:
        filters.append({"property": "Status", "select": {"equals": status_filter}})

    payload: dict = {"page_size": 200}
    if len(filters) == 1:
        payload["filter"] = filters[0]
    elif len(filters) > 1:
        payload["filter"] = {"and": filters}

    tasks = []
    try:
        has_more = True
        while has_more:
            r = requests.post(
                f"https://api.notion.com/v1/databases/{TASKS_DB_ID}/query",
                headers=_headers(),
                json=payload,
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            pages = data.get("results", [])
            for p in pages:
                props = p.get("properties", {})
                tasks.append({
                    "notion_id":   p["id"],
                    "title":       _get_text(props.get("Task", {})),
                    "client":      _get_text(props.get("Customer Name", {})),
                    "client_id":   _get_text(props.get("Client ID", {})),
                    "assigned_to": _get_select(props.get("Assigned To", {})),
                    "due_date":    _get_date(props.get("Due Date", {})),
                    "status":      _get_select(props.get("Status", {})),
                    "progress":    _get_number(props.get("Progress", {})),
                    "service":     _get_select(props.get("Task Type", {})),
                    "url":         p.get("url", ""),
                })
            has_more = data.get("has_more", False)
            if has_more:
                payload["start_cursor"] = data.get("next_cursor")
                
        return tasks
    except Exception as e:
        logger.error(f"Notion list_tasks failed: {e}")
        return []


def update_task(notion_id: str, status: str = None, progress: int = None,
                submission_note: str = None, assigned_to: str = None,
                new_title: str = None, due_date: str = None,
                task_title: str = "", assignee: str = "", client_name: str = "") -> bool:
    """
    Update Status, Progress, SubmissionNote, AssignedTo, Title, and/or DueDate on a task page.
    Pass only the fields you want to change.
    Automatically sends WhatsApp notification on key status changes.
    """
    if not is_configured():
        return False

    props = {}
    if status is not None:
        props["Status"] = _select(status)
    if progress is not None:
        props["Progress"] = _number(progress)
    if submission_note is not None:
        props["Notes"] = _text(submission_note)
    if assigned_to is not None:
        props["Assigned To"] = _select(assigned_to)
    if new_title is not None:
        props["Task"] = _title(new_title)  # The title property is called 'Task'
    if due_date is not None:
        props["Due Date"] = _date(due_date)

    if not props:
        return True  # nothing to update

    try:
        r = requests.patch(
            f"https://api.notion.com/v1/pages/{notion_id}",
            headers=_headers(),
            json={"properties": props},
            timeout=10,
        )
        r.raise_for_status()

        # ── WhatsApp notification ──
        if status and notify_task_status_changed:
            try:
                notify_task_status_changed(
                    task_title  = task_title  or "(unnamed task)",
                    assignee    = assignee    or "Team member",
                    client_name = client_name or "Unknown client",
                    old_status  = "",
                    new_status  = status,
                )
            except Exception as ne:
                logger.warning(f"WhatsApp notification failed (non-fatal): {ne}")

        return True
    except Exception as e:
        logger.error(f"Notion update_task failed: {e}")
        return False


def archive_notion_page(page_id: str) -> bool:
    """
    Archives a Notion page (moves it to trash). Used for deleting clients/tasks.
    """
    if not is_configured() or not page_id:
        return False
    try:
        r = requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=_headers(),
            json={"archived": True},
            timeout=10,
        )
        r.raise_for_status()
        logger.info(f"Notion: archived page {page_id}")
        return True
    except Exception as e:
        logger.error(f"Notion archive_notion_page failed: {e}")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD AGGREGATE — all clients + their tasks
# ══════════════════════════════════════════════════════════════════════════════

def get_dashboard_data() -> dict:
    """
    Returns { clients: [ { ...client, tasks: [...] } ] }
    Used by /api/notion/dashboard to power projects.html
    """
    if not is_configured():
        return {"configured": False, "clients": []}

    clients = list_clients()
    all_tasks = list_tasks()  # fetch all tasks once

    for client in clients:
        client["tasks"] = [
            t for t in all_tasks
            if t["client_id"] == client["notion_id"]
        ]

    return {"configured": True, "clients": clients}
