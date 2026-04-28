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
if [ ! -f "cert.pem" ] || [ ! -f "key.pem" ]; then
    echo "  Generating self-signed TLS certificate for HTTPS..."
    ./venv/bin/python generate_cert.py
fi

PI_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "  Starting Vinyl Inventory (HTTPS)..."
echo "  Open on your phone: https://${PI_IP}:5001"
echo ""
echo "  First visit: tap 'Advanced' → 'Proceed to vinyl-inventory (unsafe)'"
echo "  Then install via browser menu → 'Add to Home Screen'"
echo ""
echo "  Press Ctrl+C to stop"
echo ""

./venv/bin/python app.py
