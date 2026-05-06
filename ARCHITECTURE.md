# Claude Office Assistant - Architecture & Project Overview

## Project Summary
Claude Office Assistant is a lightweight, production-ready web application built to replicate the core experience of the official Anthropic Claude UI (claude.ai). It offers multi-turn chat streaming, advanced layout features (Artifacts, Message Editing), model auto-routing, and persistent volume storage, deployed natively on Railway.

## Technology Stack
- **Backend:** Python (Flask), Flask-CORS, Gunicorn
- **AI Integration:** Anthropic Python SDK (streaming mode)
- **Frontend:** Vanilla HTML/CSS/JS (Zero build step, lightweight)
- **Deployment:** Railway (Containerized via Python Nixpacks)
- **Persistence:** Local JSON file stores mapped to persistent Railway Volumes

## Core Architecture

### 1. Model Routing Engine (`backend/model_router.py`)
To optimize token costs and speed, the application dynamically analyzes the user's prompt to determine complexity:
- **Simple Tasks:** (Greetings, general questions, summaries) are routed to `claude-haiku-4-5-20251001` for rapid, cost-effective responses.
- **Complex Tasks:** (Coding, deep analysis, math) are automatically routed to `claude-sonnet-4-6` for high reasoning capability.

### 2. Frontend Application (`frontend/app.js` & `style.css`)
The UI mimics the sleek dark-mode aesthetic of modern AI applications.
- **Streaming Pipeline:** Uses `fetch` and `ReadableStream` to process Server-Sent Events (SSE) yielded by Flask in real-time.
- **Message Editing:** Users can click the Edit pencil to overwrite a past message. The app issues a command to truncate the backend history from that point forward and forks a new conversational branch.
- **Artifacts:** Code blocks generating HTML/SVG can be rendered via a "Preview" button, which toggles a split-screen iframe view, isolating the generated code securely.

### 3. Backend Routing (`backend/app.py`)
A modular Flask application managing:
- `/api/conversations`: Chat creation and history retrieval.
- `/api/conversations/<id>/stream`: The primary chat generation endpoint yielding Anthropic text streams.
- `/api/projects`: Logic for creating "Knowledge Bases" allowing users to attach persistent contextual files.

### 4. Data Persistence & File Processing
- **Conversations & Memory:** Stored in lightweight local JSON files (`logs/conversations.json`, `logs/memory.json`) secured with threading locks.
- **Budget Tracking:** Monitors daily/monthly API consumption and blocks requests if the predefined `MONTHLY_BUDGET_LIMIT` is exceeded.
- **File Parsing:** Supported via `backend/file_processor.py`. Parses text from PDFs, CSVs, and Word Documents to inject into the model's context window.

## Current State & Potential Improvements
Currently, the system is fully operational and bug-free, but as usage scales, the following areas could be improved:

1. **Database Migration:** Migrating from JSON file locking to SQLite or PostgreSQL to handle high-concurrency requests safely.
2. **Conversation Forking UI:** While the backend supports branch truncation, adding a UI element to switch between multiple branches of the same conversation (e.g., `< 2/3 >`) would enhance UX.
3. **Advanced File Context:** Implementing vector embeddings (RAG) for large uploaded documents rather than stuffing the entire text into the context window.
4. **Backend Refactoring:** Splitting the monolithic `app.py` (>1,000 lines) into Flask Blueprints (e.g., `routes/chat.py`, `routes/admin.py`) for easier maintainability.
