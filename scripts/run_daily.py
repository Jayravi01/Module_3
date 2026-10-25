"""LIVE daily run: scrape the government sources, store today's records,
then export the CSVs automatically.

    python scripts/run_daily.py

Writes:
    exports/regulatory_records.csv   full history (every day so far)
    exports/daily/<today>.csv        today's records only
    exports/run_log.csv              one row per run
Schedule it once a day (see README "Run it every day").
"""
import argparse
import logging
from datetime import date
from pathlib import Path

import _bootstrap  # noqa: F401
from visa_monitor.config import Settings
from visa_monitor.export import export_after_run
from visa_monitor.pipeline import run_daily
from visa_monitor.storage import get_store

p = argparse.ArgumentParser()
p.add_argument("--out", default=str(_bootstrap.ROOT / "exports"), help="Folder for CSV exports")
p.add_argument("--date", help="Backfill/override run date (YYYY-MM-DD); normally leave blank")
args = p.parse_args()

log_dir = _bootstrap.ROOT / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.StreamHandler(),
                              logging.FileHandler(log_dir / "monitor.log", encoding="utf-8")])

settings = Settings()
store = get_store(settings)
try:
    summary = run_daily(settings=settings, store=store,
                        run_date=date.fromisoformat(args.date) if args.date else None)
    files = export_after_run(store, date.fromisoformat(summary["run_date"]), args.out)
    print("\nThis run:", summary)
    total = store.feed()
    today = [r for r in total if r["record_date"] == summary["run_date"]]
    print(f"Stored so far: {len(total)} records in total, {len(today)} dated {summary['run_date']} "
          "(a re-run on the same day skips sources already captured)")
    print("CSV exports:")
    for k, v in files.items():
        print(f"  {k:<13} {Path(v).resolve()}")
finally:
    store.close()
