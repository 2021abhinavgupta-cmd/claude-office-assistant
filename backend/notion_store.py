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
import time
import logging
import requests
from typing import Optional

try:
    from notifications import notify_task_status_changed
except ImportError:
    notify_task_status_changed = None

logger = logging.getLogger(__name__)


def _notion_request(method: str, url: str, **kwargs) -> requests.Response:
    """
    Make a Notion API request with 3-attempt exponential backoff.
    Raises requests.HTTPError on final failure.
    """
    last_exc = None
    for attempt in range(1, 4):
        try:
            r = requests.request(method, url, timeout=15, **kwargs)
            if r.status_code == 429:  # Notion rate limit
                wait = 2 ** (attempt - 1)
                logger.warning(f"Notion rate-limited, retrying in {wait}s (attempt {attempt})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.Timeout as e:
            logger.warning(f"Notion request timed out (attempt {attempt}): {url}")
            last_exc = e
            time.sleep(2 ** (attempt - 1))
        except requests.exceptions.HTTPError as e:
            raise  # non-retryable HTTP errors bubble up immediately
        except Exception as e:
            logger.warning(f"Notion request error (attempt {attempt}): {e}")
            last_exc = e
            time.sleep(2 ** (attempt - 1))
    raise requests.exceptions.ConnectionError(f"Notion request failed after 3 attempts: {url}") from last_exc

NOTION_TOKEN      = os.getenv("NOTION_TOKEN", "")
CLIENTS_DB_ID     = os.getenv("NOTION_CLIENTS_DB_ID", "")
TASKS_DB_ID       = os.getenv("NOTION_TASKS_DB_ID", "")
NOTION_VERSION    = "2022-06-28"

# ── Notion is configured? ────────────────────────────────────────────────────

def is_configured() -> bool:
    """Reads env vars dynamically so Railway hot-deploys take effect without restart."""
    return bool(
        os.getenv("NOTION_TOKEN") and
        os.getenv("NOTION_CLIENTS_DB_ID") and
        os.getenv("NOTION_TASKS_DB_ID")
    )


def _headers() -> dict:
    """Read token dynamically on every call so env var updates are picked up."""
    token = os.getenv("NOTION_TOKEN", "")
    return {
        "Authorization":  f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type":   "application/json",
    }

# Dynamic DB IDs (hot-reload safe)
def _clients_db() -> str:
    return os.getenv("NOTION_CLIENTS_DB_ID", CLIENTS_DB_ID)

def _tasks_db() -> str:
    return os.getenv("NOTION_TASKS_DB_ID", TASKS_DB_ID)


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _text(value: str) -> dict:
    """Notion rich_text property value."""
    return {"rich_text": [{"text": {"content": str(value or "")}}]}


def _title(value: str) -> dict:
    """Notion title property value."""
    return {"title": [{"text": {"content": str(value or "")}}]}


def _select(value: str) -> dict:
    """Notion select property. Sends None if value is blank (clears the field)."""
    if value and str(value).strip():
        return {"select": {"name": str(value).strip()}}
    return {"select": None}


def _multi_select(value: str) -> dict:
    """Notion multi_select property. Accepts a comma-separated string."""
    if not value or not str(value).strip():
        return {"multi_select": []}
    names = [n.strip() for n in str(value).split(",") if n.strip()]
    return {"multi_select": [{"name": n} for n in names]}


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


def _get_multi_select(prop: dict) -> str:
    items = prop.get("multi_select") or []
    return ", ".join(item.get("name", "") for item in items)


_page_title_cache = {}

def _fetch_page_title(page_id: str) -> str:
    if page_id in _page_title_cache:
        return _page_title_cache[page_id]
    try:
        r = _notion_request("GET", f"https://api.notion.com/v1/pages/{page_id}", headers=_headers())
        if r.status_code == 200:
            data = r.json()
            props = data.get("properties", {})
            # Find the title property
            for k, v in props.items():
                if v.get("type") == "title":
                    title = _get_text(v)
                    _page_title_cache[page_id] = title
                    return title
    except: pass
    _page_title_cache[page_id] = ""
    return ""

def _get_string_val(prop: dict) -> str:
    if not prop: return ""
    if "rich_text" in prop or "title" in prop: return _get_text(prop)
    if "select" in prop: return _get_select(prop)
    if "multi_select" in prop: return _get_multi_select(prop)
    if "formula" in prop:
        f = prop["formula"]
        if f.get("type") == "string": return f.get("string", "") or ""
    if "rollup" in prop:
        r = prop["rollup"]
        if r.get("type") == "array":
            arr = r.get("array", [])
            if arr: return _get_string_val(arr[0])
    if "relation" in prop:
        rels = prop.get("relation", [])
        if rels: return _fetch_page_title(rels[0]["id"])
    return ""


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
        r = _notion_request(
            "POST",
            "https://api.notion.com/v1/pages",
            headers=_headers(),
            json=payload,
        )
        page = r.json()
        logger.info(f"Notion: created client '{name}' — page {page['id']}")
        return {"notion_id": page["id"], "name": name}
    except Exception:
        logger.exception(f"Notion create_client failed for '{name}'")
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
            r = _notion_request(
                "POST",
                f"https://api.notion.com/v1/databases/{_clients_db()}/query",
                headers=_headers(),
                json=payload,
            )
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
    except Exception:
        logger.exception("Notion list_clients failed")
        return []


def update_client_status(notion_id: str, status: str) -> bool:
    """Update the Status property of a client page."""
    if not is_configured():
        return False
    try:
        _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{notion_id}",
            headers=_headers(),
            json={"properties": {"Status": _select(status)}},
        )
        return True
    except Exception:
        logger.exception(f"Notion update_client_status failed for {notion_id}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TASKS
# ══════════════════════════════════════════════════════════════════════════════

def create_task(title: str, client_name: str, client_notion_id: str,
                assigned_to: str = "", due_date: str = "",
                status: str = "not_started", progress: int = 0,
                service: str = "", notes: str = "") -> Optional[dict]:
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
        "parent": {"database_id": _tasks_db()},
        "properties": {
            "Task":          _title(title),
            "Customer Name": _text(client_name),
            "Client ID":     _text(client_notion_id),
            "Assigned To":   _multi_select(assigned_to),
            "Due Date":      _date(due_date),
            "Status":        _select(status),
            "Progress":      _number(progress),
            "Task Type":     _select(service),
        },
    }
    if notes:
        payload["properties"]["Notes"] = _text(notes)

    try:
        r = _notion_request(
            "POST",
            "https://api.notion.com/v1/pages",
            headers=_headers(),
            json=payload,
        )
        page = r.json()
        logger.info(f"Notion: created task '{title}' for client '{client_name}'")
        return {"notion_id": page["id"], "title": title}
    except Exception:
        logger.exception(f"Notion create_task failed for '{title}'")
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
        filters.append({"property": "Assigned To", "multi_select": {"contains": assigned_to}})
    if client_notion_id:
        filters.append({"property": "Client ID", "rich_text": {"equals": client_notion_id}})
    if status_filter:
        filters.append({"property": "Status", "select": {"equals": status_filter}})

    payload: dict = {
        "page_size": 200,
        "sorts": [{"timestamp": "created_time", "direction": "descending"}]
    }
    if len(filters) == 1:
        payload["filter"] = filters[0]
    elif len(filters) > 1:
        payload["filter"] = {"and": filters}

    tasks = []
    try:
        has_more = True
        while has_more:
            r = _notion_request(
                "POST",
                f"https://api.notion.com/v1/databases/{_tasks_db()}/query",
                headers=_headers(),
                json=payload,
            )
            data = r.json()
            pages = data.get("results", [])
            for p in pages:
                props = p.get("properties", {})
                desc = _get_string_val(props.get("Notes", {}))
                brief = _get_string_val(props.get("Brief", {}))
                content = _get_string_val(props.get("Content", {}))
                idea = _get_string_val(props.get("Idea", {}))
                scripts_copy = _get_string_val(props.get("Scripts/ Copy", {})) or _get_string_val(props.get("Script/ Copy", {}))
                caption = _get_string_val(props.get("Caption", {}))
                file_link = props.get("File (Drive Link)", {}).get("url", "") or props.get("File", {}).get("url", "") or props.get("Drive Link", {}).get("url", "") or _get_text(props.get("File (Drive Link)", {})) or _get_text(props.get("File", {}))
                
                # Parse pipe-separated values from description/Notes if present
                if not brief and desc and "|" in desc:
                    parts = [pt.strip() for pt in desc.split("|")]
                    for pt in parts:
                        pt_lower = pt.lower()
                        if pt_lower.startswith("brief:"): brief = pt[6:].strip()
                        elif pt_lower.startswith("content:"): content = pt[8:].strip()
                        elif pt_lower.startswith("idea:"): idea = pt[5:].strip()
                        elif pt_lower.startswith("scripts:") or pt_lower.startswith("script:"): scripts_copy = pt.split(":", 1)[1].strip()
                        elif pt_lower.startswith("scripts/copy:") or pt_lower.startswith("script/copy:"): scripts_copy = pt.split(":", 1)[1].strip()
                        elif pt_lower.startswith("caption:"): caption = pt[8:].strip()
                        elif pt_lower.startswith("link:"): file_link = pt[5:].strip()
                        elif pt_lower.startswith("file:"): file_link = pt[5:].strip()

                client_name_val = _get_string_val(props.get("Customer Name")) or _get_string_val(props.get("Client Name")) or _get_string_val(props.get("Client")) or _get_string_val(props.get("Brand")) or _get_string_val(props.get("Customer")) or _get_string_val(props.get("Account"))
                
                tasks.append({
                    "notion_id":   p["id"],
                    "title":        _get_text(props.get("Task", {})) or _get_text(props.get("Post Title", {})),
                    "client_name":  client_name_val,
                    "client_notion_id": _get_text(props.get("Client ID", {})),
                    "assigned_to":  _get_multi_select(props.get("Assigned To", {})),
                    "due_date":     _get_date(props.get("Due Date", {})) or _get_date(props.get("Post Day", {})),
                    "status":       _get_select(props.get("Status", {})),
                    "progress":    _get_number(props.get("Progress", {})),
                    "service":     _get_select(props.get("Task Type", {})),
                    "description":  desc,
                    "url":         p.get("url", ""),
                    "type":         _get_select(props.get("Type", {})) or _get_select(props.get("Task Type", {})),
                    "brief":        brief,
                    "content":      content,
                    "idea":         idea,
                    "scripts_copy": scripts_copy,
                    "caption":      caption,
                    "file_link":    file_link,
                    "created_time": p.get("created_time", "")
                })
            has_more = data.get("has_more", False)
            if has_more:
                payload["start_cursor"] = data.get("next_cursor")
        return tasks
    except Exception:
        logger.exception("Notion list_tasks failed")
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
        status_map = {
            "approved": "Approved",
            "blocked": "Blocked",
            "in_review": "Pending Review",
            "pending_review": "Pending Review",
            "in_progress": "In Progress",
            "done": "Done",
            "not_started": "Not Started",
            "need_to_start": "Not Started",
            "posted": "Posted",
            "final": "Final",
            "scheduled": "Scheduled",
            "paused": "Paused"
        }
        notion_status_name = status_map.get(status.lower(), status)
        props["Status"] = _select(notion_status_name)
    if progress is not None:
        props["Progress"] = _number(progress)
    if submission_note is not None:
        props["Notes"] = _text(submission_note)
    if assigned_to is not None:
        props["Assigned To"] = _multi_select(assigned_to)
    if new_title is not None:
        props["Task"] = _title(new_title)
    if due_date is not None:
        props["Due Date"] = _date(due_date)

    if not props:
        return True  # nothing to update

    try:
        _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{notion_id}",
            headers=_headers(),
            json={"properties": props},
        )

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
            except Exception:
                logger.warning("WhatsApp notification failed (non-fatal)", exc_info=True)

        return True
    except Exception:
        logger.exception(f"Notion update_task failed for {notion_id}")
        return False


def archive_notion_page(page_id: str) -> bool:
    """
    Archives a Notion page (moves it to trash). Used for deleting clients/tasks.
    """
    if not is_configured() or not page_id:
        return False
    try:
        _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=_headers(),
            json={"archived": True},
        )
        logger.info(f"Notion: archived page {page_id}")
        return True
    except Exception:
        logger.exception(f"Notion archive_notion_page failed for {page_id}")
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

    assigned_task_ids = set()
    for client in clients:
        client["tasks"] = [
            t for t in all_tasks
            if t.get("client_notion_id") == client["notion_id"]
        ]
        assigned_task_ids.update(t["notion_id"] for t in client["tasks"])
        
    unassigned_tasks = [t for t in all_tasks if t["notion_id"] not in assigned_task_ids]
    if unassigned_tasks:
        clients.append({
            "notion_id": "unassigned",
            "name": "Unassigned Tasks",
            "url": "",
            "tasks": unassigned_tasks,
            "deadline": "",
        })

    return {"configured": True, "clients": clients}
