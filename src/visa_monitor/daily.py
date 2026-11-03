"""The ONE command Windows Task Scheduler calls (milestone 3.2): ``python scripts\\daily.py``.

    python scripts\\daily.py              run if a run is due, then email what needs attention
    python scripts\\daily.py --dry-run    only say whether a run is due
    python scripts\\daily.py --force      run now, ignoring the once-a-day rules

"RUN WHEN DUE", NOT "RUN AT A CLOCK TIME" (same design as Modules 1, 2 and 4)
---------------------------------------------------------------------------
A laptop is not a server: at 8:30 it may be asleep or offline. So Task Scheduler calls this
command OFTEN (08:30 Melbourne with wake-up, at log-on and every 30 minutes) and each call asks:

    1. Is it 08:30 Melbourne or later today?   (M1 7:00, M2 7:30, M4 8:00: no tunnel clashes)
    2. Has there been no successful run since 08:30 today?
    3. Fewer than 3 attempts today?
    4. Was the last attempt at least 60 minutes ago?

Only if all four hold does it run, so calling it ten times = calling it once ("idempotent").
Before using an attempt, a 5-second network check skips the run (WITHOUT counting an
attempt) when the laptop is offline. A LOCK FILE (logs/visa_run.lock) stops two runs
overlapping; one older than 2 hours is treated as left over from a crash.

After the run (alerts.py):
    * daily alert email only for High/Medium changes or a source that NEWLY stopped working;
    * weekly insights email after the first successful run on/after Monday;
    * if the run failed (tunnel, database): a warning email, at most one per day;
    * if there has been NO successful run for 48 hours: a "data is stale" email (max one/day).
Every decision is appended to logs/visa_daily.log, so a skipped day can be explained.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .env import LOG_DIR, ROOT, load_env_file

TIMEZONE = ZoneInfo("Australia/Melbourne")   # on Windows this needs the "tzdata" package
RUN_AFTER = (8, 30)                          # 08:30 Melbourne
MAX_ATTEMPTS_PER_DAY = 3
RETRY_AFTER_MINUTES = 60
STALE_AFTER_HOURS = 48
WEEKLY_WEEKDAY = 0                           # Monday = 0 ... Sunday = 6
LOCK_MAX_AGE = timedelta(hours=2)
NETWORK_CHECK_HOSTS = ("immi.homeaffairs.gov.au", "www.education.gov.au", "www.google.com")
NETWORK_CHECK_TIMEOUT = 5
STATE_FILE, LOCK_FILE, LOG_FILE = "visa_daily_state.json", "visa_run.lock", "visa_daily.log"


# --------------------------------------------------------------------------- time rules (pure)

def melbourne(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(TIMEZONE)


def due_time(now: datetime) -> datetime:
    return melbourne(now).replace(hour=RUN_AFTER[0], minute=RUN_AFTER[1], second=0, microsecond=0)


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def decide(now: datetime, state: dict) -> tuple[bool, str]:
    """(should_run, reason). Pure function: no clock, no files -> easy to test."""
    local, due = melbourne(now), due_time(now)
    if local < due:
        return False, f"before {RUN_AFTER[0]:02d}:{RUN_AFTER[1]:02d} Melbourne (it is {local:%H:%M})"
    last_success = _time(state.get("last_success_at"))
    if last_success and last_success >= due:
        return False, f"already ran successfully today ({melbourne(last_success):%H:%M} Melbourne)"
    today = local.date().isoformat()
    attempts = state.get("attempts", 0) if state.get("date") == today else 0
    if attempts >= MAX_ATTEMPTS_PER_DAY:
        return False, f"gave up for today after {attempts} attempts"
    last_attempt = _time(state.get("last_attempt_at")) if state.get("date") == today else None
    if last_attempt:
        waited = (now - last_attempt).total_seconds() / 60
        if waited < RETRY_AFTER_MINUTES:
            return False, f"last attempt {waited:.0f} min ago; retry after {RETRY_AFTER_MINUTES}"
    return True, f"run is due (attempt {attempts + 1} of {MAX_ATTEMPTS_PER_DAY} today)"


def record_attempt(state: dict, now: datetime) -> dict:
    today = melbourne(now).date().isoformat()
    attempts = state.get("attempts", 0) if state.get("date") == today else 0
    return {**state, "date": today, "attempts": attempts + 1, "last_attempt_at": now.isoformat(timespec="seconds")}


def needs_stale_warning(now: datetime, state: dict) -> bool:
    """True when the last success is older than STALE_AFTER_HOURS and we have not warned today."""
    last_success = _time(state.get("last_success_at")) or _time(state.get("first_attempt_at"))
    if last_success is None or now - last_success < timedelta(hours=STALE_AFTER_HOURS):
        return False
    warned = _time(state.get("last_stale_warning_at"))
    return warned is None or now - warned >= timedelta(hours=24)


def weekly_due(now: datetime, state: dict) -> bool:
    """Send the weekly insights after a successful run when today is Monday (and it was not sent
    today), or when the last weekly email is 7+ days old (Monday was missed: laptop off).
    Before the first Monday nothing is sent, so installing the scheduler mid-week is quiet."""
    local = melbourne(now)
    last = _time(state.get("last_weekly_at"))
    if last and melbourne(last).date() == local.date():
        return False
    if local.weekday() == WEEKLY_WEEKDAY:
        return True
    return last is not None and now - last >= timedelta(days=7)


def track_failures(state: dict, failures: list[dict], ok_keys) -> tuple[list[dict], list[str], dict]:
    """(new_failures, recovered_keys, new_state).

    Only a source that worked before and fails now is "new". The first run just remembers
    what is failing, so installing the scheduler does not start with a false alarm, and a
    source that is blocked every day does not cause an email every day.
    """
    failed = {f.get("key") for f in failures if f.get("key")}
    succeeded = set(ok_keys)
    if "known_failed_sources" not in state:
        new_failures, recovered, known = [], [], set()
    else:
        known = set(state["known_failed_sources"])
        new_failures = [f for f in failures if f.get("key") not in known]
        recovered = sorted(known & succeeded)
    updated = sorted((known - succeeded) | failed)
    return new_failures, recovered, {**state, "known_failed_sources": updated}


# --------------------------------------------------------------------------- files

def network_ok(hosts=NETWORK_CHECK_HOSTS, timeout: float = NETWORK_CHECK_TIMEOUT) -> bool:
    """True if ANY host accepts a TCP connection on port 443 (no page is requested)."""
    for host in hosts:
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def load_state(log_dir: Path = LOG_DIR) -> dict:
    try:
        return json.loads((Path(log_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict, log_dir: Path = LOG_DIR) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    (Path(log_dir) / STATE_FILE).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def log_line(message: str, log_dir: Path = LOG_DIR, now: datetime | None = None) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    with (Path(log_dir) / LOG_FILE).open("a", encoding="utf-8") as stream:
        stream.write(f"{melbourne(now):%Y-%m-%d %H:%M:%S} Melbourne | {message}\n")
    print(message)


class RunLock:
    """Context manager around logs/visa_run.lock. ``acquired`` is False if another run holds it."""

    def __init__(self, log_dir: Path = LOG_DIR, now: datetime | None = None):
        self.path = Path(log_dir) / LOCK_FILE
        self.now = now or datetime.now(timezone.utc)
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                started = datetime.fromisoformat(self.path.read_text(encoding="utf-8").split("|")[0])
            except ValueError:
                started = self.now - LOCK_MAX_AGE - timedelta(seconds=1)
            if self.now - started < LOCK_MAX_AGE:
                return self
        self.path.write_text(f"{self.now.isoformat()}|pid {os.getpid()}", encoding="utf-8")
        self.acquired = True
        return self

    def __exit__(self, *exc):
        if self.acquired:
            self.path.unlink(missing_ok=True)
        return False


# --------------------------------------------------------------------------- the run

def run_pipeline(now: datetime, weekly: bool) -> dict:
    """Scrape, store in RDS, write the CSVs, and read back what the emails need.

    Returns the pipeline summary plus ``alert_changes`` (feed rows to email), and, when ``weekly``
    is True, ``week`` = {changes, runs, failures, label} for the last 7 days.
    """
    from .config import Settings
    from .db import open_store
    from .export import export_after_run
    from .pipeline import run_daily

    settings = Settings()
    with open_store(settings) as store:
        summary = run_daily(settings=settings, store=store)
        day = date.fromisoformat(summary["run_date"])
        export_after_run(store, day, ROOT / "exports")
        wanted = set(summary["alert_ids"])
        summary["alert_changes"] = [r for r in store.feed(day, day) if r["id"] in wanted]
        summary["stored"] = settings.storage_backend
        if weekly:
            start = day - timedelta(days=6)
            summary["week"] = {
                "changes": [r for r in store.feed(start, day) if r["record_type"] == "change"],
                "runs": [r for r in store.runs() if str(r["run_date"]) >= start.isoformat()],
                "failures": store.failed_sources_since(start),
                "label": f"{start:%d %b} - {day:%d %b %Y}",
            }
    return summary


def main(*, force: bool = False, dry_run: bool = False, now: datetime | None = None,
         log_dir: Path = LOG_DIR, runner=None, notifier=None, weekly_notifier=None,
         failure_notifier=None, network_check=None) -> int:
    """Exit codes: 0 ok/skipped, 1 run failed, 2 another run holds the lock, 3 offline (no try used)."""
    from . import alerts
    runner = runner or run_pipeline
    notifier = notifier or alerts.notify_daily
    weekly_notifier = weekly_notifier or alerts.notify_weekly
    failure_notifier = failure_notifier or alerts.notify_failure
    network_check = network_check or network_ok
    now = now or datetime.now(timezone.utc)

    state = load_state(log_dir)
    should_run, reason = decide(now, state)
    if force:
        should_run, reason = True, "forced by --force"
    if dry_run:
        print(f"Would {'RUN' if should_run else 'SKIP'}: {reason}"
              + (" (+ weekly insights email)" if should_run and weekly_due(now, state) else ""))
        return 0
    if not should_run:
        log_line(f"skipped: {reason}", log_dir, now)
        if needs_stale_warning(now, state):
            _stale_warning(state, now, log_dir, failure_notifier)
        return 0

    if not network_check():
        log_line("skipped: no internet connection (not counted as an attempt)", log_dir, now)
        if needs_stale_warning(now, state):
            _stale_warning(state, now, log_dir, failure_notifier)
        return 3

    with RunLock(log_dir, now) as lock:
        if not lock.acquired:
            log_line("another visa monitor run holds the lock; nothing done", log_dir, now)
            return 2
        state = record_attempt(state, now)
        state.setdefault("first_attempt_at", now.isoformat(timespec="seconds"))
        save_state(state, log_dir)
        weekly = weekly_due(now, state)
        log_line(f"starting: {reason}" + (" (+ weekly insights)" if weekly else ""), log_dir, now)
        try:
            result = runner(now, weekly)
        except (Exception, SystemExit) as exc:      # tunnel / database / unexpected error
            log_line(f"run failed: {type(exc).__name__}: {exc}", log_dir, now)
            _failure(state, now, log_dir, failure_notifier,
                     f"The visa & regulatory run failed before finishing: {type(exc).__name__}: {exc}. "
                     "The data already stored is unchanged; the next attempt retries automatically. "
                     "If it mentions the tunnel, check logs/tunnel.log (most often: add 'My IP' to the "
                     "jumpbox security group).")
            return 1

        if result.get("status") == "failed":        # every source failed: not a usable run
            log_line(f"run failed: all {result.get('checked', 0)} sources were unavailable "
                     "(internet or site problem); will retry", log_dir, now)
            _failure(state, now, log_dir, failure_notifier,
                     f"Every source ({result.get('checked', 0)}) was unavailable in this run, so nothing new "
                     "could be checked. The stored data is unchanged; the next attempt retries automatically.")
            return 1

        new_failures, recovered, state = track_failures(state, result.get("failures", []),
                                                        result.get("ok_keys", []))
        label = f"run of {melbourne(now):%d %b %Y %H:%M} Melbourne"
        try:
            outcome = notifier(result.get("alert_changes", []), new_failures, label, recovered=recovered)
        except Exception as exc:                    # email trouble must not lose the run
            outcome = f"email failed: {type(exc).__name__}: {exc}"
        weekly_outcome = "not due"
        if weekly and "week" in result:
            week = result["week"]
            try:
                weekly_outcome = weekly_notifier(week["changes"], week["runs"], week["failures"], week["label"])
                state["last_weekly_at"] = now.isoformat(timespec="seconds")
            except Exception as exc:
                weekly_outcome = f"email failed: {type(exc).__name__}: {exc}"

        state["last_success_at"] = now.isoformat(timespec="seconds")
        save_state(state, log_dir)
        log_line(f"finished: {result.get('checked', 0)} sources, {result.get('failed', 0)} unavailable "
                 f"({len(new_failures)} new), {result.get('baselines', 0)} baselines, {result.get('changes', 0)} "
                 f"changes ({len(result.get('alert_changes', []))} High/Medium), storage "
                 f"{result.get('stored', '?')}, alert {outcome}, weekly {weekly_outcome}",
                 log_dir, now)
        return 0


def _failure(state, now, log_dir, failure_notifier, body):
    """At most one failure email per day."""
    today = melbourne(now).date().isoformat()
    if state.get("last_failure_email") == today:
        return
    try:
        failure_notifier("Visa & regulatory monitor: daily run failed", body)
        state["last_failure_email"] = today
        save_state(state, log_dir)
    except Exception as exc:
        log_line(f"failure email could not be sent: {exc}", log_dir, now)


def _stale_warning(state, now, log_dir, failure_notifier):
    last = state.get("last_success_at") or "never"
    try:
        failure_notifier("Visa & regulatory monitor: data is stale",
                         f"No successful visa & regulatory run for more than {STALE_AFTER_HOURS} hours (last "
                         f"success: {last}). Check logs/visa_daily.log and logs/tunnel.log: most often the "
                         "laptop was off, or the tunnel failed (public IP changed: add it to the jumpbox "
                         "security group as 'My IP').")
        state["last_stale_warning_at"] = now.isoformat(timespec="seconds")
        save_state(state, log_dir)
    except Exception as exc:
        log_line(f"stale warning could not be sent: {exc}", log_dir, now)


def cli(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Module 3 daily run (called by Windows Task Scheduler).")
    parser.add_argument("--force", action="store_true", help="run now, ignoring the once-a-day rules")
    parser.add_argument("--dry-run", action="store_true", help="only say whether a run is due")
    args = parser.parse_args(argv)
    load_env_file()                             # ALERT_* and DB_* settings; the real environment wins
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(LOG_DIR / "monitor.log", encoding="utf-8")])
    sys.exit(main(force=args.force, dry_run=args.dry_run))
