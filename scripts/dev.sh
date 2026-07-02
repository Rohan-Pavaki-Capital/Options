#!/usr/bin/env bash
# Single-command dev launcher: starts FastAPI backend + Vite frontend in
# parallel. Streams both logs to this terminal; Ctrl-C stops both.
#
# Usage:  ./dev.sh
#
# Open the frontend at  http://localhost:5173
# Open API docs at      http://localhost:8000/docs

set -e

# Run from the repo root (this file lives in scripts/)
cd "$(dirname "$0")/.."

trap 'kill $(jobs -p) 2>/dev/null || true' EXIT

echo "Starting FastAPI backend on http://localhost:8000 ..."
uvicorn backend:app --reload --port 8000 &

echo "Starting Vite frontend on http://localhost:5173 ..."
(cd Frontend && npm install && npm run dev) &

wait
