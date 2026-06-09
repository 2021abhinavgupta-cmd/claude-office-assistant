import sys
import json
import traceback

sys.path.insert(0, 'backend')
from notion_store import list_tasks, is_configured

def _shape_client_task(t: dict, source: str) -> dict:
    status = (t.get("status") or "not_started").lower().replace(" ", "_").replace("-", "_")
    progress = int(t.get("progress") or 0)
    if progress == 0:
        progress = {
            "done": 100, "approved": 100,
            "submitted": 80, "in_review": 80,
            "in_progress": 50,
            "not_started": 0, "blocked": 0,
        }.get(status, 0)
    start_raw = t.get("start_date") or t.get("created_at") or ""
    due_raw   = t.get("due_date") or ""
    def _parse_date(s):
        if not s: return None
        return s.split("T")[0] if "T" in s else s
    desc = t.get("description") or ""
    brief = t.get("brief") or ""
    idea = t.get("idea") or ""
    caption = t.get("caption") or ""
    file_link = t.get("file_link") or ""

    if not brief and desc and "|" in desc:
        parts = [p.strip() for p in desc.split("|")]
        for p in parts:
            p_lower = p.lower()
            if p_lower.startswith("brief:"): brief = p[6:].strip()
            elif p_lower.startswith("content:"): t["content"] = p[8:].strip()
            elif p_lower.startswith("idea:"): idea = p[5:].strip()
            elif p_lower.startswith("scripts:") or p_lower.startswith("script:"): t["scripts_copy"] = p.split(":", 1)[1].strip()
            elif p_lower.startswith("scripts/copy:") or p_lower.startswith("script/copy:"): t["scripts_copy"] = p.split(":", 1)[1].strip()
            elif p_lower.startswith("caption:"): caption = p[8:].strip()
            elif p_lower.startswith("link:"): file_link = p[5:].strip()
            elif p_lower.startswith("file:"): file_link = p[5:].strip()

    return {
        "id":          t.get("notion_id") or t.get("id") or "",
        "title":       t.get("title") or "Untitled Task",
        "status":      status,
        "progress":    progress,
        "assigned_to": t.get("assigned_to") or "",
        "description": desc,
        "start_date":  _parse_date(start_raw),
        "due_date":    _parse_date(due_raw),
        "client_name": t.get("client_name") or "",
        "source":      source,
        "service":     t.get("service") or "",
        "type":        t.get("type") or "",
        "brief":       brief,
        "content":     t.get("content") or "",
        "idea":        idea,
        "scripts_copy": t.get("scripts_copy") or "",
        "caption":     caption,
        "file_link":   file_link,
    }

try:
    tasks = list_tasks()
    for t in tasks:
        if 'Engagement Reel 1' in t.get('title', ''):
            shaped = _shape_client_task(t, 'notion')
            print(json.dumps(shaped, indent=2))
            break
except Exception as e:
    traceback.print_exc()
