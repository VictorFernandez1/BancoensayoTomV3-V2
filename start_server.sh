#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# ── Check venv exists ───────────────────────────────────────────────────
if [ ! -f "integratedvenv/bin/activate" ]; then
    echo "ERROR: Virtual environment not found."
    echo "Please create it first by running:"
    echo ""
    echo "    python3 -m venv integratedvenv"
    echo "    source integratedvenv/bin/activate"
    echo "    pip install -r backend/requirements.txt"
    echo ""
    exit 1
fi

# Activate virtual environment
echo "Activating virtual environment..."
source integratedvenv/bin/activate

# ── Release port 8000 if still bound from a previous run ────────────────
echo "Releasing port 8000..."
fuser -k 8000/tcp 2>/dev/null || true

# ── Start server ──────────────────────────────────────────────────────────
echo ""
echo "Starting server at http://localhost:8005"
echo "Press Ctrl+C to stop."
echo ""
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8005
