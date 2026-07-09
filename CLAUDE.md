# CLAUDE.md — AI Coding Assistant Guide

This file provides essential context for AI coding assistants (System, Copilot, etc.) working on this codebase.

** Token Saving Tip for Users:** When starting a new chat, you can simply say *"Review CLAUDE.md to understand the architecture, then [do my task]"*. This gives the AI all the routing, database, and UI patterns immediately without having to burn context tokens manually scanning the repository files.

---

## Project Overview

**Agency Portal Assistant** is a full-stack, production-deployed internal tool for a small team (8 employees). It is a System AI-powered workspace that combines:

- Multi-turn intelligent chat workspace (auto-routed by task complexity)
- Project & client management with per-project knowledge bases
- Daily standups, task tracking, and attendance
- Live "Huddle" chat (multi-user real-time via Server-Sent Events)
- Smart auto-tagging of conversations by project/client
- A client portal for external clients to log in and view their tasks
- Notion integration, document export, HTML generator, and presentation builder

**Deployment:** Railway (persistent volume at `/logs/` for the SQLite DB).  
**Live URL:** The app is accessed via the Railway-assigned URL.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, Flask, Gunicorn (gevent worker) |
| AI | Anthropic Python SDK (Standard/Fast models) |
| Frontend | Vanilla HTML/CSS/JS (zero build step) |
| Database | SQLite via `logs/app.db` (WAL mode) |
| Scheduling | APScheduler (`task_scheduler.py` — daily cron) |
| Rate limiting | Flask-Limiter (see `requirements.txt`) |
| Deployment | Railway with Nixpacks |
| Auth | Server-side session tokens (stored in SQLite `sessions` table) |

**Notable deps** (`backend/requirements.txt`): `anthropic`, `flask-cors`, `flask-compress`, `Flask-Limiter`, `apscheduler`, `gevent`, `pypdf`/`python-docx`/`openpyxl` (file parsing), `weasyprint`/`python-pptx`/`reportlab` (export). Notion integration uses raw `requests`, not an SDK.

---

## Directory Structure

```
claude-office-assistant/
├── backend/
│   ├── app.py                  # Main Flask app — ALL conversation + AI routes
│   ├── db.py                   # SQLite init, schema, migrations. Run on startup.
│   ├── conversation_store.py   # CRUD for conversations (SQLite-backed)
│   ├── memory_store.py         # Per-user AI memory (persistent profiles)
│   ├── project_store.py        # Project & knowledge base CRUD
│   ├── model_router.py         # Task-to-model routing logic (haiku vs sonnet)
│   ├── system_prompt.py        # Master system prompt + per-task prompt builder
│   ├── budget_tracker.py       # Monthly API cost tracking + budget enforcement
│   ├── file_processor.py       # PDF/CSV/Word file parsing for attachments
│   ├── kb_retriever.py         # FTS5 knowledge base retrieval (System Projects-style)
│   ├── document_exporter.py    # Export chats to DOCX/PDF
│   ├── notifications.py        # WhatsApp notifications via Twilio (outbound)
│   ├── skills.py               # Builtin skill library (8 hardcoded skills)
│   ├── custom_skills_store.py  # CRUD for user-created custom skills (SQLite)
│   ├── notion_store.py         # Full Notion API integration
│   ├── task_scheduler.py       # Background task risk escalation (APScheduler, daily 08:00)
│   ├── web_fetcher.py          # SSRF-guarded URL scraper — powers /api/fetch-url + inline link fetching in chat
│   ├── utils.py                # IST timezone helpers, atomic employees.json read/write, its own _is_admin()
│   ├── extensions.py           # Shared Flask app instance (avoids circular imports)
│   └── routes/
│       ├── __init__.py         # Empty — blueprint registration actually happens in app.py, not here
│       ├── auth.py             # Login, logout, session verify, employee list
│       ├── attendance.py       # Check-in/checkout, daily attendance records
│       ├── ops.py              # Standups, tasks, alerts, SQLite admin, exports, bet, discovery forms
│       └── system.py           # Health check, budget admin, usage stats
├── frontend/
│   ├── index.html              # MAIN CHAT UI (app entry point)
│   ├── app.js                  # All frontend logic for index.html (2200+ lines)
│   ├── kanban.js                # MISLEADING NAME: actually the Projects/KB side-panel grid for index.html — NOT a kanban board (see Gotchas)
│   ├── optimizer.js            # Prompt Optimizer panel, extracted out of app.js
│   ├── skills.js                # Builtin/custom skills menu + manager modal, extracted out of app.js
│   ├── utils.js                 # index.html-only: markdown/text rendering, code-block copy/download/preview buttons
│   ├── style.css               # All styles (dark mode, glassmorphism)
│   ├── auth.js                 # Shared auth guard (include on every protected page)
│   ├── shared-config.js        # Shared employee loader (window.EMPLOYEES)
│   ├── login.html              # Employee PIN login
│   ├── dashboard.html          # Founder analytics dashboard
│   ├── api-costing.html        # Per-model cost/usage dashboard (split out of dashboard.html)
│   ├── projects.html           # Projects & knowledge base manager
│   ├── standup.html            # Daily standup submission + team view
│   ├── my-tasks.html           # Personal task tracker with subtasks/delegation
│   ├── project.html            # Individual project page
│   ├── project.js              # JS for project.html
│   ├── presentation.html       # AI presentation builder
│   ├── html-generator.html     # AI HTML page generator
│   ├── discovery-form.html     # PUBLIC, unauthenticated client discovery questionnaire
│   ├── manage-forms.html       # Admin editor for the discovery form schema (form_templates table)
│   ├── client-forms-db.html    # Admin viewer for submitted discovery answers (discovery_submissions table)
│   ├── bet.html                 # Internal joke feature: lunch bet poll, allowlisted to 4 employee IDs
│   ├── client-login.html       # Client portal login (separate from employee login)
│   ├── client-dashboard.html   # Client portal dashboard
│   ├── client-portal.html      # Client task view
│   ├── client-admin.html       # Admin: manage client accounts
│   ├── client-auth.js          # Auth guard for client portal pages
│   ├── client-onboard.html     # New client onboarding form (3 steps: Info → Services → Review & Create). Includes Username/Password fields for client portal access. Success screen offers Done or  Add Tasks
│   └── add-tasks.html          # Apply workflow task templates to an existing client (select client → pick workflow(s) → POST to /api/clients/<id>/auto-tasks)
├── config/
│   └── employees.json          # Static employee data (id, name, pin, role, etc.)
├── logs/                       # Persistent volume — DO NOT delete
│   └── app.db                  # SQLite database (all production data lives here — the ONLY real DB file)
├── scripts/                    # Utility/migration scripts
├── requirements.txt            # Python dependencies
├── Procfile                    # Railway: web: gunicorn backend.app:app
├── nixpacks.toml               # Railway build config
└── railway.toml                # Railway deploy config
```

> [!NOTE]
> **Repo-root clutter (not part of the running app):** dozens of one-off `fix_*.py` / `test_*.py` / `patch.py` / `query_db.py` scripts, plus stray `.db` files (`app_old.db`, `backend/local.db`, `backend/database.sqlite`, etc.) accumulate at the repo root and in `backend/` from past AI-assisted debugging sessions. None are imported by the app and none are referenced anywhere in `backend/`/`frontend/` (verified by grep). **The only DB the app ever reads/writes is `logs/app.db`** via `db.py::get_connection()`. Don't mistake a root `fix_*.py` for a real module, and don't debug against a stray `.db` file — always point at `logs/app.db`. Safe to delete/gitignore these when doing cleanup.

---

## Key Patterns & Conventions

### 1. Authentication (Employee)
- Login is PIN-based (4-digit PIN in `config/employees.json`)
- `auth.py` issues a session token stored in the `sessions` table
- Every protected page includes `<script src="auth.js"></script>` which calls `/api/auth/verify` and redirects to `login.html` if invalid
- The `currentUser` object (`{user_id, user_name, role, is_admin}`) is stored in `localStorage` under `"agency_portal_user"`

### 2. Authentication (Client Portal)
- Clients log in at `client-login.html` using username + password
- Stored in `client_users` table. Separate `client_sessions` table.
- Client pages include `client-auth.js` (NOT `auth.js`)

> [!WARNING]
> **Known Security Flaws in Client Onboarding:**
> 1. **Plaintext Passwords:** Client passwords in `client_users` are currently stored and compared in plaintext (`routes/auth.py`). They need to be securely hashed.
> 2. **`POST /api/clients` (`app.py::create_client`)** actually authenticates via session cookie (`_verify_session`), not body spoofing — this warning was stale. (It *was* broken a different way until this session: missing `body = request.get_json(...)` meant every call 500'd unconditionally — now fixed.)
> 3. **Missing Auth on Notion endpoint:** `POST /api/notion/clients` (`routes/ops.py`) has completely missing authentication/authorization. Anyone can create a Notion client.
> 4. **Bet feature spoofing:** `POST /api/bet` and `POST /api/bet/question` (`routes/ops.py`) authorize by checking a client-supplied `user_id` against a hardcoded whitelist (`["emp002","emp003","emp007","emp008"]`) — zero session verification. Same spoofing pattern as #2, just lower stakes.
> 5. **Discovery form fully public, no rate limit:** `GET/POST /api/form-templates*` and `POST /api/discovery-submissions` (`routes/ops.py`) have no auth check at all. `discovery-form.html` is intentionally public (it's a client-facing intake form), but the submission endpoint can be spammed by anyone with the link.
> 6. **Inconsistent admin allowlists:** Admin-checking logic is duplicated with different employee lists in at least 4 places: `utils.py::_is_admin()`, `app.py`'s own `_is_admin()` (~line 2472), and `dashboard.html`'s `ONBOARD_ADMINS = ["emp001","emp003","emp004"]`. Adding a new admin requires updating all of them — easy to miss one and end up with inconsistent access.

### 3. AI Routing
- Task types: `general`, `coding`, `html_design`, `presentations`, `email`, `scripts`, `captions`, `meetings`, `announcements`, `analysis`, `data_analysis`, `content`, `whatsapp`
- `model_router.py` maps task type → model tier (haiku/sonnet). `whatsapp` maps to Haiku.
- The streaming endpoint `POST /api/conversations/<id>/stream` is the primary chat endpoint
- If `should_think()` returns True (complex query), the model is pinned to `claude-sonnet-4-6` with extended thinking enabled (`budget_tokens=14000`)
- Optional request body fields for the stream endpoint:
  - `skill_id` — builtin skill key (e.g. `"web_search"`) or custom skill ID prefixed `sk_`
  - `style` — `"concise"`, `"detailed"`, or `"formal"`; injects a style instruction into the system prompt
  - `webSearchEnabled` — boolean; enables web search tool (also auto-enabled when `skill_id == "web_search"`)

### 4. Conversation & Huddle System
- Conversations are stored as JSON blobs in the `conversations` SQLite table
- Each conversation has: `id`, `user_id`, `title`, `messages[]`, `task_type`, `project_id`, `participant_ids[]`, `participant_names{}`
- **Huddle** = a conversation with `len(participant_ids) > 1`
- Live SSE stream for huddles: `GET /api/conversations/<id>/huddle-events`
- Invite endpoint: `POST /api/conversations/<id>/invite` — body: `{user_id, user_name}`
- The frontend polls `loadConversations()` every 5 seconds so invited users see the new chat without refreshing

### 5. Smart Auto-Tagging
- When the **first message** of a new conversation is sent, `_auto_tag_bg()` runs in a background thread
- It calls System Haiku with a list of all projects/clients, asks which one matches the message
- If a match is found, it updates the conversation's `project_id` or `client_id` in the DB
- The frontend re-renders the tag badge automatically via the SSE `done` event

### 6. Database
- Single SQLite file at `logs/app.db`
- `db.py` calls `init_db()` and `migrate_from_json()` on every startup (safe/idempotent)
- All `ALTER TABLE ... ADD COLUMN` are wrapped in `try/except` to be non-breaking
- Always use `get_connection()` from `db.py` — never create `sqlite3.connect()` directly
- FTS5 Virtual Table for the RAG knowledge base is actually named **`kb_chunks_fts`** (columns: `project_id`, `user_id`, `doc_id`, `filename`, `chunk`; `tokenize='porter'`, ranked with `bm25()`). It is *not* called `project_knowledge` despite that name appearing in older comments/docs — don't search for a `project_knowledge` table, it doesn't exist.
- **Full table list** (`init_db()` in `db.py`): `budget`, `conversations`, `memory`, `custom_skills`, `usage_logs`, `attendance`, `daily_attendance`, `standups`, `mohit_bets` (bet feature), `app_settings` (key/value, e.g. `bet_question`), `standup_tasks`, `task_risk` (scheduler escalation), `sessions`, `client_users`, `client_dependencies`, `client_sessions`, `client_task_feedback`, `projects`, `form_templates` (discovery form schema), `client_form_answers` (per-client answers — has API endpoints but **no dedicated frontend page**, looks half-built), `discovery_submissions` (public intake submissions), `project_files`, `kb_chunks_fts`.

### 7. Skills System
- Two tiers: **builtin** (defined in `skills.py`) and **custom** (stored in `custom_skills` table, IDs prefixed `sk_`).
- Builtin skills: `web_search`, `content_writer`, `email_drafter`, `video_scripter`, `meeting_summary`, `data_analyst`, `code_helper`, `social_caption`.
- Skills inject a `"SKILL INSTRUCTION:\n"` block prepended to the system prompt during streaming.
- If a skill specifies `"model": "sonnet"` and the user hasn't set a `model_override`, the request auto-upgrades to Sonnet.
- `web_search` builtin skill also forces `webSearchEnabled = True`.
- Custom skills are per-user but can be marked `is_shared=1` to appear for all users.
- API endpoints: `GET /api/skills?user_id=` → `{builtin, custom}`, `POST /api/skills/custom` → create, `DELETE /api/skills/custom/<id>` → delete (owner only).
- Frontend: Prompt Optimizer panel lives in `frontend/optimizer.js` and the skills menu/manager lives in `frontend/skills.js` — both were extracted out of `app.js` into their own files, both loaded only by `index.html`.

### 8. FTS5 Knowledge Base & File Processing (RAG)
- Projects can have uploaded documents (PDF, DOCX, CSV, TXT, MD, etc.).
- `file_processor.py` parses these documents on upload, extracting raw text.
- Text is inserted into the `kb_chunks_fts` SQLite FTS5 virtual table for fast full-text search.
- `kb_retriever.py` intercepts queries. When auto-tagging associates a chat with a specific `project_id`, the system performs a BM25 keyword search against `kb_chunks_fts` using the user's query.
- The top matched snippets are injected directly into the conversation context window so Claude can answer questions based on the client's uploaded files.

### 9. Persistent User Memory
- Managed by `memory_store.py`.
- Stores unstructured facts, profiles, or rules about a user in a JSON list format inside the `memory` SQLite table. Max 50 items per user.
- Every chat stream explicitly calls `format_for_prompt()` to extract the user's latest 20 memories. FTS is NOT used here; they are simply appended to the `MASTER_SYSTEM_PROMPT`.
- Also includes `format_team_memories()` which allows the AI to reference stylistic writing traits from *other* team members if asked to "write this in Sarah's style".

### 10. Frontend Architecture & DOM Updates
- **Zero-Build Vanilla JS**: The app relies entirely on standard ES6 JavaScript in `app.js` (no React, Vue, or bundler).
- **DOM Mutations**: State is managed manually. Functions like `appendMessage()`, `showWelcomeScreen()`, and `renderConversations()` physically detach and re-attach HTML elements. 
- Always ensure parent containers are visible (`display: flex` or `block`) before expecting appended elements to size correctly (e.g. scrollbars on `textarea`).
- **Base API URL**: Defined once at the top of `app.js`:
  ```js
  const API = location.hostname === 'localhost' ? 'http://localhost:5000' : location.origin;
  ```
- All other frontend files use the same pattern inline or via `shared-config.js`

### 11. Discovery Questionnaire System
- Three-part feature built around a dynamic, admin-editable form schema:
  1. **`manage-forms.html`** (employee-only) edits the question schema via `GET/POST /api/form-templates[/<id>]`, stored as JSON in the `form_templates` table (default template id `discovery_global`, seeded in `db.py` with 32 questions across 10 sections).
  2. **`discovery-form.html`** is the **public, unauthenticated** questionnaire — no `auth.js`/`client-auth.js` include. It fetches the schema via `GET /api/form-templates/<form_id>` and submits via `POST /api/discovery-submissions` with `{form_id, company_name, email, answers}`.
  3. **`client-forms-db.html`** (employee-only) lists all submissions via `GET /api/discovery-submissions`.
- Separate from this is `client_form_answers` (per-client, keyed by `client_id`) with its own endpoints (`/api/clients/<id>/form-answers`) — appears unused by any frontend page, likely a half-finished parallel feature. Don't confuse it with `discovery_submissions`.

### 12. Task Risk Escalation (Scheduler)
- `task_scheduler.py::init_scheduler(app)` registers an APScheduler job that runs daily at **08:00**, plus fires once immediately on startup.
- It scans non-approved tasks (Notion or SQLite) with due dates and escalates `task_risk.risk_level` through `day1` (reminder) → `day2` (warning) → `day3` (at_risk) → `day5` (critical), tracked per-task via the `alerted_dayN` columns so each threshold only fires once.
- Fired alerts are written to `usage_logs` as `{"type": "task_alert", ...}` JSON blobs, which the founder dashboard reads to show recent risk alerts.

### 13. URL Fetching / SSRF Guard
- `backend/web_fetcher.py` powers `POST /api/fetch-url` and is also invoked inline during chat streaming when a user message contains a URL.
- `is_safe_url()` blocks requests to private/local IPs using `ipaddress.ip_address(...).is_global` before fetching — treat this as the canonical SSRF guard; any new URL-fetching feature should route through it rather than calling `requests.get()` directly.
- Fetched pages are parsed with BeautifulSoup (script/style/nav stripped) and truncated to 8000 chars before being injected into context.

---

## Environment Variables (Required)

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (required) |
| `MONTHLY_BUDGET_LIMIT` | Max USD spend per month (e.g., `50`) |
| `SECRET_KEY` | Flask secret key for sessions |
| `TWILIO_ACCOUNT_SID` | (Optional) Outbound WhatsApp notifications via Twilio |
| `TWILIO_AUTH_TOKEN` | (Optional) Outbound WhatsApp notifications via Twilio |
| `TWILIO_WHATSAPP_FROM` | (Optional) Twilio WhatsApp sender number |
| `NOTION_TOKEN` | (Optional) Notion API integration |
| `WHATSAPP_PHONE_NUMBER_ID` | (Optional) Meta Cloud API phone number ID for WhatsApp bot |
| `WHATSAPP_ACCESS_TOKEN` | (Optional) Meta Cloud API bearer token for WhatsApp bot |
| `META_VERIFY_TOKEN` | (Optional) Shared secret for Meta webhook verification handshake |

---

## Running Locally

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set environment variables in config/.env or shell
export ANTHROPIC_API_KEY=sk-ant-...

# 3. Start the backend
python -m backend.app
# OR
bash start.sh

# 4. Open the frontend
# Navigate to: http://localhost:5000/
```

The Flask app serves the `frontend/` folder as static files.

> [!NOTE]
> The default `python`/`pip` on PATH may not be this project's venv. Use `.venv/Scripts/python.exe` (Windows) explicitly for anything needing project deps (Flask, requests, etc) — a bare `python -m pyflakes ...` can silently run against an unrelated environment.

### Debugging
- `.venv/Scripts/python.exe -m pyflakes backend/*.py backend/routes/*.py` — static undefined-name check (`pip install pyflakes` first if missing). This codebase has a recurring copy-paste bug pattern: a variable set in one `if/else` branch, then referenced unconditionally afterward (or via a stray `for...else`) — silently 500s in production. Caught 4 real bugs this way in one session.

---

## Common Development Tasks

### Add a new API endpoint
Add it to `backend/app.py` (core conversation routes) or the appropriate Blueprint in `backend/routes/`:
- Auth-related → `routes/auth.py`
- Standup/tasks/alerts → `routes/ops.py`
- Attendance → `routes/attendance.py`
- Health/admin/system → `routes/system.py`

### Add a new database table
Add the `CREATE TABLE IF NOT EXISTS` statement inside `init_db()` in `backend/db.py`.

### Add a new employee
Edit `config/employees.json`. Fields: `id` (empXXX), `name`, `role`, `pin`, `department`, `status`, `whatsapp`.

### Add a new frontend page
1. Create `frontend/yourpage.html`
2. Include `<script src="auth.js"></script>` near the top of `<body>`
3. Include `<script src="shared-config.js"></script>` if you need employee data
4. Use the same `API` base URL pattern

---

## API Route Map

A quick reference map for developers of the massive backend API.

### `app.py` (Core AI & UI Routes)
- **Static:** `/`, `/<path:filename>`, `/uploads/<path:filename>`
- **Chat:** `/api/chat`, `/api/conversations`, `/api/conversations/<id>`, `/api/conversations/<id>/chat`, `/api/conversations/<id>/stream`, `/api/conversations/<id>/title` (PATCH)
- **Huddle:** `/api/conversations/<id>/invite`, `/api/conversations/<id>/huddle-events`
- **Projects:** `/api/projects`, `/api/projects/<id>` (incl. PATCH/DELETE), `/api/projects/<id>/instructions`, `/api/projects/<id>/files` (incl. DELETE `/files/<file_id>`), `/api/projects/<id>/knowledge` (incl. DELETE `/knowledge/<doc_id>`), `/api/projects/<id>/conversations`
- **Clients:** `/api/clients`, `/api/clients/<id>`, `/api/client-users/<username>`, `/api/clients/<id>/tasks`, `/api/clients/<id>/auto-tasks`
- **Tasks (Client):** `/api/tasks`, `/api/tasks/<id>` (PATCH), `/api/tasks/<id>/open`, `/api/tasks/<id>/submit`, `/api/tasks/<id>/approve`, `/api/tasks/<id>/reject`, `/api/tasks/<id>/done`
- **Skills:** `/api/skills`, `/api/skills/custom`, `/api/skills/custom/<id>`, `/api/optimize-prompt`
- **Memory:** `/api/memory/<user_id>`, `/api/memory/<user_id>/<memory_id>`
- **Export/Misc:** `/api/html/generate` (+ `/generate/stream`), `/api/html/preview`, `/api/presentation`, `/api/fetch-url`, `/api/upload`
- **Dashboard:** `/api/dashboard/founder`, `/api/blockers` (admin-only; walks `dependencies` for overdue-blocking tasks)
- **Social Media:** `/api/social-media/auto-fill` (bulk-fills empty caption/script fields via Sonnet)
- **DB Admin:** `/api/admin/backup-db`, `/api/admin/restore-db` (POST) — DB backup/restore over HTTP, verify auth before relying on these
- **WhatsApp:** `/whatsapp/webhook`

> [!WARNING]
> **Duplicate/dead route definitions in `app.py`:** `GET`/`POST /api/projects` and `GET /api/projects/<id>` are each defined **twice or three times** (~line 1441/1447/1457 vs ~2378/2385/2396 vs ~2486). Flask/Werkzeug matches whichever rule was **registered first**, so only the first block (the `project_store`-based chat Projects/KB feature) is actually live. The second `list_projects`/`create_project`/`get_project` block (~2378-2401) and the "PROJECT TRACKER ROUTES" block starting ~2486 (which reuses names like `get_projects`/`get_clients` for a *different* clients+tasks entity model) are **unreachable dead code** — but that same dead block also defines helper functions (`_pt_conn`, `_calc_progress`, `_is_admin`) that ARE used elsewhere. Before editing anything named `get_projects`/`list_projects`/`get_clients` in `app.py`, check whether it's actually reachable — it's easy to "fix" dead code by mistake.

### `routes/ops.py` (Standups, Operations, AI Actions)
- **Standups:** `/api/standup`, `/api/standup/today`, `/api/standup/history`, `/api/standup/my-tasks`, `/api/standup/auto-fill`, `/api/standup/carry-over`, `/api/standup/smart-add`
- **Notion Sync:** `/api/notion/status`, `/api/notion/clients`, `/api/notion/tasks`, `/api/notion/dashboard`
- **AI Analytics:** `/api/ai/breakdown`, `/api/ai/proof-of-work`, `/api/ai/coach`, `/api/ai/meeting-to-tasks`, `/api/ai/daily-summary`
- **Client Portal:** `/api/client-portal/tasks`, `/api/client-portal/tasks/<id>/feedback`, `/api/client-portal/dependencies/upload`, `/api/client-portal/dependencies/text`, `/api/client-portal/dependencies` (GET/DELETE)
- **Bet (internal fun feature):** `/api/bet` (GET/POST), `/api/bet/question` (POST) — see Key Patterns #12 and security warning above
- **Discovery Forms:** `/api/form-templates` (GET), `/api/form-templates/<id>` (GET/POST), `/api/clients/<id>/form-answers` (GET/POST), `/api/discovery-submissions` (GET/POST)
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

## Gotchas & Known Issues

1. **Huddle invites** — The invited user's sidebar updates via a 5-second poll (`setInterval(loadConversations, 5000)` in `app.js`). If it seems broken, check that `list_conversations()` in `conversation_store.py` is matching the invited user's ID in `participant_ids`.

2. **Auto-tagging** — Only fires on the **first** message of a conversation (`len(context) == 1`). If `project_id`/`client_id` is already set, it skips. The badge appears after the `done` SSE event triggers a `loadConversations()` refresh.

3. **SQLite on Railway** — The DB lives at `logs/app.db` on a persistent volume. Do NOT store anything important in `/tmp`. Always use the `logs/` directory.

4. **Streaming vs non-streaming** — The primary chat endpoint is `/stream` (SSE). The `/chat` endpoint is non-streaming (legacy). Both must broadcast huddle messages via `_huddle_broadcast()`.

5. **`is_huddle` in stream** — The `conversation_stream()` function extracts `participants` and `is_huddle` from the conversation object. Make sure `conv` is fetched **before** adding the new message, or the participant count will be stale.

6. **Model pinning for thinking** — `should_think()` can override the model to `claude-sonnet-4-6`. Be careful: if a user selects "haiku" via `model_override`, thinking is bypassed (correct behavior).

7. **Standup Smart Add** — When users add tasks to their daily standup, they go through `/api/standup/smart-add`. We disabled the AI from generating a `clean_title` because it was altering users' exact phrasing in frustrating ways. The system now retains the exact `title` the user typed while only using the AI to infer the `client_name`.

8. **Standup Auto-Carry-Over** — When an employee loads their tasks for today (`/api/standup/my-tasks`), if they have no tasks yet, the system automatically carries over pending tasks from their *most recent* active day (using `MAX(date)` before today). This safely handles weekends and skipped days without relying on a hardcoded "yesterday" (`timedelta(days=1)`).

9. **Client Onboarding Flow** — `client-onboard.html` creates the client record only. The success screen offers options to go to the Board or  Add Tasks. To reliably pass context to the Add Tasks screen (and prevent ID type mismatches between Notion UUIDs and SQLite IDs), `client-onboard.html` saves selected services and deadline to `localStorage` (`claude_client_services` and `claude_client_deadlines`) keyed by **both the ID and the lowercase client name**, and passes the name, ID, and deadline in the URL.

10. **Add Tasks UI (2 Steps)** — `add-tasks.html` has been streamlined into exactly 2 steps: "Select Client & Workflows" and "Add Tasks". The layout is permanently wide (`max-width: 1200px`) to accommodate the Social Media Content Calendar spreadsheet. 
    - **Service Auto-Recall**: When opened via onboarding, or when a client is manually selected, the app checks `localStorage` by client name and auto-selects their chosen workflows and target deadline.
    - **Social Media Calendar**: The "Social" workflow renders a specialized full-width HTML table for planning posts with dropdowns for Post Types (Story, Reel, Static, Carousel) and Statuses (Scheduled, In Progress, Need to Start, Posted, Paused, Final).
    - **Standard Workflows**: Tasks for other workflows are rendered as editable lists. All tasks now start completely **Unassigned** so users can skip assigning them until later.
    - The task templates are:
      - **social** (10 tasks): Client Onboarding & Setup → Brand Brief → Content Strategy formulation → Approval on Strategy → Content Ideation → Content Approval (Cal Sheet) → Discussion with Creative → Creative Assigning → Content Creation (Kanban) → Final Drive Link
      - **branding** (8 tasks): Brand Essence → Stylescape → Logo Design Presentation → Logo Iterations → Visual Style → Collateral → Brand Guidelines Content → Brand Guidelines
      - **website** (4 tasks): Concept & Flow → UI 1st Draft Review → Content → Final Build
      - **shoot** (3 tasks): Video Script & Concept → Video Shoot / Production → Video Editing & Post
      - **miscellaneous** (1 task): Custom Deliverable Setup

11. **Employee API Parsing** — The backend `/api/employees` returns an object `{"employees": [...]}`. Frontend scripts must parse this as an array of objects and convert it into a dictionary `{ id: name }` for dropdowns, otherwise `<option>` tags will render as `[object Object]`.

12. **Add Tasks Quick Action** — The ` Add Tasks` button in `dashboard.html`'s Quick Actions is visible to all users. The page itself restricts creation to admins (`ONBOARD_ADMINS = ["emp001", "emp003", "emp004"]`) and shows an "Access Restricted" message for everyone else.

13. **Project Board — Sheets View (Social Media Only)** — `projects.html` includes a per-client **Sheets** tab that is **only visible for Social Media clients**. A client is detected as social if any of its tasks has `service === "Social Media"` or a title matching `/^\[(Story|Static|Reel|Carousel|Post|Video)\]/i`. The `<div class="cb" data-social="...">` attribute stores this flag.
    - The Sheets view renders a spreadsheet-style table with columns: `#`, `Post Day`, `Post Title`, `Type`, `Brief`, `Idea`, `Caption`, `Assigned To`, `Status`, `File (Drive Link)`.
    - **All columns are editable inline**: `Post Day` uses a `<input type="date">`, `Type` and `Status` use `<select>` dropdowns, all other text cells use `contenteditable="true"`.
    - **Status Dropdown (Sheets) — 6 Social Media Options**: `Need to Start`, `Scheduled`, `In Progress`, `Paused`, `Posted`, `Final`. These replaced the old generic 4-option dropdown (`Not Started / In Progress / In Review / Approved`). Old statuses like `approved`/`done`/`not_started` are auto-mapped to their nearest social media equivalent.
    - On edit (onblur / onchange), `saveSheetRow(taskId, cellElem, clientId)` fires and sends a `PATCH` to `/api/notion/tasks/<id>` (Notion mode) or `/api/sqlite/tasks/<id>` (SQLite mode) with `{ new_title, due_date, assigned_to, status, submission_note }`.
    - After saving, `renderClientCalendar(clientId)` is called to immediately reflect changes in the Calendar tab.

14. **Project Board — Edit Button Routing** — Clicking **✏ Edit** in the calendar popup calls `openEditTask(clientId, taskId)`. For **social media clients** this navigates directly to the Sheets tab (opens the client card if collapsed, calls `switchClientTab` to `sheets`, scrolls into view). For **non-social clients** it switches to the Task List tab and highlights the task with an outline.

15. **`/api/sqlite/tasks/<id>` PATCH** — Defined in `backend/routes/ops.py` → `sqlite_patch_task()`. Accepts fields: `new_title` (→ `title`), `assigned_to`, `due_date`, `status`, `progress`, `submission_note` (→ `description`).

16. **Client Onboarding — Portal Credentials** — `client-onboard.html` step 1 includes optional **Username** and **Password** fields under a "Client Portal Access" section. When the form is submitted, these are passed in the POST body as `client_username` and `client_password`. The backend (`routes/ops.py` for Notion mode, `app.py` for SQLite mode) performs a **pre-check** for username uniqueness before creating the client, then inserts into `client_users` table. For Notion clients, the correct field is `client["notion_id"]` (NOT `client["id"]`) — using the wrong key caused a silent `KeyError` that skipped account creation.

17. **Social Media Kanban — Dynamic Columns** — Both `projects.html` (Project Board) and `client-dashboard.html` (Client Portal) now auto-detect social media clients and render **different Kanban column sets**:
    - **Default clients** (Branding / Website / Shoot): 4 columns — To Do → In Progress → In Review → Approved
    - **Social Media clients**: 6 columns —  Need to Start →  Scheduled →  In Progress → ⏸ Paused →  Posted →  Final
    - **Detection logic** (same in both files): a client is social if ANY task has `service` containing "social", OR title containing "social", OR title matching bracket-style post types `/^\[(story|reel|static|carousel|post|ig|instagram|fb|facebook|tiktok|youtube)\]/i`.
    - `STATUS_MAP` / `SOCIAL_STATUS_MAP` in `projects.html` ensures drag-and-drop saves the correct status key. `bindDragEvents(board, isSocial)` now accepts an `isSocial` flag.
    - Legacy aliases `STAGE_COLS = DEFAULT_STAGE_COLS` and `STATUS_MAP = DEFAULT_STATUS_MAP` are kept so no other code breaks.

18. **Client Portal Calendar — Month Pagination** — `client-dashboard.html` calendar now supports navigating to past and future months via **← →** arrow buttons. A global `calDate = new Date()` tracks the current view month. `calPrev()` and `calNext()` decrement/increment the month and re-render. Today's date is only highlighted when viewing the actual current month.

19. **Client Portal Calendar — Hover Status Tooltip** — Hovering over any task pill in the calendar shows a CSS-powered popup tooltip (`.cal-tip`) containing: task title (bold), status with colour coding, assigned-to, and due date. The task pill's left-border colour also reflects the status at a glance (green = Posted, cyan = Final, blue = Scheduled, purple = In Progress, orange = Paused, grey = Need to Start).

20. **Social Media Statuses — Notion Sync** — The six social media status values (`need_to_start`, `scheduled`, `in_progress`, `paused`, `posted`, `final`) must exist as valid options in the **Status** property of the **Customer Onboarding Tasks** Notion database. If the Status property is a Notion "Select" type, Notion will auto-create new options on first use. If it's a "Status" type, options must be added manually in Notion's property settings.

21. **White-label UI & AI Branding Removal** — The UI has been entirely stripped of "AI", "Bot", and "System" terminology, as well as emojis, model chips, optimizer badges, and thinking indicators, in order to look like a standard human-built agency portal. The logo is now a clean, empty orange box (all text like "AP" removed). Future edits should **not** introduce new emojis, AI-related phrasing, or branding text into the logo/UI.

22. **Huddle Real-Time Identity Fix** — When implementing multi-user huddle SSE, always ensure the sender explicitly includes `sender_id` and `sender_name` in the payload (e.g. `bodyPayload` in `sendMessage`). Otherwise, the backend defaults to the conversation creator's identity, resulting in broadcast events incorrectly attributing messages and causing duplication glitches on the sender's screen. The backend saves these identities in the `conversation_store.py` message metadata so `appendMessage` can correctly render historical messages.

23. **WhatsApp Bot (Meta Cloud API)** — An inbound WhatsApp bot distinct from the Twilio outbound notifications in `notifications.py`. Routes: `GET /whatsapp/webhook` (Meta verification handshake) and `POST /whatsapp/webhook` (receives messages). Incoming text is processed by Claude (Haiku, task type `whatsapp`) and the reply is sent via `send_whatsapp_message()` in `app.py`. Always returns HTTP 200 to Meta — Meta retries on any non-200. Requires `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, and `META_VERIFY_TOKEN` env vars.

24. **Skills — Model Auto-Upgrade** — When a skill specifies `"model": "sonnet"` in `skills.py`, the streaming handler auto-upgrades the request to Sonnet unless the user explicitly sent a `model_override`. This means selecting a skill like `content_writer` or `code_helper` silently bumps the model tier; haiku-tier requests stay haiku only if no skill is active or the skill's model is also `"haiku"`. Custom skills follow the same logic using their `model` column from `custom_skills`.
25. **Social Media Creation Dates & Standups** — For social media tasks, the "Creation Date" is natively extracted from a dedicated Notion Date property or Created Time property (bypassing the old method of parsing the text description). 
    - **Calendar View**: The task appears as a normal pill on its `Post Day` (due_date). It ALSO appears as a blue `[Start]` pill on its `Creation Date`, but ONLY if the Creation Date has arrived (i.e., `<= today`). Future creation dates are hidden from the calendar to avoid clutter.
    - **Standup Auto-Fill**: Social media tasks bypass the normal "pull if due within 7 days" logic. They are ONLY auto-filled into a user's daily standup if today's date EXACTLY matches the Creation Date (meaning the work starts today), or if they are already `in_progress`, or if they reach their actual Post Day (`due_date`). This prevents premature cluttering of standups with future social media posts.

26. **Notion People Properties & Auto-Fill** — The "Assigned To" column in Notion may be a `people` property rather than a simple `multi_select` dropdown. 
    - **Parsing**: `notion_store.py` extracts the full names from `people` properties (e.g., "Abhinav Gupta"). 
    - **Fuzzy Matching**: Since the local employee mappings (`config/employees.json`) use first names ("Abhinav"), the backend uses fuzzy substring matching (`ename.lower() in notion_name.lower()`) to safely map tasks.
    - **API Filtering Bypass**: The Notion API errors out if you attempt to use a `multi_select` filter on a `people` property. Therefore, the "Auto-Fill" button (which only requests tasks for a specific user) fetches *all* tasks from Notion and filters them entirely in Python, avoiding API query rejections.
    
27. **Notion Column Fallbacks (Title Extraction)** — When determining a task's title from Notion, `notion_store.py` checks for the following properties in order: `"Task"` -> `"Post Title"` -> `"Post"`. This ensures tasks from Social Media boards (which use "Post 17") are not skipped as "Untitled" tasks.

28. **Welcome Screen Initialization**: `showWelcomeScreen()` MUST be called during `DOMContentLoaded` if no specific conversation is loaded (i.e. `!convIdParam`). This ensures that DOM elements like `file-chips` are correctly moved from the hidden `chat-view` to `welcome-input-wrap`. Failing to do so causes pasted images on the home screen to append chips to a hidden container and can trigger native scrollbars in the empty textarea.

29. **Chat Interface Widths**: The main chat container elements (`.msg`, `.input-bar`, `#welcome-input-wrap`) use `max-width: 980px` to provide a wider reading area. Avoid reducing this to prevent the UI from feeling cramped.

30. **Usage Tracking UI**: The frontend mini-budget tracker displays **Overall Usage** (`total_spent_ever`), not monthly usage, and does not render a progress bar against a monthly limit.

31. **Multi-User Identity & Branding in Chat**: User messages display a right-aligned name above their chat bubbles to differentiate senders in multi-user huddles. The assistant's avatar explicitly renders the "OPs" brand text (do not use placeholder initials like "AP" in `appendTyping()`).

32. **Persistent Client Dependencies & File Uploads** — Client projects have a "Dependencies" section for Notes, Links, Images, Videos, and Voice Notes. These are permanently stored in the `dependencies` SQLite table. File uploads (including recorded voice notes) are saved locally to the backend `uploads/` directory using standard `werkzeug.utils.secure_filename` logic.
    - **Client Dashboard Modals:** Uploaded assets are hidden behind "Open" or "Open Folder" buttons that trigger spacious, file-explorer-style modals. Inside the modal, files are intelligently grouped by upload date categories ("Today", "Yesterday", "Last week", etc.).
    - **Specialized Views:** Drive Links bypass the grid layout and are displayed as a continuous serialized numbered list. Notes are rendered as iPhone-style full-width rounded yellow cards with proper text wrapping and timestamps.

33. **Native Voice Recorder** — The client portal includes a native browser voice recorder using `navigator.mediaDevices.getUserMedia` and the `MediaRecorder` API. It records audio chunks, compiles them into a single `audio/webm` Blob, and uploads them to the server just like a regular file attachment. It does NOT use a 3rd-party library.

34. **Daily Standup & Notion Integration** — The "Unassigned Tasks" pseudo-client generated by Notion (tasks with no assigned client) has been repurposed as "**Daily Standup Tasks**" and forced into the **Miscellaneous** tab on the Project Board. Furthermore, `/api/notion/dashboard` intercepts these Notion tasks and cross-references them with the local SQLite `standup_tasks` table:
    - If a task is marked `done` in the local Standup DB, it is **hidden/filtered out** from the Project Board entirely.
    - If a task has `subtasks` defined in the Standup DB (stored as a JSON string), they are attached to the task object and beautifully rendered as a checklist directly on the Kanban task card in `projects.html`.

35. **Mobile Optimization** — The client dashboard (`client-dashboard.html`) and associated portal modals have been thoroughly optimized for mobile screens. This includes flexible wrap layouts, stacked modals, responsive calendar grids, and hamburger/scrollable tab menus, ensuring external clients have a flawless experience on their phones.

36. **SQLite Datetime Parsing in JavaScript** — The server returns dates in standard SQL format (`YYYY-MM-DD HH:MM:SS`). When parsing these into native JavaScript `Date` objects on the frontend using `new Date(dateStr + 'Z')`, some browsers (Safari/Chrome) may throw an "Invalid Date" error. Always convert the space to a `T` first to strictly conform to the ISO standard: `new Date(dateStr.replace(' ', 'T') + 'Z')`.

37. **`kanban.js` is misleadingly named** — Despite the filename, it contains the **Projects/knowledge-base grid** for `index.html`'s side panel (`loadProjects`, `renderAllProjectsGrid`, `showCreateProjectModal`, `renderProjectKb`). It is unrelated to the actual Kanban board UI, which lives inline inside `projects.html` and `client-dashboard.html` (see #17 above). Don't go looking for the board's drag-and-drop code here.

38. **One-off patch scripts masquerading as app files** — `frontend/api-costing-edit.py` and `frontend/dashboard-edit.py` are not part of the running app: they're scripts that were run once to regex-rewrite `api-costing.html` and `dashboard.html` (splitting the cost dashboard out of the founder dashboard), have no Flask route, and aren't imported anywhere. Same story for root-level `temp.js` and `temp_standup.html` — zero references anywhere in `backend/`/`frontend/`, they're orphaned drafts, not served by Flask. See also the repo-root clutter note under Directory Structure.

39. **Admin allowlists are duplicated and inconsistent** — See security warning #6 above. When adding a new admin employee, you must update `utils.py::_is_admin()`, `app.py`'s local `_is_admin()`, and `dashboard.html`'s `ONBOARD_ADMINS` separately — there is no single source of truth.

40. **Discovery form vs. client form-answers — don't conflate** — `discovery_submissions` (public intake, no `client_id`) and `client_form_answers` (per-client, keyed by `client_id`) are two separate tables/features that look superficially similar. Only the former has frontend pages (`discovery-form.html`, `client-forms-db.html`); the latter's endpoints (`/api/clients/<id>/form-answers`) have no UI consumer found — treat it as unfinished, not broken, if asked to "fix the client form answers page" (there isn't one yet).

41. **`auto_fill_standup()` (`routes/ops.py`) is fragile — check variable scope before editing.** Had 3 separate `NameError` bugs where `target_uids`/`insert_allowed`/`names` were set inside `if sync_all: ... else: ...` but then referenced unconditionally afterward (once via a stray `for...else` instead of `if...else`), silently 500ing every "Sync All Tasks" / personal "Auto-Fill" click. Fixed, but the pattern is easy to reintroduce here.

42. **Assigning a task in Sheets/Kanban directly inserts into `standup_tasks`** — `PATCH /api/notion/tasks/<id>` (`notion_update_task()`, `routes/ops.py` ~1303) auto-inserts a `pending` row into the assignee's standup for today the moment `assigned_to` changes, *if* the Notion write succeeds. This is independent of the `/api/standup/auto-fill` pull-based flow. If a task doesn't show up after assigning, check whether the Notion PATCH actually succeeded before assuming Auto-Fill is broken.

43. **Notion "Assigned To" property type varies by workspace — don't hardcode `_multi_select()`.** `notion_store.py::update_task()` writes it via `_assigned_to_prop()`, which reads the live Tasks DB schema (`_get_assigned_to_prop_type()`, cached 5min) and builds a `people`/`select`/`multi_select` payload to match. Notion rejects the **entire** PATCH if any one property's shape is wrong — a bad assumption here used to silently kill status/title/due_date updates bundled in the same request too.

44. **`auto_fill_standup()` — watch for variables assigned inside a per-task `if matched_id:` block then read again outside it.** A stray `matched_id` reference after the loop used a stale value from a prior iteration, silently marking the wrong row `done` (or crashing when nothing had matched yet). Same family of bug as #41 — this file has a repeated tendency to leak loop-local state past its scope. Before editing any conditional block inside the finished-tasks/pending-tasks loops here, trace every variable back to where it was last assigned.

45. **`notion_update_task()` "Untitled Task" bug — Sheets checkbox assign-only edits never send a title.** `saveSheetRow()` (`projects.html`) sends a **diff-only** PATCH body: if the user only toggled an assignee checkbox, `new_title`/`task_title` are omitted entirely. The backend used to hard-fallback to the literal string `"Untitled Task"` in that case. Fixed by adding `notion_store.get_task_summary(notion_id)`, which fetches the live Notion page and builds `"{client_name} — {title} ({content preview})"` when the request didn't supply a title. Any other endpoint that accepts a diff-only payload from `saveSheetRow()` should assume `new_title` may be absent and NOT treat that as "task has no title" — fetch it from Notion instead of hardcoding a placeholder.

46. **Project Board header nav** — `projects.html`'s header no longer has a "Paste Meeting Notes" button; it was replaced with a "Daily Standup" link (`<a href="standup.html">`) per user request. The `openMeetingNotesModal()` function and its `/api/ai/meeting-to-tasks` call are still defined in the file but now unreferenced — dead code, left in place intentionally (not deleted) in case the meeting-notes-to-tasks feature needs to be re-surfaced elsewhere later.
