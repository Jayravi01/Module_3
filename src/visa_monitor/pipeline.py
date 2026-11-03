"""Daily run: scrape -> snapshot -> baseline or diff -> classify -> store -> alert.

    Day 1 for a source  : no earlier snapshot  -> 'baseline' record
    Later days, same hash: nothing new          -> history untouched
    Later days, new hash : diff vs last good day -> 'change' record (+ alert if >= ALERT_MIN_LEVEL)

Re-running on the same day is safe: sources already captured today are skipped.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from .alerts import should_alert
from .classifier import Classifier
from .config import Settings, load_sources
from .diff import diff_pages
from .scraper import Scraper
from .storage import BaseStore, SQLiteStore

log = logging.getLogger(__name__)


def melbourne_today(settings: Settings) -> date:
    """The run date is the Melbourne calendar date, whatever the computer's time zone."""
    return datetime.now(settings.tz).date()


def run_daily(settings: Optional[Settings] = None, store: Optional[BaseStore] = None,
              scraper: Optional[Scraper] = None, classifier: Optional[Classifier] = None,
              run_date: Optional[date] = None, sources: Optional[list[dict]] = None) -> dict:
    settings = settings or Settings()
    store = store or SQLiteStore(settings.sqlite_path)
    scraper = scraper or Scraper(settings)
    classifier = classifier or Classifier(settings)
    day = run_date or melbourne_today(settings)
    sources = sources if sources is not None else load_sources(settings.sources_file)

    started = datetime.now(settings.tz)
    ids = store.sync_sources(sources)
    stats = dict(checked=0, failed=0, core_total=0, core_ok=0, baselines=0, changes=0, alerts=0)
    # For the scheduler (daily.py): which records need an alert email, which sources failed,
    # which worked. The pipeline itself never sends email, so a manual run_daily.py stays quiet.
    alert_ids: list[int] = []
    failures: list[dict] = []
    ok_keys: list[str] = []
    log.info("Run %s: %d sources, classifier=%s", day, len(sources),
             settings.openai_model if classifier.uses_llm else "rules")

    for src in sources:
        sid = ids[src["key"]]
        core = src.get("priority", 1) == 1
        stats["checked"] += 1
        stats["core_total"] += core

        if store.snapshot_ok_today(sid, day):           # idempotent re-run
            stats["core_ok"] += core
            ok_keys.append(src["key"])
            continue

        fetch = scraper.fetch(src)
        store.save_snapshot(sid, day, fetch)
        if not fetch.ok:
            stats["failed"] += 1
            log.warning("  ✗ %s — %s", src["key"], fetch.error)
            failures.append({"key": src["key"], "name": src["name"], "url": src["url"],
                             "status": fetch.status, "error": fetch.error})
            store.commit()
            continue
        stats["core_ok"] += core
        ok_keys.append(src["key"])

        previous = store.previous_snapshot(sid, day)
        if previous is None:
            rec = classifier.summarise_baseline(fetch.text, src)
            if store.insert_record(sid, day, "baseline", rec, src["url"], None,
                                   datetime.now(settings.tz)):
                stats["baselines"] += 1
            log.info("  ● %s — baseline stored", src["key"])
        elif previous[0] == fetch.content_hash:
            log.info("  = %s — no change", src["key"])
        else:
            diff = diff_pages(previous[1] or "", fetch.text, settings.llm_max_chars)
            if diff.is_trivial:
                log.info("  ~ %s — trivial edit ignored", src["key"])
            else:
                rec = classifier.classify_change(diff, src)
                if not rec.is_substantive:
                    log.info("  ~ %s — cosmetic change (LLM), not recorded", src["key"])
                else:
                    rid = store.insert_record(sid, day, "change", rec, src["url"], diff.context,
                                         datetime.now(settings.tz))
                    if rid:
                        stats["changes"] += 1
                        log.info("  ▲ %s — %s impact change", src["key"], rec.impact_level)
                        if should_alert(rec, settings.alert_min_level):
                            alert_ids.append(rid)       # emailed by daily.py after the run
                            stats["alerts"] += 1
        store.commit()

    if hasattr(scraper, "close"):
        scraper.close()
    status = "success" if stats["failed"] == 0 else ("partial" if stats["failed"] < stats["checked"] else "failed")
    store.log_run({
        "run_date": day.isoformat(), "started_at": started.isoformat(),
        "finished_at": datetime.now(settings.tz).isoformat(),
        "sources_checked": stats["checked"], "sources_failed": stats["failed"],
        "core_sources_total": stats["core_total"], "core_sources_ok": stats["core_ok"],
        "baselines_created": stats["baselines"], "changes_found": stats["changes"],
        "alerts_sent": stats["alerts"], "status": status,
    })
    store.commit()
    summary = {"run_date": day.isoformat(), "status": status, **stats}
    log.info("Run complete: %s", summary)
    return {**summary, "alert_ids": alert_ids, "failures": failures, "ok_keys": ok_keys}
