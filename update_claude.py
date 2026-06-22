import os

file_path = "CLAUDE.md"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update Database section
db_target = """### 6. Database
- Single SQLite file at `logs/app.db`
- `db.py` calls `init_db()` and `migrate_from_json()` on every startup (safe/idempotent)
- All `ALTER TABLE ... ADD COLUMN` are wrapped in `try/except` to be non-breaking
- Always use `get_connection()` from `db.py` — never create `sqlite3.connect()` directly"""

db_replacement = """### 6. Database
- Single SQLite file at `logs/app.db`
- `db.py` calls `init_db()` and `migrate_from_json()` on every startup (safe/idempotent)
- All `ALTER TABLE ... ADD COLUMN` are wrapped in `try/except` to be non-breaking
- Always use `get_connection()` from `db.py` — never create `sqlite3.connect()` directly
- FTS5 Virtual Tables are used heavily for search (e.g. `project_knowledge` table for RAG queries). FTS tables do not support standard `ALTER TABLE` statements easily."""

content = content.replace(db_target, db_replacement)

# 2. Add FTS5 and Memory Store sections
skills_target = """- Frontend: Prompt Optimizer panel in the chat UI (`index.html` / `app.js`)."""

skills_replacement = """- Frontend: Prompt Optimizer panel in the chat UI (`index.html` / `app.js`).

### 8. FTS5 Knowledge Base & File Processing (RAG)
- Projects can have uploaded documents (PDF, DOCX, CSV, TXT, MD, etc.).
- `file_processor.py` parses these documents on upload, extracting raw text.
- Text is inserted into the `project_knowledge` SQLite FTS5 virtual table for fast full-text search.
- `kb_retriever.py` intercepts queries. When auto-tagging associates a chat with a specific `project_id`, the system performs a BM25 keyword search against `project_knowledge` using the user's query.
- The top matched snippets are injected directly into the conversation context window so Claude can answer questions based on the client's uploaded files.

### 9. Persistent User Memory
- Managed by `memory_store.py`.
- Stores unstructured facts, profiles, or rules about a user in a JSON list format inside the `memory` SQLite table. Max 50 items per user.
- Every chat stream explicitly calls `format_for_prompt()` to extract the user's latest 20 memories. FTS is NOT used here; they are simply appended to the `MASTER_SYSTEM_PROMPT`.
- Also includes `format_team_memories()` which allows the AI to reference stylistic writing traits from *other* team members if asked to "write this in Sarah's style"."""

content = content.replace(skills_target, skills_replacement)

# 3. Update Frontend API section
api_target = """### 8. Frontend API Base URL
- Defined once at the top of `app.js`:
  ```js
  const API = location.hostname === 'localhost' ? 'http://localhost:5000' : location.origin;
  ```
- All other frontend files use the same pattern inline or via `shared-config.js`"""

api_replacement = """### 10. Frontend Architecture & DOM Updates
- **Zero-Build Vanilla JS**: The app relies entirely on standard ES6 JavaScript in `app.js` (no React, Vue, or bundler).
- **DOM Mutations**: State is managed manually. Functions like `appendMessage()`, `showWelcomeScreen()`, and `renderConversations()` physically detach and re-attach HTML elements. 
- Always ensure parent containers are visible (`display: flex` or `block`) before expecting appended elements to size correctly (e.g. scrollbars on `textarea`).
- **Base API URL**: Defined once at the top of `app.js`:
  ```js
  const API = location.hostname === 'localhost' ? 'http://localhost:5000' : location.origin;
  ```
- All other frontend files use the same pattern inline or via `shared-config.js`"""

content = content.replace(api_target, api_replacement)

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("Updated CLAUDE.md successfully.")
