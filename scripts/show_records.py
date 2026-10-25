"""Show the stored records and run history in the terminal.

    python scripts/show_records.py                    # summary + today's records + run log
    python scripts/show_records.py --all              # every record, all days
    python scripts/show_records.py --date 2026-10-12  # one day
    python scripts/show_records.py --changes          # only detected changes (no baselines)
    python scripts/show_records.py --impact High      # filter by impact level
    python scripts/show_records.py --id 7             # full details of one record
"""
import argparse
import shutil
import textwrap
from collections import Counter
from datetime import date, datetime

import _bootstrap  # noqa: F401
from visa_monitor.config import Settings
from visa_monitor.storage import get_store

WIDTH = shutil.get_terminal_size((120, 40)).columns


def cut(text, n):
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def table(rows, cols):
    """cols: list of (header, key, width) — the last column stretches to fill the screen."""
    fixed = sum(w for _, _, w in cols[:-1]) + 3 * (len(cols) - 1)
    cols = cols[:-1] + [(cols[-1][0], cols[-1][1], max(20, WIDTH - fixed - 1))]
    line = " | ".join(h.ljust(w) for h, _, w in cols)
    print(line)
    print("-+-".join("-" * w for _, _, w in cols))
    for r in rows:
        print(" | ".join(cut(r.get(k), w).ljust(w) for _, k, w in cols))


def heading(text):
    print(f"\n{text}\n{'=' * min(len(text), WIDTH)}")


p = argparse.ArgumentParser()
p.add_argument("--all", action="store_true", help="all records, every day")
p.add_argument("--date", help="records for one day (YYYY-MM-DD)")
p.add_argument("--changes", action="store_true", help="only changes, no baselines")
p.add_argument("--impact", choices=["High", "Medium", "Low"])
p.add_argument("--id", type=int, help="show full details of one record")
a = p.parse_args()

settings = Settings()
store = get_store(settings)
feed = store.feed()
runs = store.runs()

# ---------------------------------------------------------------- one record
if a.id is not None:
    rec = next((r for r in feed if r["id"] == a.id), None)
    if not rec:
        raise SystemExit(f"No record with id {a.id}")
    heading(f"Record {rec['id']} — {rec['title']}")
    for label, key in [("Date", "record_date"), ("Type", "record_type"), ("Source", "source_name"),
                       ("Agency", "agency"), ("Impact", "impact_level"), ("Reason", "impact_reason"),
                       ("Change type", "change_type"), ("Visa categories", "visa_categories"),
                       ("Effective date", "effective_date"), ("Health cover", "affects_health_cover"),
                       ("Classifier", "classifier"), ("Detected at", "detected_at"),
                       ("Link", "source_url")]:
        print(f"{label:>16}: {rec.get(key) if rec.get(key) not in (None, '') else '—'}")
    print(f"{'Summary':>16}:")
    print(textwrap.indent(textwrap.fill(rec["change_summary"] or "", WIDTH - 20), " " * 18))
    store.close()
    raise SystemExit

# ---------------------------------------------------------------- summary
today = datetime.now(settings.tz).date().isoformat()
heading("Module 3 — Visa & Regulatory Change Monitor")
print(f"Storage        : {settings.storage_backend}"
      + (f" ({settings.sqlite_path})" if settings.storage_backend == "sqlite" else ""))
print(f"Total records  : {len(feed)}  "
      f"(baselines {sum(r['record_type'] == 'baseline' for r in feed)}, "
      f"changes {sum(r['record_type'] == 'change' for r in feed)})")
days = sorted({r["record_date"] for r in feed})
print(f"Days with data : {len(days)}" + (f"  ({days[0]} → {days[-1]})" if days else ""))
changes = [r for r in feed if r["record_type"] == "change"]
if changes:
    print(f"Changes by impact: {dict(Counter(r['impact_level'] for r in changes))}")

# ---------------------------------------------------------------- records
rows = feed
if a.date:
    rows = [r for r in rows if r["record_date"] == a.date]
    label = f"Records on {a.date}"
elif a.all:
    label = "All records"
else:
    rows = [r for r in rows if r["record_date"] == today]
    label = f"Today's records ({today})"
if a.changes:
    rows = [r for r in rows if r["record_type"] == "change"]
    label += " — changes only"
if a.impact:
    rows = [r for r in rows if r["impact_level"] == a.impact]
    label += f" — {a.impact} impact"

heading(f"{label}: {len(rows)}")
if rows:
    table(rows, [("ID", "id", 4), ("Date", "record_date", 10), ("Type", "record_type", 8),
                 ("Impact", "impact_level", 6), ("Visa", "visa_categories", 14),
                 ("Source", "source_name", 32), ("Summary", "change_summary", 40)])
    print("\nFull details of one record:  python scripts/show_records.py --id <ID>")
else:
    print("Nothing to show." + ("" if a.date or a.all else
                                "  Run: python scripts/run_daily.py   (or use --all)"))

# ---------------------------------------------------------------- run log
heading(f"Run log (last {min(len(runs), 10)} runs)")
if runs:
    for r in runs:
        r["coverage"] = f"{r['core_sources_ok']}/{r['core_sources_total']}"
        r["finished"] = str(r["finished_at"] or "")[:16].replace("T", " ")
    table(runs[-10:], [("Run date", "run_date", 10), ("Status", "status", 8),
                       ("Checked", "sources_checked", 7), ("Failed", "sources_failed", 6),
                       ("Coverage", "coverage", 8), ("Baselines", "baselines_created", 9),
                       ("Changes", "changes_found", 7), ("Finished", "finished", 16)])
else:
    print("No runs yet.")
store.close()
