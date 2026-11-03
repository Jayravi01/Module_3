"""LIVE daily run: scrape the government sources, store today's records,
then export the CSVs automatically.

    python scripts/run_daily.py

Stores the history in AWS RDS (schema visa) when DB_HOST is set in .env, opening the
SSH tunnel by itself; otherwise in the local SQLite file data/visa_monitor.db.

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
from visa_monitor.db import config_from_env, describe, open_store
from visa_monitor.env import schema_name
from visa_monitor.export import export_after_run
from visa_monitor.pipeline import run_daily

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
db_config = config_from_env() if settings.storage_backend == "postgres" else None
where = (f"AWS RDS {describe(db_config)}, schema {schema_name()}" if db_config
         else f"local SQLite {settings.sqlite_path}" if settings.storage_backend != "postgres"
         else "AWS RDS (not configured: DB_HOST is empty in .env)")
logging.info("Storage: %s", where)
with open_store(settings) as store:
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
