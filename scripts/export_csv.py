"""Export the full regulatory history (or a date range) to CSV.

    python scripts/export_csv.py
    python scripts/export_csv.py --start 2026-10-12 --end 2026-10-31 --out exports/oct.csv
"""
import argparse
from datetime import date

import _bootstrap
from visa_monitor.export import export_csv

p = argparse.ArgumentParser()
p.add_argument("--start"); p.add_argument("--end")
p.add_argument("--out", default="exports/regulatory_records.csv")
a = p.parse_args()
store = _bootstrap.open_store()
path = export_csv(store, a.out,
                  date.fromisoformat(a.start) if a.start else None,
                  date.fromisoformat(a.end) if a.end else None)
print(f"Wrote {path}")
