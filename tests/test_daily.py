"""Checks for the daily scheduler and the alert emails (milestone 3.2).

Nothing here touches the network, the database or a real mailbox: the clock is passed in,
the pipeline and the email sender are replaced by small fakes, and files go to tmp_path.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visa_monitor import alerts, daily  # noqa: E402

MEL = ZoneInfo("Australia/Melbourne")


def at(day: str, hhmm: str) -> datetime:
    """A Melbourne wall-clock time as an aware datetime (what the scheduler receives)."""
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=MEL)


TUE, MON = "2026-11-03", "2026-11-09"     # 3 Nov 2026 is a Tuesday, 9 Nov a Monday


# ----------------------------------------------------------------- when to run
def test_not_before_830_melbourne():
    run, why = daily.decide(at(TUE, "08:10"), {})
    assert not run and "before 08:30" in why


def test_due_after_830_with_no_run_today():
    run, why = daily.decide(at(TUE, "08:31"), {})
    assert run and "attempt 1 of 3" in why


def test_only_one_successful_run_per_day():
    state = {"last_success_at": at(TUE, "08:35").isoformat()}
    assert daily.decide(at(TUE, "12:00"), state)[0] is False
    assert daily.decide(at("2026-11-04", "08:40"), state)[0] is True      # next day: due again


def test_retry_waits_60_minutes_and_gives_up_after_3():
    state = daily.record_attempt({}, at(TUE, "08:31"))
    assert daily.decide(at(TUE, "09:00"), state)[0] is False             # 29 min later
    assert daily.decide(at(TUE, "09:32"), state)[0] is True              # 61 min later
    state = daily.record_attempt(daily.record_attempt(state, at(TUE, "09:32")), at(TUE, "10:40"))
    run, why = daily.decide(at(TUE, "13:00"), state)
    assert not run and "gave up" in why


def test_stale_warning_after_48_hours_once_a_day():
    state = {"last_success_at": at(TUE, "08:35").isoformat()}
    assert not daily.needs_stale_warning(at("2026-11-04", "09:00"), state)
    later = at("2026-11-05", "09:00")
    assert daily.needs_stale_warning(later, state)
    state["last_stale_warning_at"] = later.isoformat()
    assert not daily.needs_stale_warning(later + timedelta(hours=3), state)


# ----------------------------------------------------------------- weekly email
def test_weekly_sent_on_monday_once():
    assert daily.weekly_due(at(MON, "08:35"), {})
    assert not daily.weekly_due(at(MON, "15:00"), {"last_weekly_at": at(MON, "08:35").isoformat()})


def test_weekly_not_sent_midweek_before_first_monday():
    assert not daily.weekly_due(at(TUE, "08:35"), {})


def test_missed_monday_is_caught_up_next_run():
    state = {"last_weekly_at": at("2026-11-02", "08:35").isoformat()}       # previous Monday
    assert daily.weekly_due(at("2026-11-10", "08:35"), state)               # Tuesday, 8 days later


# ----------------------------------------------------------------- source failures
def test_first_run_remembers_failures_without_alarm():
    new, rec, state = daily.track_failures({}, [{"key": "a"}], ["b"])
    assert new == [] and state["known_failed_sources"] == ["a"]


def test_only_newly_failing_sources_are_reported_and_recoveries_noted():
    state = {"known_failed_sources": ["a"]}
    new, rec, state = daily.track_failures(state, [{"key": "a"}, {"key": "b"}], [])
    assert [f["key"] for f in new] == ["b"]
    new, rec, state = daily.track_failures(state, [{"key": "b"}], ["a"])
    assert new == [] and rec == ["a"] and state["known_failed_sources"] == ["b"]


# ----------------------------------------------------------------- the whole call
def fake_result(changes=(), failures=(), ok=("s1",), week=None):
    result = {"checked": 2, "failed": len(failures), "baselines": 0, "changes": len(changes),
              "alert_changes": list(changes), "failures": list(failures), "ok_keys": list(ok),
              "stored": "postgres"}
    if week:
        result["week"] = week
    return result


class Calls:
    def __init__(self):
        self.daily, self.weekly, self.failure = [], [], []

    def notifier(self, changes, new_failures, label, recovered=None):
        self.daily.append((changes, new_failures))
        return "sent" if changes or new_failures else "nothing to send"

    def weekly_notifier(self, *args):
        self.weekly.append(args)
        return "sent"

    def failure_notifier(self, subject, body):
        self.failure.append(subject)
        return "sent"


def call(tmp_path, now, calls, runner, **kw):
    return daily.main(now=now, log_dir=tmp_path, runner=runner, notifier=calls.notifier,
                      weekly_notifier=calls.weekly_notifier, failure_notifier=calls.failure_notifier,
                      network_check=kw.pop("network_check", lambda: True), **kw)


def test_successful_run_emails_changes_and_is_not_repeated(tmp_path):
    calls = Calls()
    change = {"id": 1, "impact_level": "High"}
    assert call(tmp_path, at(TUE, "08:31"), calls, lambda now, weekly: fake_result([change])) == 0
    assert calls.daily == [([change], [])]
    assert call(tmp_path, at(TUE, "09:30"), calls, lambda now, weekly: fake_result([change])) == 0
    assert len(calls.daily) == 1                                 # second call skipped
    assert "already ran successfully" in (tmp_path / daily.LOG_FILE).read_text()


def test_offline_does_not_use_an_attempt(tmp_path):
    calls = Calls()
    assert call(tmp_path, at(TUE, "08:31"), calls, lambda n, w: fake_result(), network_check=lambda: False) == 3
    assert daily.load_state(tmp_path) == {}


def test_failed_run_sends_one_failure_email_per_day(tmp_path):
    calls = Calls()

    def broken(now, weekly):
        raise RuntimeError("tunnel did not open")

    assert call(tmp_path, at(TUE, "08:31"), calls, broken) == 1
    assert call(tmp_path, at(TUE, "09:40"), calls, broken) == 1
    assert calls.failure == ["Visa & regulatory monitor: daily run failed"]


def test_all_sources_failing_is_a_failed_attempt(tmp_path):
    calls = Calls()
    result = {**fake_result(failures=[{"key": "s1"}, {"key": "s2"}], ok=()), "status": "failed"}
    assert call(tmp_path, at(TUE, "08:31"), calls, lambda n, w: result) == 1
    state = daily.load_state(tmp_path)
    assert "last_success_at" not in state and "known_failed_sources" not in state
    assert calls.daily == [] and len(calls.failure) == 1
    assert daily.decide(at(TUE, "09:35"), state)[0] is True          # retried after 60 minutes


def test_monday_run_sends_weekly_insights(tmp_path):
    calls = Calls()
    week = {"changes": [], "runs": [], "failures": {}, "label": "03 Nov - 09 Nov 2026"}
    assert call(tmp_path, at(MON, "08:31"), calls, lambda now, weekly: fake_result(week=week if weekly else None)) == 0
    assert len(calls.weekly) == 1
    assert daily.load_state(tmp_path)["last_weekly_at"].startswith(MON)


def test_lock_blocks_a_second_run(tmp_path):
    calls = Calls()
    (tmp_path / daily.LOCK_FILE).write_text(f"{at(TUE, '08:30').isoformat()}|pid 1", encoding="utf-8")
    assert call(tmp_path, at(TUE, "08:31"), calls, lambda n, w: fake_result()) == 2


# ----------------------------------------------------------------- email content
ROW = {"id": 7, "record_date": "2026-11-03", "impact_level": "High", "source_name": "Student visa (subclass 500)",
       "change_summary": "OSHC must start on arrival", "impact_reason": "health-cover condition",
       "visa_categories": "500", "effective_date": "2027-01-01", "affects_health_cover": True,
       "source_url": "https://example.gov.au/500", "change_type": "health_insurance", "title": "t"}


def test_should_alert_uses_minimum_level():
    assert alerts.should_alert(SimpleNamespace(impact_level="Medium"), "Medium")
    assert not alerts.should_alert(SimpleNamespace(impact_level="Low"), "Medium")
    assert not alerts.should_alert(SimpleNamespace(impact_level="Medium"), "High")


def test_quiet_day_sends_nothing():
    assert alerts.build_daily_message([], [], "run") is None


def test_daily_message_lists_high_first_with_link():
    medium = {**ROW, "id": 8, "impact_level": "Medium", "source_name": "Processing times"}
    subject, text, html, rows = alerts.build_daily_message([medium, ROW], [], "run of 03 Nov")
    assert subject == "Visa & regulatory monitor: 1 High, 1 Medium impact changes"
    assert [r["id"] for r in rows] == [7, 8]
    assert "https://example.gov.au/500" in html and "2027-01-01" in html


def test_new_failure_alone_still_emails():
    subject, *_ = alerts.build_daily_message([], [{"key": "x", "name": "Fair Work", "error": "HTTP 403"}], "run")
    assert subject.endswith("1 source stopped working")


def test_weekly_message_counts_visas_and_source_health():
    runs = [{"run_date": "2026-11-03", "core_sources_ok": 10, "core_sources_total": 10},
            {"run_date": "2026-11-04", "core_sources_ok": 9, "core_sources_total": 10}]
    subject, text, html, rows = alerts.build_weekly_message([ROW], runs, {"Fair Work": 1}, "03 Nov - 09 Nov 2026")
    assert "1 change" in subject
    assert "ran on 2 of 7 days" in text and "95%" in text
    assert "500 (1)" in text and "Fair Work (1 day)" in text


def test_preview_file_written_without_smtp(tmp_path):
    out = alerts.notify_daily([ROW], [], "run", config=None, log_dir=tmp_path,
                              sender=lambda *a: pytest.fail("must not send"))
    assert out == "preview" or alerts.config_from_env() is not None
    if out == "preview":
        assert "OSHC must start on arrival" in (tmp_path / alerts.DAILY_PREVIEW).read_text(encoding="utf-8")


def test_send_uses_csv_attachment():
    sent = {}

    def sender(config, subject, text, html, attachment, filename):
        sent.update(subject=subject, attachment=attachment, filename=filename)

    cfg = {"user": "u", "password": "p", "to": ["a@b"], "from": "u", "host": "h", "port": 465}
    assert alerts.notify_daily([ROW], [], "run", config=cfg, sender=sender) == "sent"
    assert sent["filename"] == "visa_changes.csv" and b"OSHC must start on arrival" in sent["attachment"]
