#!/usr/bin/env bash
# macOS / Linux: run the live monitor (use from cron for daily runs).
cd "$(dirname "$0")"
source .venv/bin/activate
python scripts/run_daily.py
