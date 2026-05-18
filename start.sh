#!/bin/bash
# Asia Miles Flight Finder — start script (local + ngrok)

cd "$(dirname "$0")/backend"

# Load .env
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# ── Check ngrok ──────────────────────────────────────────────
if ! command -v ngrok &>/dev/null; then
  echo ""
  echo "❌  ngrok not installed. Install it first:"
  echo "    1. Go to https://ngrok.com/download"
  echo "    2. Download the Mac (Apple Silicon or Intel) zip"
  echo "    3. Unzip and move ngrok to /usr/local/bin/ngrok"
  echo "    4. Run this script again"
  echo ""
  exit 1
fi

# ── Configure ngrok auth token (one-time, safe to re-run) ───
if [ -n "$NGROK_AUTHTOKEN" ]; then
  ngrok config add-authtoken "$NGROK_AUTHTOKEN" 2>/dev/null
fi

# ── Install Python deps (first run only) ─────────────────────
if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "📦 Installing Python dependencies..."
  pip3 install -r requirements.txt
fi

# ── Install Playwright browser (first run only) ──────────────
if ! python3 -c "from playwright.sync_api import sync_playwright; sync_playwright().__enter__().chromium.executable_path" 2>/dev/null; then
  echo "🌐 Installing Playwright Chromium..."
  python3 -m playwright install chromium
fi

# ── Start FastAPI backend ────────────────────────────────────
echo ""
echo "🚀 Starting backend on http://localhost:8000 ..."
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000 &
UVICORN_PID=$!

# Give uvicorn a moment to bind
sleep 2

# ── Start ngrok tunnel ───────────────────────────────────────
echo "🌐 Starting ngrok tunnel..."
if [ -n "$NGROK_DOMAIN" ]; then
  ngrok http --domain="$NGROK_DOMAIN" 8000 > /tmp/ngrok.log 2>&1 &
else
  ngrok http 8000 > /tmp/ngrok.log 2>&1 &
fi
NGROK_PID=$!

sleep 3  # Give ngrok time to connect

# ── Show public URL ──────────────────────────────────────────
if [ -n "$NGROK_DOMAIN" ]; then
  PUBLIC_URL="https://$NGROK_DOMAIN"
else
  PUBLIC_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'])" 2>/dev/null \
    || echo "(check http://localhost:4040 for URL)")
fi

echo ""
echo "✅ Asia Miles Flight Finder is running!"
echo ""
echo "   🌐 Public URL : $PUBLIC_URL"
echo "   💻 Local only : http://localhost:8000"
echo ""
echo "   Press Ctrl+C to stop everything"
echo ""

# ── Cleanup on exit ──────────────────────────────────────────
cleanup() {
  echo ""
  echo "Stopping backend and ngrok..."
  kill $UVICORN_PID 2>/dev/null || true
  kill $NGROK_PID 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

wait
