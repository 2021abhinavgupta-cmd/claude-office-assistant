import os

markdown_content = """# CLAUDE.md — AI Coding Assistant Guide

This file is the ultimate source of truth for AI coding assistants (System, Copilot, etc.) working on this codebase. 

**Token Saving Tip for Users:** When starting a new chat, you can simply say *"Review CLAUDE.md to understand the architecture, then [do my task]"*. This gives the AI all the routing, database, and UI patterns immediately without having to manually scan the repository files.

---

## 1. Project Overview & Architecture

**Agency Portal Assistant** is a full-stack, production-deployed internal tool for a small agency (8 employees) and their external clients. It acts as an AI-powered operating system that combines:
- Intelligent chat workspace (auto-routed by task complexity between fast and reasoning models)
- Real-time multi-user chat ("Huddles") via Server-Sent Events (SSE)
- Project & client management with FTS5-powered Retrieval-Augmented Generation (RAG)
- Daily standups, personal task tracking, and attendance monitoring
- A Client Portal for external clients to view deliverables and provide feedback
- WhatsApp Bot integration (Meta Cloud API) for inbound interactions

**Deployment Structure:** 
- **Backend:** Python 3.11, Flask, Gunicorn. Hosted on Railway.
- **Frontend:** Vanilla HTML/CSS/JS. Zero build steps. Served statically by Flask.
- **Database:** Single SQLite file (`logs/app.db`) mounted on a persistent volume with WAL (Write-Ahead Logging) enabled.

---

## 2. Directory Structure

```text
claude-office-assistant/
├── backend/
│   ├── app.py                  # Main Flask app — ALL core AI + chat routes
│   ├── db.py                   # SQLite schema, migrations, WAL mode config
│   ├── conversation_store.py   # CRUD for chats (JSON blobs inside SQLite)
│   ├── memory_store.py         # Persistent cross-chat user memory logic
│   ├── project_store.py        # Project metadata & files CRUD
│   ├── kb_retriever.py         # FTS5 vectorless search for RAG
│   ├── file_processor.py       # PDF/CSV/Word parser for knowledge base
│   ├── model_router.py         # Dynamic routing between Haiku and Sonnet
│   ├── system_prompt.py        # Master system prompts + memory injection
│   ├── budget_tracker.py       # Tracks total_spent_ever across all users
│   ├── document_exporter.py    # Exports chats to DOCX/PDF
│   ├── notifications.py        # Outbound Twilio notifications
│   ├── skills.py               # Builtin AI skills (code_helper, etc.)
│   ├── custom_skills_store.py  # User-created custom skills
│   ├── notion_store.py         # Legacy Notion API integrations
│   ├── task_scheduler.py       # Background risk escalation cron jobs
│   └── routes/                 # Blueprint sub-modules
│       ├── auth.py             # Employee & Client login flows
│       ├── attendance.py       # Check-in/checkout logic
│       ├── ops.py              # Standups, tasks, Client Portal API
│       └── system.py           # Health checks, budget stats
├── frontend/
│   ├── index.html              # Main chat workspace
│   ├── app.js                  # Monolithic chat logic (2500+ lines)
│   ├── style.css               # Global CSS (dark mode, glassmorphism)
│   ├── shared-config.js        # Global configs (e.g., EMPLOYEES array)
│   ├── auth.js                 # Employee route guard (redirects to login)
│   ├── client-auth.js          # Client route guard
│   ├── dashboard.html          # Founder admin dashboard
│   ├── projects.html           # Project manager (Kanban/Spreadsheet)
│   ├── standup.html            # Daily standups
│   ├── my-tasks.html           # Personal task tracker
│   ├── client-*.html           # Client-facing portal pages
│   └── presentation.html       # AI slide builder
├── config/
│   └── employees.json          # Hardcoded employee master list (PINs)
├── logs/                       # Persistent Railway Volume
│   └── app.db                  # The single source of truth database
```

---

## 3. Database Schema (SQLite `app.db`)

The application relies completely on a local SQLite database running in WAL mode to handle high concurrency. Schema updates are non-destructive (`try/except ALTER TABLE`).

**Core Tables:**
- `conversations`: Stores chats. Uses a `data` column containing a massive JSON blob of all messages. Indexed by `user_id`.
- `memory`: Stores persistent user facts as JSON arrays. Read by `memory_store.py`.
- `custom_skills`: Stores user-defined prompts.
- `projects` & `project_files`: Client workspaces and uploaded raw files.
- `kb_chunks_fts`: **FTS5 Virtual Table**. Crucial for RAG. Stores chunked text from project files. Does *not* support standard ALTER TABLE.
- `standups` & `standup_tasks`: Tracks daily employee to-dos.
- `client_users` & `client_sessions`: External client credentials.
- `sessions`: Internal employee session tokens.

---

## 4. AI Pipeline & RAG

The core endpoint is `POST /api/conversations/<id>/stream` in `app.py`. The data flow is:

1. **Auto-Tagging:** On the first message, a background thread calls a cheap model to classify which project/client the chat belongs to.
2. **Model Routing:** `model_router.py` decides if the query needs Haiku (fast) or Sonnet (complex reasoning) based on the `task_type` or skill.
3. **Memory Injection:** `memory_store.py` fetches the last 20 memories for the user and injects them into the `MASTER_SYSTEM_PROMPT`.
4. **Knowledge Retrieval (RAG):** If the chat is tagged to a project, `kb_retriever.py` executes a BM25 keyword search against the `kb_chunks_fts` FTS5 table and injects the raw file snippets into the prompt.
5. **Skill Injection:** If a user selects a builtin skill (e.g., `web_search`), its specific prompt block is injected.
6. **Streaming:** The Anthropic API is called via streaming, yielding Server-Sent Events (SSE) back to `app.js`.

---

## 5. Task & Project Management (Social Media Logic)

Task tracking is split into two domains:
- **Employee Standups (`ops.py`)**: Personal daily tasks that auto-carry-over to the next day.
- **Client Projects (`projects.html`)**: Kanban boards for deliverables.

**The Social Media Exception:**
Social media clients receive special treatment in the UI:
- **Detection**: A client is "Social Media" if a task service is "Social" or the title contains brackets like `[Reel]`.
- **Spreadsheet View**: In `projects.html`, social clients get a spreadsheet tab allowing inline edits of `Post Day`, `Type`, `Status`, `Caption`, etc.
- **Dynamic Kanban**: Social media kanban boards have 6 columns (`Need to Start` → `Scheduled` → `In Progress` → `Paused` → `Posted` → `Final`) instead of the standard 4.
- **Standup Auto-Fill**: Social media tasks bypass the normal 7-day standup pull. They only appear in a standup if `today == Creation Date` or `today == Post Day`.

---

## 6. Auth & Security

**Employee Auth:**
- PIN-based (4 digits checked against `config/employees.json`).
- `auth.py` creates a session in the `sessions` table.
- Guarded by `<script src="auth.js"></script>`.

**Client Auth:**
- Username/Password based, checked against `client_users` table.
- Guarded by `<script src="client-auth.js"></script>`.
- **SECURITY FLAW:** Client passwords are currently stored in plaintext. `POST /api/clients` is also vulnerable to ID spoofing.

---

## 7. Frontend State Management

The frontend uses zero build tools. `app.js` is a monolithic 2500+ line file.
- **DOM Mutations:** State is not bound (no React). Functions physically detach, recreate, and re-attach elements (e.g., `appendMessage()`, `renderConversations()`).
- **Base URL:** Always references `const API = location.hostname === 'localhost' ? 'http://localhost:5000' : location.origin;`
- **Huddles (Real-time):** Huddles are chats with multiple `participant_ids`. Users receive live updates via `GET /api/conversations/<id>/huddle-events` (SSE). Un-focused users see new invites via a 5-second `setInterval` poll on `/api/conversations`.

---

## 8. API Route Map

A quick reference of major sub-blueprints:

* **`app.py`**: `/api/chat`, `/api/conversations/*`, `/api/projects/*`, `/api/skills/*`, `/api/memory/*`, `/api/html/*`, `/whatsapp/webhook`
* **`routes/auth.py`**: `/api/auth/login`, `/api/auth/client_login`, `/api/auth/logout`
* **`routes/ops.py`**: `/api/standup/*`, `/api/notion/*`, `/api/sqlite/tasks/*`, `/api/client-portal/tasks`
* **`routes/attendance.py`**: `/api/attendance/checkin`, `/api/attendance/checkout`
* **`routes/system.py`**: `/api/health`, `/api/budget`, `/api/usage`, `/api/web-search`

---

## 9. Gotchas & Known Issues

1. **Welcome Screen Initialization**: `showWelcomeScreen()` MUST be called during `DOMContentLoaded` if no specific conversation is loaded (i.e. `!convIdParam`). This ensures DOM elements like `file-chips` are correctly moved from the hidden `chat-view` to `welcome-input-wrap`. Failing to do so causes pasted images to disappear and triggers native scrollbars.
2. **Chat Interface Widths**: The main chat container elements (`.msg`, `.input-bar`, `#welcome-input-wrap`) use `max-width: 980px` to provide a wider reading area. Avoid reducing this.
3. **Usage Tracking UI**: The frontend budget tracker displays **Overall Usage** (`total_spent_ever`), not monthly usage.
4. **Multi-User Identity in Chat**: User messages display a right-aligned name above their bubbles. The assistant's avatar explicitly renders the "OPs" brand text. (Do not use "AP").
5. **Huddle Identity Bug**: When sending a message in a huddle, `sender_id` and `sender_name` must be explicitly included in the POST payload, otherwise the backend attributes the message to the conversation creator.
6. **Auto-tagging Delay**: Auto-tagging fires only on the *first* message. The UI badge appears when the `done` SSE event triggers a refresh.
7. **Standup Smart Add**: `clean_title` generation was disabled because AI altered users' specific phrasing. The system only infers the `client_name` now.
8. **Client Onboarding Context**: `client-onboard.html` saves selected workflows to `localStorage` keyed by client ID so that `add-tasks.html` can auto-recall them.
9. **White-label UI**: All emojis, "AI", "Bot", and model chip indicators were stripped to maintain a "human-built agency portal" illusion. Do not reintroduce them.
"""

with open("CLAUDE.md", "w", encoding="utf-8") as f:
    f.write(markdown_content)

print("Rewrite of CLAUDE.md complete.")
