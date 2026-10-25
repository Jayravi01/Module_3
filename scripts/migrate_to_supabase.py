"""Copy your local history (data/visa_monitor.db) into Supabase without losing anything.

    python scripts/migrate_to_supabase.py --dry-run     # show what would be copied
    python scripts/migrate_to_supabase.py               # copy it

Needs DATABASE_URL in .env (Supabase -> Connect -> "Transaction pooler" URI, port 6543).

* Creates the tables and Tableau views first (sql/001_schema.sql), so a brand-new
  Supabase project works straight away.
* Safe to run more than once: rows that are already in Supabase are skipped, never
  duplicated or overwritten (same unique keys the daily run uses).
* Source ids are matched by their `key`, so ids that differ between SQLite and
  Supabase don't matter.
* Times are converted correctly: `detected_at` was saved in Melbourne local time;
  SQLite's own defaults (scraped_at, added_at, sent_at) were saved in UTC.

After it finishes, set STORAGE_BACKEND=postgres in .env and the daily run carries on
from the same history.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401
from visa_monitor.config import Settings
from visa_monitor.storage import PostgresStore

TABLES = ["sources", "page_snapshots", "regulatory_records", "run_log", "alerts"]


def as_list(value) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    try:
        return list(json.loads(value))
    except (TypeError, ValueError):
        return [v.strip() for v in str(value).split(",") if v.strip()]


def as_bool(value):
    return None if value is None else bool(value)


def as_ts(value, assume_tz):
    """Text from SQLite -> timezone-aware datetime for Postgres."""
    if value in (None, ""):
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=assume_tz)


def rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f"select * from {table} order by 1")]


def migrate(sqlite_path: str, pg: PostgresStore, tz: ZoneInfo, dry_run: bool = False) -> dict:
    src = sqlite3.connect(sqlite_path)
    data = {t: rows(src, t) for t in TABLES}
    src.close()
    print("Local SQLite contains: " + ", ".join(f"{t}={len(v)}" for t, v in data.items()))
    if dry_run:
        print("Dry run - nothing written.")
        return {"dry_run": True, **{t: len(v) for t, v in data.items()}}

    utc = timezone.utc
    cur = pg.conn.cursor()
    added = dict.fromkeys(TABLES, 0)

    # sources -> match by key
    for s in data["sources"]:
        cur.execute(
            "insert into sources (key, name, agency, url, category, visa_hint, render, selector, "
            "priority, active, added_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict do nothing returning id",
            (s["key"], s["name"], s["agency"], s["url"], s["category"], as_list(s["visa_hint"]),
             s["render"] or "static", s["selector"], s["priority"], as_bool(s["active"]),
             as_ts(s["added_at"], utc) or datetime.now(utc)))
        added["sources"] += cur.fetchone() is not None
    cur.execute("select key, id from sources")
    pg_source = dict(cur.fetchall())
    src_map = {s["id"]: pg_source[s["key"]] for s in data["sources"] if s["key"] in pg_source}

    for p in data["page_snapshots"]:
        cur.execute(
            "insert into page_snapshots (source_id, snapshot_date, content_hash, content, "
            "http_status, fetch_ok, error, scraped_at) values (%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (source_id, snapshot_date) do nothing returning id",
            (src_map[p["source_id"]], p["snapshot_date"], p["content_hash"], p["content"],
             p["http_status"], bool(p["fetch_ok"]), p["error"], as_ts(p["scraped_at"], utc)))
        added["page_snapshots"] += cur.fetchone() is not None

    rec_map: dict[int, int] = {}
    for r in data["regulatory_records"]:
        sid = src_map[r["source_id"]]
        cur.execute(
            "insert into regulatory_records (source_id, record_date, record_type, title, "
            "change_summary, effective_date, visa_categories, change_type, impact_level, "
            "impact_reason, affects_health_cover, confidence, classifier, source_url, diff_excerpt, "
            "detected_at, qa_is_real_change, qa_impact_correct, qa_categories_correct, qa_reviewed_by) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (source_id, record_date, record_type) do nothing returning id",
            (sid, r["record_date"], r["record_type"], r["title"], r["change_summary"],
             r["effective_date"] or None, as_list(r["visa_categories"]), r["change_type"],
             r["impact_level"], r["impact_reason"], bool(r["affects_health_cover"]),
             r["confidence"], r["classifier"], r["source_url"], r["diff_excerpt"],
             as_ts(r["detected_at"], tz), as_bool(r["qa_is_real_change"]),
             as_bool(r["qa_impact_correct"]), as_bool(r["qa_categories_correct"]),
             r["qa_reviewed_by"]))
        got = cur.fetchone()
        if got:
            added["regulatory_records"] += 1
            rec_map[r["id"]] = got[0]
        else:
            cur.execute("select id from regulatory_records where source_id=%s and record_date=%s "
                        "and record_type=%s", (sid, r["record_date"], r["record_type"]))
            rec_map[r["id"]] = cur.fetchone()[0]

    for run in data["run_log"]:
        cur.execute(
            "insert into run_log (run_date, started_at, finished_at, sources_checked, sources_failed, "
            "core_sources_total, core_sources_ok, baselines_created, changes_found, alerts_sent, status) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (run_date) do nothing returning run_date",
            (run["run_date"], as_ts(run["started_at"], tz), as_ts(run["finished_at"], tz),
             run["sources_checked"], run["sources_failed"], run["core_sources_total"],
             run["core_sources_ok"], run["baselines_created"], run["changes_found"],
             run["alerts_sent"], run["status"]))
        added["run_log"] += cur.fetchone() is not None

    for a in data["alerts"]:
        rid = rec_map.get(a["record_id"])
        cur.execute("select 1 from alerts where record_id is not distinct from %s and alert_date=%s "
                    "and channel is not distinct from %s", (rid, a["alert_date"], a["channel"]))
        if cur.fetchone():
            continue
        cur.execute("insert into alerts (record_id, alert_date, impact_level, channel, sent_at, delivered) "
                    "values (%s,%s,%s,%s,%s,%s)",
                    (rid, a["alert_date"], a["impact_level"], a["channel"],
                     as_ts(a["sent_at"], utc), as_bool(a["delivered"])))
        added["alerts"] += 1

    pg.commit()
    print("Copied to Supabase (new rows): " + ", ".join(f"{t}={n}" for t, n in added.items()))

    # Verify: every local record must now exist in Supabase
    cur.execute("select count(*) from regulatory_records")
    total = cur.fetchone()[0]
    missing = len(data["regulatory_records"]) - len(rec_map)
    print(f"Supabase now holds {total} regulatory records. Missing from local copy: {missing}")
    return added


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sqlite", help="Path to the local database (default: SQLITE_PATH from .env)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    settings = Settings()
    sqlite_path = args.sqlite or settings.sqlite_path
    if not args.dry_run and not settings.database_url:
        sys.exit("DATABASE_URL is empty. Paste the Supabase Transaction pooler URI into .env first.")

    if args.dry_run and not settings.database_url:
        migrate(sqlite_path, None, settings.tz, dry_run=True)
        return
    pg = PostgresStore(settings.database_url)
    try:
        if not args.dry_run:
            pg.apply_schema(_bootstrap.ROOT / "sql" / "001_schema.sql")
            print("Schema ready (tables + Tableau views).")
        migrate(sqlite_path, pg, settings.tz, dry_run=args.dry_run)
    finally:
        pg.close()
    if not args.dry_run:
        print("\nDone. Now set STORAGE_BACKEND=postgres in .env and run scripts/run_daily.py as usual.")


if __name__ == "__main__":
    main()
