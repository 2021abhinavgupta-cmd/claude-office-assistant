#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Claude Office Assistant — Startup Script
# Run: bash start.sh
# ─────────────────────────────────────────────────────────────────────────────

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# ── 1. Warn (non-blocking) if API key not set ─────────────────────────────────
if grep -q "your_anthropic_api_key_here" "config/.env" 2>/dev/null; then
  SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null)
  echo ""
  echo "╔══════════════════════════════════════════════════════════╗"
  echo "║  ⚠️  No API key — UI works, chat will show an error     ║"
  echo "╚══════════════════════════════════════════════════════════╝"
  echo "  → Open config/.env and set:"
  echo "      ANTHROPIC_API_KEY=sk-ant-api03-YOUR-KEY"
  echo "      FLASK_SECRET_KEY=$SECRET"
  echo "  → Get key at: https://console.anthropic.com"
  echo ""
fi

# ── 2. Activate venv (create if missing) ─────────────────────────────────────
if [ ! -d "venv" ]; then
  echo "⚙️  Creating virtual environment..."
  python3 -m venv venv
fi
source venv/bin/activate
pip install -q -r backend/requirements.txt 2>/dev/null

# ── 3. Free port 5000 if in use ──────────────────────────────────────────────
if lsof -ti:5000 > /dev/null 2>&1; then
  echo "⚙️  Freeing port 5000..."
  lsof -ti:5000 | xargs kill -9 2>/dev/null || true
  sleep 1
fi

# ── 4. Start Flask ────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  ✦ Claude Office Assistant                               ║"
echo "║  🌐 Open: http://localhost:5000                          ║"
echo "║  📊 Dashboard: http://localhost:5000/dashboard.html      ║"
echo "║  Press Ctrl+C to stop                                    ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

(sleep 2 && open "http://localhost:5000" 2>/dev/null || true) &

cd backend
gunicorn app:app --bind 0.0.0.0:5000 --workers 4 --worker-class gevent
