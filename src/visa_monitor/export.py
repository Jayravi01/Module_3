"""Direct CSV export of every record — no approval step, as the client requested.

The CSV is a full history (one row per baseline/change record), so a file
exported tomorrow still contains today's rows. It doubles as a Tableau data
source when a live database connection isn't available.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

from .storage import FEED_COLUMNS, BaseStore

EXPORT_HEADERS = {
    "record_date": "Record Date", "record_type": "Record Type", "title": "Title",
    "change_summary": "Change Summary", "effective_date": "Effective Date",
    "visa_categories": "Visa Categories Affected", "change_type": "Change Type",
    "impact_level": "Impact Level", "impact_reason": "Impact Assessment",
    "affects_health_cover": "Affects Health Cover", "confidence": "Confidence",
    "classifier": "Classifier", "source_name": "Source", "agency": "Agency",
    "source_category": "Source Category", "source_url": "Source Link",
    "detected_at": "Detected At", "id": "Record ID",
}


def records_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[EXPORT_HEADERS[c] for c in FEED_COLUMNS])
    writer.writeheader()
    for r in rows:
        writer.writerow({EXPORT_HEADERS[c]: r.get(c) for c in FEED_COLUMNS})
    return buf.getvalue()


def export_csv(store: BaseStore, path: str | Path,
               start: date | None = None, end: date | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(records_to_csv(store.feed(start, end)), encoding="utf-8-sig")  # BOM: opens cleanly in Excel
    return path


def export_after_run(store: BaseStore, run_date: date, out_dir: str | Path = "exports") -> dict:
    """Written automatically after every live run:
    - regulatory_records.csv      full history, all days (overwritten with the latest full copy)
    - daily/<YYYY-MM-DD>.csv      only that day's baselines/changes (kept forever)
    - run_log.csv                 one row per daily run (proof the monitor ran)
    """
    out = Path(out_dir)
    full = export_csv(store, out / "regulatory_records.csv")
    day_file = export_csv(store, out / "daily" / f"{run_date.isoformat()}.csv", run_date, run_date)
    runs = store.runs()
    log_path = out / "run_log.csv"
    if runs:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(runs[0]))
        w.writeheader()
        w.writerows(runs)
        log_path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return {"full_history": str(full), "today": str(day_file), "run_log": str(log_path)}
