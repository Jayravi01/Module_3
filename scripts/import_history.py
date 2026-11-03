r"""Import the October 2026 history (Jayravi's last CSV export) into the database.

    python scripts\import_history.py            # preview only: shows what would be imported
    python scripts\import_history.py --apply    # write it

Why: the AWS database started fresh on 3 Nov 2026. Jayravi's earlier runs (baselines of
12 Oct, 4 changes found on 29 Oct, and their run-log rows) exist only in the CSV files she
exported, which are kept in Git history (commit 1a657a8). This script reads them straight
from Git, so nothing has to be copied by hand.

What it does to each row:
  * text cleaned: invisible zero-width characters and repeated spaces removed;
  * the CURRENT rules applied (same as new data): baselines get the page's own visa and type
    'baseline'; every row gets its basis (stated / inferred / none);
  * impact, summary, dates and classifier ('rules') are kept as recorded.
Safe to run again: a record that already exists (same source, date and type) and a run date
that is already in run_log are skipped, never overwritten. Page snapshots were not exported,
so the imported changes cannot be re-analysed.
"""
import argparse
import csv
import io
import json
import re
import subprocess
from datetime import date, datetime
from zoneinfo import ZoneInfo

import _bootstrap
from visa_monitor.classifier import RegulatoryRecord, apply_basis

COMMIT = "1a657a8"
MELBOURNE = ZoneInfo("Australia/Melbourne")
INVISIBLE = re.compile(r"[​‌‍⁠﻿]")


def from_git(commit: str, path: str) -> str:
    out = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=_bootstrap.ROOT,
                         capture_output=True, check=True)
    return out.stdout.decode("utf-8-sig")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", INVISIBLE.sub("", text or "")).strip()


def as_list(value):
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else json.loads(value or "[]")


def plan(store, records_csv: str, runs_csv: str):
    """(records to insert, runs to insert, skipped messages). Nothing is written here."""
    sources = {name: (sid, as_list(hint)) for sid, name, hint in
               store.query("select id, name, visa_hint from sources")}
    existing = {(sid, str(d), t) for sid, d, t in
                store.query("select source_id, record_date, record_type from regulatory_records")}
    records, skipped = [], []
    for row in csv.DictReader(io.StringIO(records_csv)):
        name = row["Source"]
        if name not in sources:
            skipped.append(f"source not in config: {name}")
            continue
        sid, hints = sources[name]
        day = date.fromisoformat(row["Record Date"])
        kind = row["Record Type"]
        if (sid, day.isoformat(), kind) in existing:
            skipped.append(f"already stored: {name} {day} {kind}")
            continue
        summary = clean(row["Change Summary"])
        eff = row.get("Effective Date") or ""
        rec = RegulatoryRecord(
            title=clean(row["Title"])[:200] or "Imported record",
            change_summary=summary,
            effective_date=date.fromisoformat(eff[:10]) if eff else None,
            visa_categories=[v.strip() for v in (row["Visa Categories Affected"] or "").split(",") if v.strip()],
            change_type=row["Change Type"] or "other",
            impact_level=row["Impact Level"] or "Low",
            impact_reason=clean(row["Impact Assessment"]),
            affects_health_cover=row["Affects Health Cover"].strip().lower() == "true",
            confidence=float(row["Confidence"] or 0.4),
            classifier=row["Classifier"] or "rules")
        rec = apply_basis(rec, kind, summary, {"visa_hint": hints})
        detected = datetime.fromisoformat(row["Detected At"]).replace(tzinfo=MELBOURNE)
        records.append((sid, day, kind, rec, row["Source Link"], detected, name))
    stored_runs = {str(r[0]) for r in store.query("select run_date from run_log")}
    runs = []
    for row in csv.DictReader(io.StringIO(runs_csv)):
        if row["run_date"] in stored_runs:
            skipped.append(f"run already stored: {row['run_date']}")
            continue
        runs.append({k: (int(v) if k not in ("run_date", "started_at", "finished_at", "status") else v)
                     for k, v in row.items()})
    return records, runs, skipped


def main():
    parser = argparse.ArgumentParser(description="Import Jayravi's October CSV history into the database.")
    parser.add_argument("--apply", action="store_true", help="write the rows (default: preview only)")
    parser.add_argument("--commit", default=COMMIT, help=f"Git commit holding the CSVs (default {COMMIT})")
    args = parser.parse_args()
    store = _bootstrap.open_store()
    records, runs, skipped = plan(store, from_git(args.commit, "exports/regulatory_records.csv"),
                                  from_git(args.commit, "exports/run_log.csv"))
    print(f"To import: {len(records)} record(s), {len(runs)} run(s)"
          + ("" if args.apply else " (preview only, nothing written)"))
    for _sid, day, kind, rec, _url, _det, name in records:
        print(f"  {day} {kind:<8} {rec.impact_level:<6} {name[:40]:<40} visa {', '.join(rec.visa_categories) or 'General'}"
              f" ({rec.visa_basis})")
    for run in runs:
        print(f"  run {run['run_date']}: {run['sources_checked']} sources, {run['changes_found']} changes")
    for message in skipped:
        print(f"  skipped - {message}")
    if args.apply and (records or runs):
        for sid, day, kind, rec, url, detected, _name in records:
            store.insert_record(sid, day, kind, rec, url, None, detected)
        for run in runs:
            store.log_run(run)
        store.commit()
        print(f"Imported {len(records)} record(s) and {len(runs)} run(s). Run again: it should import 0.")


if __name__ == "__main__":
    main()
