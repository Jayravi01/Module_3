"""v10 checks: Supabase connection options, Lambda packaging path, and the
SQLite -> Postgres migration.

The Postgres tests need a real database and are skipped unless TEST_DATABASE_URL
is set, e.g.  TEST_DATABASE_URL=postgresql://postgres@localhost:5432/test pytest
They drop and recreate the Module 3 tables, so NEVER point this at Supabase.
"""
import importlib
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from visa_monitor.storage import SQLiteStore, pg_connect_kwargs  # noqa: E402
from test_pipeline import D1, D2, D3, V1, V2, run  # noqa: E402

PG_URL = os.getenv("TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not PG_URL, reason="set TEST_DATABASE_URL to run Postgres tests")


def test_pooler_url_disables_prepared_statements_and_requires_ssl():
    url = "postgresql://postgres.abcd:pw@aws-0-ap-southeast-2.pooler.supabase.com:6543/postgres"
    kw = pg_connect_kwargs(url)
    assert kw["prepare_threshold"] is None
    assert kw["sslmode"] == "require"
    assert kw["connect_timeout"] == 15


def test_url_settings_are_respected():
    url = "postgresql://u:p@db.abcd.supabase.co:5432/postgres?sslmode=verify-full&connect_timeout=5"
    kw = pg_connect_kwargs(url)
    assert "sslmode" not in kw and "connect_timeout" not in kw
    assert pg_connect_kwargs("postgresql://u@localhost/db").get("sslmode") is None


def test_lambda_package_reads_bundled_sources(tmp_path, monkeypatch):
    import visa_monitor.config as cfg
    pkg = tmp_path / "visa_monitor"
    pkg.mkdir()
    for f in (ROOT / "src" / "visa_monitor").glob("*.py"):
        (pkg / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (pkg / "sources.yaml").write_text((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"),
                                      encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for m in [m for m in sys.modules if m.startswith("visa_monitor")]:
        monkeypatch.delitem(sys.modules, m)
    lam_cfg = importlib.import_module("visa_monitor.config")
    assert lam_cfg.DEFAULT_SOURCES_FILE == pkg / "sources.yaml"
    assert len(lam_cfg.load_sources(lam_cfg.DEFAULT_SOURCES_FILE)) == len(cfg.load_sources())


@pytest.fixture
def pg_store():
    from visa_monitor.storage import PostgresStore
    store = PostgresStore(PG_URL)
    store.conn.execute("drop view if exists v_regulatory_feed, v_records_by_visa, v_daily_kpis")
    store.conn.execute("drop table if exists alerts, run_log, regulatory_records, page_snapshots, sources")
    store.commit()
    store.apply_schema(ROOT / "sql" / "001_schema.sql")
    yield store
    store.close()


@needs_pg
def test_pipeline_runs_on_postgres(pg_store):
    run(pg_store, D1, V1)
    run(pg_store, D2, V1)
    run(pg_store, D3, V2)
    feed = pg_store.feed()
    assert {(r["record_date"], r["record_type"]) for r in feed} == {
        ("2026-10-12", "baseline"), ("2026-10-14", "change")}
    # Tableau view works
    assert pg_store.query("select count(*) from v_regulatory_feed")[0][0] == len(feed)


@needs_pg
def test_migration_copies_everything_once(tmp_path, pg_store):
    import migrate_to_supabase as mig
    from zoneinfo import ZoneInfo
    db = tmp_path / "local.db"
    local = SQLiteStore(str(db))
    run(local, D1, V1)
    run(local, D2, V1)
    run(local, D3, V2)
    local_feed = local.feed()
    local.close()

    tz = ZoneInfo("Australia/Melbourne")
    first = mig.migrate(str(db), pg_store, tz)
    assert first["regulatory_records"] == len(local_feed) == 4
    assert first["run_log"] == 3
    second = mig.migrate(str(db), pg_store, tz)          # re-run: nothing duplicated
    assert all(n == 0 for n in second.values())

    pg_feed = pg_store.feed()
    key = lambda r: (r["record_date"], r["record_type"], r["source_name"])  # noqa: E731
    assert sorted(map(key, pg_feed)) == sorted(map(key, local_feed))
    # Melbourne detected_at survives: same wall-clock time after conversion
    for lf in local_feed:
        pf = next(p for p in pg_feed if key(p) == key(lf))
        assert pf["visa_categories"] == lf["visa_categories"]
        melb = datetime.fromisoformat(pf["detected_at"]).astimezone(tz)
        assert melb.strftime("%Y-%m-%d %H:%M:%S") == lf["detected_at"]

    # The daily run continues on top of the migrated history without duplicates
    run(pg_store, date(2026, 10, 15), V2)
    assert len(pg_store.feed()) == 4
