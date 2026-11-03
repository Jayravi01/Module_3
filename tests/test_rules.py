"""Checks for milestone 3.3: precise visa tags, whole-word keyword rules, the 'basis' columns,
RSS sources, and upgrading the existing RDS tables (PostgreSQL test only with TEST_DB_HOST)."""
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from visa_monitor.classifier import (Classifier, RegulatoryRecord, apply_basis,  # noqa: E402
                                     find_subclasses, has_term, rules_classify)
from visa_monitor.config import Settings, load_sources  # noqa: E402
from visa_monitor.diff import diff_pages  # noqa: E402
from visa_monitor.scraper import rss_to_text  # noqa: E402

STUDENT = {"key": "s500", "name": "Student visa (subclass 500)", "agency": "Home Affairs",
           "url": "https://example.gov.au/500", "category": "visa_page", "visa_hint": ["500"]}
NEWS = {"key": "news", "name": "Minister media releases", "agency": "Minister",
        "url": "https://example.gov.au/news", "category": "media_release", "visa_hint": []}
clf = Classifier(Settings(llm_enabled=False, storage_backend="sqlite"))


# ----------------------------------------------------------------- visa tags
def test_baseline_is_tagged_with_the_pages_own_visa_only():
    page = ("Student visa (subclass 500). You may later apply for a Temporary Graduate visa "
            "(subclass 485) or a Student Guardian visa (subclass 590).")
    rec = clf.summarise_baseline(page, STUDENT)
    assert rec.visa_categories == ["500"] and rec.visa_basis == "stated"
    assert rec.change_type == "baseline" and rec.impact_basis == "none" and rec.impact_level == "Low"


def test_news_baseline_has_no_visa_tag():
    rec = clf.summarise_baseline("2026-11-01 | Visit to China | Minister visited Beijing", NEWS)
    assert rec.visa_categories == [] and rec.visa_basis == "none"


def test_change_uses_subclasses_named_in_the_changed_lines():
    old = "Student visa (subclass 500)\nApply online."
    new = old + "\nFrom 1 January 2027 Temporary Graduate visa (subclass 485) holders need OSHC."
    rec = clf.classify_change(diff_pages(old, new), STUDENT)
    assert rec.visa_categories == ["485"] and rec.visa_basis == "stated"
    assert rec.effective_date == date(2027, 1, 1) and rec.effective_date_basis == "stated"
    assert rec.impact_level == "High" and rec.impact_basis == "inferred"


def test_change_without_named_visa_falls_back_to_page_visa_as_inferred():
    old = "Student visa (subclass 500)\nFees apply."
    new = old + "\nThe application charge increases."
    rec = clf.classify_change(diff_pages(old, new), STUDENT)
    assert rec.visa_categories == ["500"] and rec.visa_basis == "inferred"
    assert rec.effective_date is None and rec.effective_date_basis == "none"


def test_news_change_without_visa_is_general():
    rec = clf.classify_change(diff_pages("a | old item", "a | old item\nb | Minister opens a new office"), NEWS)
    assert rec.visa_categories == [] and rec.visa_basis == "none"


def test_llm_date_not_in_text_is_marked_inferred():
    rec = RegulatoryRecord(title="t", change_summary="s", effective_date=date(2027, 7, 1),
                           visa_categories=["482"])
    apply_basis(rec, "change", "Skills in Demand visa (subclass 482) changes announced", STUDENT)
    assert rec.effective_date_basis == "inferred" and rec.visa_basis == "stated"


# ----------------------------------------------------------------- keyword rules
def test_keywords_match_whole_words_only():
    assert has_term("a new visa cap applies", "cap")
    assert not has_term("financial capacity of the applicant", "cap")
    assert not has_term("send us your feedback", "fee")
    assert has_term("from 1 july", "from 1 ")


def test_capacity_wording_is_not_high_impact():
    rec = rules_classify("Our capacity to help applicants has improved.", STUDENT, "change")
    assert rec.impact_level == "Low"


def test_explicit_subclass_patterns():
    assert find_subclasses("Visitor visa (subclass 600) and the 417 visa") == ["417", "600"]
    assert find_subclasses("Call 131 500 for help") == []


# ----------------------------------------------------------------- RSS sources
RSS = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<title>Minister RSS Feed</title><lastBuildDate>Tue, 3 Nov 2026 10:00:00 +1100</lastBuildDate>
<item><title>Student visa integrity reforms</title><pubDate>Sun, 1 Nov 2026 00:00:00 +1100</pubDate>
<description>&lt;p&gt;New rules for subclass 500 &amp;amp; OSHC.&lt;/p&gt;</description></item>
<item><title>Visit to China</title><pubDate>Tue, 17 Mar 2026 09:00:00 +1100</pubDate></item>
</channel></rss>"""


def test_rss_becomes_one_line_per_item_without_build_date():
    text = rss_to_text(RSS)
    assert text.splitlines() == ["2026-11-01 | Student visa integrity reforms | New rules for subclass 500 & OSHC.",
                                 "2026-03-17 | Visit to China"]
    assert "10:00" not in text


def test_new_sources_are_configured():
    keys = {s["key"]: s for s in load_sources(ROOT / "config" / "sources.yaml")}
    for key in ("minister_home_affairs", "minister_immigration_assistant", "minister_education",
                "minister_international_education", "mia_media_releases"):
        assert key in keys
    assert keys["minister_home_affairs"]["render"] == "rss"
    assert keys["mia_media_releases"]["priority"] == 2


# ----------------------------------------------------------------- upgrade existing RDS tables
from test_db import PG, needs_pg  # noqa: E402
from visa_monitor import db  # noqa: E402
from visa_monitor.storage import PostgresStore  # noqa: E402

UPGRADE_SCHEMA = "visa_upgrade_test"


@needs_pg
def test_existing_tables_from_milestone_3_2_are_upgraded(tmp_path):
    """RDS already has the 3.1/3.2 tables and data: applying the new SQL must add the basis
    columns, allow render='rss', keep the old rows, and extend the view."""
    old_sql = subprocess.run(["git", "show", "fb48ff2:sql/001_schema.sql"], cwd=ROOT,
                             capture_output=True, text=True)
    if old_sql.returncode != 0:
        pytest.skip("old schema not available in this checkout")
    path = tmp_path / "old.sql"
    path.write_text(old_sql.stdout, encoding="utf-8")
    cfg = {"host": PG["host"], "port": int(PG["port"] or 5432), "dbname": PG["name"] or "postgres",
           "user": PG["user"] or "postgres", "password": PG["password"] or "", "sslmode": "prefer"}
    admin = db.connect(cfg)
    admin.execute(f"DROP SCHEMA IF EXISTS {UPGRADE_SCHEMA} CASCADE")
    admin.commit()
    conn = db.connect(cfg, UPGRADE_SCHEMA)
    try:
        db.apply_schema(conn, UPGRADE_SCHEMA, path)                      # the 3.2 tables
        conn.execute("insert into sources (key, name, url) values ('k', 'n', 'u')")
        conn.execute("insert into regulatory_records (source_id, record_date, record_type) "
                     "values (1, '2026-11-03', 'baseline')")
        conn.commit()
        db.apply_schema(conn, UPGRADE_SCHEMA)                             # the 3.3 SQL
        db.apply_schema(conn, UPGRADE_SCHEMA)                             # and again (every run)
        conn.execute("insert into sources (key, name, url, render) values ('r', 'rss', 'u2', 'rss')")
        row = conn.execute("select record_type, visa_basis from v_regulatory_feed").fetchone()
        assert row == ("baseline", None)
        store = PostgresStore(conn)
        assert store.feed()[0]["impact_basis"] is None
    finally:
        conn.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {UPGRADE_SCHEMA} CASCADE")
        admin.commit()
        admin.close()


# ----------------------------------------------------------------- retag old baselines (3.5)
def test_retag_old_baselines_once(tmp_path, monkeypatch):
    """A baseline stored by the old rules is corrected; changes are never touched; re-run = 0."""
    sys.path.insert(0, str(ROOT / "scripts"))
    monkeypatch.chdir(ROOT)
    import importlib
    import _bootstrap  # noqa: F401
    retag = importlib.import_module("retag_baselines")
    from visa_monitor.scraper import FetchResult
    from visa_monitor.storage import SQLiteStore
    store = SQLiteStore(str(tmp_path / "t.db"))
    ids = store.sync_sources([{**STUDENT, "url": STUDENT["url"]}])
    day = date(2026, 11, 3)
    page = "Student visa (subclass 500). Later: Temporary Graduate visa (subclass 485)."
    store.save_snapshot(ids["s500"], day, FetchResult("u", True, 200, page, "h"))
    old = RegulatoryRecord(title="Baseline", change_summary="s", visa_categories=["500", "485"],
                           change_type="processing_time")
    store.insert_record(ids["s500"], day, "baseline", old, "u", None)
    change = RegulatoryRecord(title="Change", change_summary="c", visa_categories=["485"],
                              change_type="fees", impact_level="Medium")
    store.insert_record(ids["s500"], date(2026, 11, 4), "change", change, "u", None)
    store.commit()
    todo = retag.plan(store)
    assert len(todo) == 1 and todo[0][3] == (["500"], "baseline", "stated", "none", "none", "Low")
    for rid, _n, _o, new in todo:
        store._exec("update regulatory_records set visa_categories=?, change_type=?, visa_basis=? where id=?",
                    (store._arr(new[0]), new[1], new[2], rid))
    store._exec("update regulatory_records set effective_date_basis='none', impact_basis='none' "
                "where record_type='baseline'")
    store.commit()
    assert retag.plan(store) == []
    feed = {r["record_type"]: r for r in store.feed()}
    assert feed["change"]["visa_categories"] == "485"        # change row untouched
