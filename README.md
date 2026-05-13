# ✦ Claude Office Assistant

An AI-powered office assistant built on the Claude API. Multiple employees can run independent conversations, upload files, and Claude remembers important context across sessions.

---

## 🚀 How to Run

### Step 1 — Set your API key
Open `config/.env` and fill in the two required values:

```env
ANTHROPIC_API_KEY=sk-ant-api03-YOUR-KEY-HERE
FLASK_SECRET_KEY=paste-64-char-hex-here
```

Get your Anthropic API key at: **https://console.anthropic.com**

Generate a Flask secret key by running:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### Step 2 — Start the app (one command)
Windows (PowerShell):
```powershell
.\start.ps1
```

macOS/Linux:
```bash
bash start.sh
```

This will:
- Create/activate the Python virtual environment automatically
- Install all dependencies
- Start the Flask server on port 5000
- Open `http://localhost:5000` in your browser

### Manual start (alternative)
```bash
source venv/bin/activate
cd backend
python app.py
```
Then open: **http://localhost:5000**

### PowerPoint (.pptx) export fails (“No module named 'pptx'”)

The PyPI package name is **`python-pptx`**; Python imports it as **`pptx`**.

```bash
source venv/bin/activate
pip install python-pptx
# or reinstall everything:
pip install -r backend/requirements.txt
```

`GET /api/health` includes **`pptx_export_ready: true`** when the library is installed.

On first **PPT export**, if `pptx` is missing, the server tries **`pip install python-pptx`** using the **same Python** as the running process (helps when Gunicorn wasn’t started from `venv`). To disable that on locked-down hosts, set **`OFFICE_ASSISTANT_DISABLE_AUTO_PIP_PPTX=1`** in `config/.env`.

---

## 🌐 Pages

| URL | Description |
|-----|-------------|
| `http://localhost:5000` | Main chat interface |
| `http://localhost:5000/dashboard.html` | Cost & usage dashboard |
| `http://localhost:5000/html-generator.html` | AI HTML page builder |
| `http://localhost:5000/presentation.html` | Slide deck creator |

---

## ✦ Features

### Multi-User Conversations
- Each employee selects their profile on first visit (saved in browser)
- Every user gets their own set of conversations
- Conversations are persisted across page refreshes

### Real-Time Streaming
- Claude's response appears **word by word**, exactly like Claude.ai
- Blinking cursor while Claude is thinking
- Copy and Retry buttons on every response

### File Uploads
- **Images** → Claude sees them (vision API)
- **PDF, DOCX, XLSX** → text extracted and included in context
- **Code files** (`.py`, `.js`, `.ts`, etc.) → read as text
- Drag & drop files anywhere on the page
- Max 20MB per file

### Persistent Memory
- Click **🧠 Memory** in the sidebar
- Add facts Claude should always remember: `"I prefer Python"`, `"My company is X"`
- Memories are injected into every conversation automatically
- Per-user — each employee has their own memory store

### Smart Model Routing (saves cost)
| Task | Model | Why |
|------|-------|-----|
| General chat, captions, scripts | Claude Haiku | Fast & cheap |
| Coding, HTML design, presentations | Claude Sonnet | More capable |

### Budget Tracking
- Monthly cap: `$150` (configurable in `config/.env`)
- Real-time spend shown in sidebar and dashboard
- All API calls logged to `logs/usage.json`

---

## 📁 Project Structure

```
claude-office-assistant/
├── backend/
│   ├── app.py                 # Flask API server (all routes)
│   ├── conversation_store.py  # Multi-turn conversation storage
│   ├── memory_store.py        # Persistent user memory
│   ├── file_processor.py      # File content extraction
│   ├── model_router.py        # Haiku/Sonnet routing logic
│   ├── budget_tracker.py      # Monthly spend tracking
│   └── requirements.txt
├── frontend/
│   ├── index.html             # Main Claude-like chat UI
│   ├── app.js                 # Frontend logic (streaming, uploads, memory)
│   ├── style.css              # Complete design system
│   ├── dashboard.html         # Cost monitoring dashboard
│   ├── html-generator.html    # AI HTML generator
│   └── presentation.html      # Slide deck creator
├── config/
│   ├── .env                   # API keys & settings ← EDIT THIS
│   └── employees.json         # Employee list for user picker
├── logs/                      # Auto-created at runtime
│   ├── usage.json             # API call history
│   ├── conversations.json     # All conversations
│   └── memories.json          # User memories
├── whatsapp_bot/
│   └── bot.py                 # WhatsApp integration (Twilio/Interakt)
├── start.ps1                  # ← RUN THIS on Windows (PowerShell)
└── start.sh                   # ← RUN THIS on macOS/Linux
```

---

## ⚙️ Configuration (`config/.env`)

```env
# Required
ANTHROPIC_API_KEY=sk-ant-api03-...
FLASK_SECRET_KEY=<64-char hex>

# Optional
MONTHLY_BUDGET_LIMIT=150.00
FLASK_PORT=5000
LOG_LEVEL=INFO

# WhatsApp (optional)
WHATSAPP_PROVIDER=twilio
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
```

---

## 🔧 Troubleshooting

**"Invalid API key" error**
→ Set `ANTHROPIC_API_KEY` in `config/.env`

**Port 5000 in use**
```bash
lsof -ti:5000 | xargs kill -9
bash start.sh
```

Windows alternative:
```powershell
Get-NetTCPConnection -LocalPort 5000 -State Listen | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force }
.\start.ps1
```

**Module not found errors**
```bash
source venv/bin/activate
pip install -r backend/requirements.txt
```

**PDF/DOCX not reading**
```bash
source venv/bin/activate
pip install pypdf python-docx openpyxl
```
