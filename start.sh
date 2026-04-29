#!/usr/bin/env bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    echo "Creating virtualenv..."
    python3 -m venv venv
fi

./venv/bin/pip install -q -r requirements.txt

if [ ! -f "config.json" ]; then
    cp config.example.json config.json
    echo ""
    echo "  ┌─────────────────────────────────────────────────────┐"
    echo "  │  SETUP REQUIRED                                     │"
    echo "  │  Edit config.json and add your API tokens:          │"
    echo "  │    discogs_token  — discogs.com/settings/developers  │"
    echo "  │    gemini_api_key — aistudio.google.com/apikey       │"
    echo "  └─────────────────────────────────────────────────────┘"
    echo ""
fi

# Generate self-signed TLS cert if not present (required for camera access on mobile)
echo ""
echo "  Starting Vinyl Inventory internal listener..."
echo "  Bind: ${VINYL_BIND_HOST:-127.0.0.1}:${VINYL_BIND_PORT:-5003}"
echo "  Expect reverse proxy (Nginx/Caddy) to terminate TLS on 443."
echo ""
echo "  Press Ctrl+C to stop"
echo ""

./venv/bin/python app.py
