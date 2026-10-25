"""Code checks only — these use in-memory HTML strings and an in-memory database.
Nothing here is shipped as data, written to exports/, or mixed with live records.

Core guarantees of the monitor:
1. Day 1 creates a baseline per source.
2. Day 2 with unchanged pages adds nothing, and day 1 is still in history.
3. Day 3 with a real change adds a 'change' record; days 1-2 untouched.
4. Volatile noise (scripts, 'last updated' stamps) never triggers a change.
5. Re-running the same day is idempotent.
6. Failed fetches are logged and don't break the run.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from visa_monitor.classifier import Classifier, rules_classify  # noqa: E402
from visa_monitor.config import Settings  # noqa: E402
from visa_monitor.diff import diff_pages  # noqa: E402
from visa_monitor.export import export_after_run, records_to_csv  # noqa: E402
from visa_monitor.kpi import compute_kpis  # noqa: E402
from visa_monitor.pipeline import run_daily  # noqa: E402
from visa_monitor.scraper import FetchResult, content_hash, normalise_html  # noqa: E402
from visa_monitor.storage import SQLiteStore  # noqa: E402


def _page(body: str, stamp: str = "1 October 2026", token: str = "abc123") -> str:
    return (f"<html><head><script>var t='{token}';</script></head><body>"
            f"<nav>Home | Visas | News</nav><main>{body}<p>Page last updated: {stamp}</p></main>"
            "<footer>Copyright Commonwealth of Australia</footer></body></html>")


BASE_500 = ("<h1>Student visa (subclass 500)</h1><p>Study full-time in a recognised course.</p>"
            "<h2>Health insurance</h2><p>You must have adequate health insurance for your stay.</p>")
PAGES = {
    "s500_v1": _page(BASE_500),
    "s500_v2": _page(BASE_500 + "<p>From 1 January 2027 visa condition 8501 requires OSHC to start "
                     "on arrival for subclass 500.</p>", stamp="14 October 2026", token="zzz"),
    "proc_v1": _page("<table><tr><th>Visa</th><th>90%</th></tr><tr><td>Subclass 500</td><td>45 days</td></tr></table>"),
    "proc_v2": _page("<table><tr><th>Visa</th><th>90%</th></tr><tr><td>Subclass 500</td><td>63 days</td></tr></table>"),
}
D1, D2, D3 = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)
SRC = [{"key": "s500", "name": "Student visa (subclass 500)", "agency": "Home Affairs",
        "url": "https://example.gov.au/500", "category": "visa_page", "visa_hint": ["500"],
        "render": "static", "selector": "main", "priority": 1},
       {"key": "proc", "name": "Processing times", "agency": "Home Affairs",
        "url": "https://example.gov.au/proc", "category": "processing_times", "visa_hint": ["500"],
        "render": "static", "selector": "main", "priority": 1}]


class FakeScraper:
    def __init__(self, pages):
        self.pages = pages      # key -> filename or None (fail)
        self.calls = 0

    def fetch(self, src):
        self.calls += 1
        name = self.pages[src["key"]]
        if name is None:
            return FetchResult(src["url"], False, 503, None, None, "HTTP 503")
        text = normalise_html(PAGES[name], src["selector"])
        return FetchResult(src["url"], True, 200, text, content_hash(text))


def settings():
    return Settings(openai_api_key="", alert_webhook_url="")


def run(store, day, pages):
    return run_daily(settings=settings(), store=store, scraper=FakeScraper(pages),
                     classifier=Classifier(settings()), run_date=day, sources=SRC)


V1 = {"s500": "s500_v1", "proc": "proc_v1"}
V2 = {"s500": "s500_v2", "proc": "proc_v2"}


def test_history_is_preserved_across_days():
    store = SQLiteStore()
    s1 = run(store, D1, V1)
    assert s1["baselines"] == 2 and s1["changes"] == 0

    s2 = run(store, D2, V1)
    assert s2["baselines"] == 0 and s2["changes"] == 0
    day1 = store.feed(D1, D1)
    assert len(day1) == 2 and all(r["record_type"] == "baseline" for r in day1)

    s3 = run(store, D3, V2)
    assert s3["changes"] == 2
    assert len(store.feed(D1, D1)) == 2          # day-1 records still there
    day3 = store.feed(D3, D3)
    assert {r["record_type"] for r in day3} == {"change"}
    assert len(store.feed()) == 4                 # full history
    assert len(store.runs()) == 3                 # one log row per day


def test_noise_does_not_trigger_change():
    a = normalise_html(_page(BASE_500), "main")
    b = normalise_html(_page(BASE_500, stamp="9 October 2026", token="different"), "main")
    assert content_hash(a) == content_hash(b)
    assert "Copyright" not in a and "Home | Visas" not in a


def test_rerun_same_day_is_idempotent():
    store = SQLiteStore()
    run(store, D1, V1)
    scraper = FakeScraper(V1)
    run_daily(settings=settings(), store=store, scraper=scraper, classifier=Classifier(settings()),
              run_date=D1, sources=SRC)
    assert scraper.calls == 0                     # nothing re-fetched
    assert len(store.feed()) == 2


def test_failed_fetch_is_logged_and_retried_later():
    store = SQLiteStore()
    s = run(store, D1, {"s500": "s500_v1", "proc": None})
    assert s["failed"] == 1 and s["status"] == "partial" and s["baselines"] == 1
    s = run(store, D1, V1)                        # retry same day succeeds
    assert s["baselines"] == 1
    assert len(store.feed(D1, D1)) == 2


def test_change_classification_rules():
    old = normalise_html(PAGES["s500_v1"], "main")
    new = normalise_html(PAGES["s500_v2"], "main")
    d = diff_pages(old, new)
    assert any("8501" in line for line in d.added)
    rec = Classifier(settings()).classify_change(d, SRC[0])
    assert rec.impact_level == "High"
    assert rec.affects_health_cover is True
    assert rec.effective_date == date(2027, 1, 1)
    assert "500" in rec.visa_categories


def test_processing_time_change_is_medium():
    text = "Student visa (subclass 500) | 28 days | 63 days\nprocessing times updated"
    rec = rules_classify(text, SRC[1], "change")
    assert rec.impact_level == "Medium" and rec.change_type == "processing_time"


def test_csv_and_kpis(tmp_path):
    store = SQLiteStore()
    for day, pages in [(D1, V1), (D2, V1), (D3, V2)]:
        run(store, day, pages)
    csv_text = records_to_csv(store.feed())
    files = export_after_run(store, D3, tmp_path)
    assert (tmp_path / "daily" / "2026-10-14.csv").exists() and Path(files["run_log"]).exists()
    assert csv_text.splitlines()[0].startswith("Record ID,Record Date")
    assert len(csv_text.strip().splitlines()) == 5
    k = compute_kpis(store)
    assert k["coverage"]["avg_core_source_coverage_pct"] == 100.0
    assert k["timeliness"]["daily_run_consistency_pct"] == 100.0
    assert k["usefulness"]["changes_detected"] == 2


def test_llm_path_uses_structured_output(monkeypatch):
    clf = Classifier(settings())
    clf._client = object()   # pretend an API key is configured
    fake = {"title": "OSHC start-date rule", "change_summary": "OSHC must start on arrival.",
            "effective_date": "2027-01-01", "visa_categories": ["subclass 500"],
            "change_type": "health_insurance", "impact_level": "High",
            "impact_reason": "Changes cover requirements for all student arrivals.",
            "affects_health_cover": True, "confidence": 0.92, "is_substantive": True}
    monkeypatch.setattr(clf, "_call_llm", lambda prompt: fake)
    d = diff_pages("a\nb", "a\nb\nOSHC must start on arrival from 1 January 2027")
    rec = clf.classify_change(d, SRC[0])
    assert rec.classifier == "gpt-4o-mini" and rec.visa_categories == ["500"]
    assert rec.effective_date == date(2027, 1, 1)


def test_llm_failure_falls_back_to_rules(monkeypatch):
    clf = Classifier(settings())
    clf._client = object()
    def boom(prompt):
        raise RuntimeError("API down")
    monkeypatch.setattr(clf, "_call_llm", boom)
    d = diff_pages("a", "a\nvisa condition 8501 now applies to subclass 500")
    assert clf.classify_change(d, SRC[0]).classifier == "rules"


# ---------------------------------------------------------------- scraper fallback
from visa_monitor.scraper import Scraper  # noqa: E402

SHELL = "<html><body><main><div id='app'></div></main></body></html>"
FULL = _page(BASE_500 + "<p>" + "Eligibility and conditions for students. " * 10 + "</p>")


def _scraper(monkeypatch, static, js, js_enabled=True):
    sc = Scraper(Settings(enable_js_render=js_enabled, polite_delay_seconds=0))
    monkeypatch.setattr(sc, "allowed", lambda url: True)
    monkeypatch.setattr(sc, "_get_static", lambda url: static)
    calls = []
    def fake_js(url):
        calls.append(url)
        return js
    monkeypatch.setattr(sc, "_get_js", fake_js)
    return sc, calls


def test_js_shell_falls_back_to_browser(monkeypatch):
    sc, calls = _scraper(monkeypatch, (200, SHELL), (200, FULL))
    r = sc.fetch(SRC[0])
    assert r.ok and calls and "Health insurance" in r.text


def test_static_page_does_not_start_browser(monkeypatch):
    sc, calls = _scraper(monkeypatch, (200, FULL), (200, SHELL))
    assert sc.fetch(SRC[0]).ok and not calls


def test_403_reported_clearly(monkeypatch):
    sc, _ = _scraper(monkeypatch, (403, ""), (403, ""))
    r = sc.fetch(SRC[0])
    assert not r.ok and "HTTP 403" in r.error


def test_shell_without_browser_explains_setting(monkeypatch):
    sc, _ = _scraper(monkeypatch, (200, SHELL), (200, FULL), js_enabled=False)
    r = sc.fetch(SRC[0])
    assert not r.ok and "ENABLE_JS_RENDER" in r.error


def test_sharepoint_form_wrapped_page_keeps_content():
    """Home Affairs wraps the whole page in <form id='aspnetForm'> — content must survive."""
    html = ("<html><body><form id='aspnetForm' method='post'><input type='hidden' value='x'>"
            "<div class='ha-page'>" + BASE_500 + "<p>" + "Visa conditions apply to all holders. " * 8 +
            "</p></div></form></body></html>")
    text = normalise_html(html, "main")
    assert "Student visa (subclass 500)" in text and len(text) > 200


def test_baseline_has_no_effective_date_and_local_detected_time():
    store = SQLiteStore()
    run(store, D1, V1)
    rows = store.feed()
    assert all(r["effective_date"] is None for r in rows)
    # stored as local "YYYY-MM-DD HH:MM:SS" (no UTC 'T...Z' form)
    assert all(len(r["detected_at"]) == 19 and "T" not in r["detected_at"] for r in rows)
