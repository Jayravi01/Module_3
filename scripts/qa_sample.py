"""Accuracy KPI workflow.

1) Draw a random sample of detected changes to review by hand:
       python scripts/qa_sample.py sample --n 50
   -> reports/qa_sample.csv  (fill the three qa_ columns with Y/N)
2) Load the reviewed file back into the database:
       python scripts/qa_sample.py load reports/qa_sample.csv --reviewer "Sri"
Then run kpi_report.py — accuracy % is computed from these reviews.
"""
import argparse
import csv
import random
from pathlib import Path

import _bootstrap  # noqa: F401
from visa_monitor.config import Settings
from visa_monitor.storage import get_store

store = get_store(Settings())
p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="cmd", required=True)
s = sub.add_parser("sample"); s.add_argument("--n", type=int, default=50); s.add_argument("--seed", type=int, default=42)
l = sub.add_parser("load"); l.add_argument("file"); l.add_argument("--reviewer", default="reviewer")
a = p.parse_args()

if a.cmd == "sample":
    rows = [r for r in store.feed() if r["record_type"] == "change"]
    random.Random(a.seed).shuffle(rows)
    rows = rows[: a.n]
    Path("reports").mkdir(exist_ok=True)
    out = Path("reports/qa_sample.csv")
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "record_date", "source_name", "change_summary", "impact_level",
                    "visa_categories", "source_url",
                    "qa_is_real_change (Y/N)", "qa_impact_correct (Y/N)", "qa_categories_correct (Y/N)"])
        for r in rows:
            w.writerow([r["id"], r["record_date"], r["source_name"], r["change_summary"],
                        r["impact_level"], r["visa_categories"], r["source_url"], "", "", ""])
    print(f"Wrote {len(rows)} rows to {out} — open each Source Link and mark Y/N.")
else:
    yn = lambda v: None if not v.strip() else v.strip().upper().startswith("Y")
    n = 0
    with open(a.file, encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if row[0] == "id":
                continue
            store._exec("update regulatory_records set qa_is_real_change=?, qa_impact_correct=?, "
                        "qa_categories_correct=?, qa_reviewed_by=? where id=?",
                        (yn(row[7]), yn(row[8]), yn(row[9]), a.reviewer, int(row[0])))
            n += 1
    store.commit()
    print(f"Loaded {n} reviews.")
