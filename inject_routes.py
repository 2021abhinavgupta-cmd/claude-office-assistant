import os

file_path = "CLAUDE.md"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

target = """---

## Gotchas & Known Issues"""

replacement = """---

## API Route Map

A quick reference map for developers of the massive backend API.

### `app.py` (Core AI & UI Routes)
- **Chat:** `/api/chat`, `/api/conversations`, `/api/conversations/<id>`, `/api/conversations/<id>/chat`, `/api/conversations/<id>/stream`
- **Huddle:** `/api/conversations/<id>/invite`, `/api/conversations/<id>/huddle-events`
- **Projects:** `/api/projects`, `/api/projects/<id>`, `/api/projects/<id>/instructions`, `/api/projects/<id>/files`, `/api/projects/<id>/knowledge`
- **Clients:** `/api/clients`, `/api/clients/<id>`, `/api/client-users/<username>`, `/api/clients/<id>/tasks`
- **Tasks (Client):** `/api/tasks`, `/api/tasks/<id>/open`, `/api/tasks/<id>/submit`, `/api/tasks/<id>/approve`, `/api/tasks/<id>/reject`, `/api/tasks/<id>/done`
- **Skills:** `/api/skills`, `/api/skills/custom`, `/api/skills/custom/<id>`, `/api/optimize-prompt`
- **Memory:** `/api/memory/<user_id>`, `/api/memory/<user_id>/<memory_id>`
- **Export/Misc:** `/api/html/generate`, `/api/presentation`, `/api/fetch-url`, `/api/upload`
- **WhatsApp:** `/whatsapp/webhook`

### `routes/ops.py` (Standups, Operations, AI Actions)
- **Standups:** `/api/standup`, `/api/standup/today`, `/api/standup/history`, `/api/standup/my-tasks`, `/api/standup/auto-fill`, `/api/standup/carry-over`, `/api/standup/smart-add`
- **Notion Sync:** `/api/notion/status`, `/api/notion/clients`, `/api/notion/tasks`, `/api/notion/dashboard`
- **AI Analytics:** `/api/ai/breakdown`, `/api/ai/proof-of-work`, `/api/ai/coach`, `/api/ai/meeting-to-tasks`, `/api/ai/daily-summary`
- **Client Portal:** `/api/client-portal/tasks`, `/api/client-portal/tasks/<id>/feedback`
- **Alerts:** `/api/alerts`, `/api/alerts/run-check`
- **Export:** `/api/export`, `/api/export/standup-tasks`

### `routes/attendance.py` (HR)
- **Attendance:** `/api/attendance/checkin`, `/api/attendance/checkout`, `/api/attendance/summary`, `/api/attendance/today`, `/api/attendance/logs`, `/api/attendance/export`
- **Employees:** `/api/employees`, `/api/employees/checkin`, `/api/employees/summary`

### `routes/auth.py` (Security)
- **Employee Login:** `/api/auth/login`, `/api/auth/verify`, `/api/auth/logout`, `/api/auth/change_pin`
- **Client Login:** `/api/auth/client_login`, `/api/auth/client_verify`, `/api/auth/client_logout`
- **Admin Portal:** `/api/auth/admin_portal_login`, `/api/auth/admin_portal_verify`, `/api/auth/admin_portal_logout`

### `routes/system.py` (Health & Budget)
- **Monitoring:** `/api/health`, `/api/routes`
- **Budget/Usage:** `/api/budget`, `/api/usage`, `/api/usage/export`
- **Admin:** `/admin/download-db`, `/admin/upload-db`

---

## Gotchas & Known Issues"""

new_content = content.replace(target, replacement)

with open(file_path, "w", encoding="utf-8") as f:
    f.write(new_content)

print("Injected API Route Map into CLAUDE.md successfully.")
