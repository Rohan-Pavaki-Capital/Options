#!/usr/bin/env bash
# One-command installer: install Python deps and create NeonDB tables.
set -e

# Run from the repo root (this file lives in scripts/)
cd "$(dirname "$0")/.."

echo "=== Installing Python dependencies ==="
pip install -r requirements.txt

echo
echo "=== Creating tables in NeonDB ==="
python -m database.setup

echo
echo "Setup complete. Run extractions with: python -m core.options <pdf_path>"
