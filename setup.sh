#!/usr/bin/env bash
# One-command installer: install Python deps and create NeonDB tables.
set -e

echo "=== Installing Python dependencies ==="
pip install -r requirements.txt

echo
echo "=== Creating tables in NeonDB ==="
python -m database.setup

echo
echo "Setup complete. Run extractions with: python options.py <pdf_path>"
