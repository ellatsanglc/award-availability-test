#!/bin/bash
# Asia Miles Flight Finder — start script

set -e

cd "$(dirname "$0")/backend"

# Install Python dependencies (first run only, skipped when already installed)
if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "📦 Installing Python dependencies..."
  pip3 install -r requirements.txt
fi

# Install Playwright browser (first run only)
if ! python3 -c "from playwright.sync_api import sync_playwright; sync_playwright().__enter__().chromium.executable_path" 2>/dev/null; then
  echo "🌐 Installing Playwright Chromium..."
  python3 -m playwright install chromium
fi

echo ""
echo "✅ Starting Asia Miles Flight Finder"
echo "   Open in your browser: http://localhost:8000"
echo "   Press Ctrl+C to stop"
echo ""

python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
