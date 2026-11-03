"""Checks for the AWS RDS connection layer (milestone 3.1).

Most tests need no database. The PostgreSQL tests run only when TEST_DB_HOST is set, e.g.

    $env:TEST_DB_HOST="localhost"; $env:TEST_DB_PORT="5432"; $env:TEST_DB_USER="postgres"
    $env:TEST_DB_PASSWORD="..."; $env:TEST_DB_NAME="test"; python -m pytest -q

They create and DROP the schema ``visa_test``, so never point them at the real medibank database.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from visa_monitor import db  # noqa: E402
from visa_monitor.config import Settings  # noqa: E402
from visa_monitor.env import load_env_file, schema_name  # noqa: E402
from visa_monitor.storage import PostgresStore  # noqa: E402
from visa_monitor.tunnel import explain_ssh_error, tunnel_settings  # noqa: E402
from test_pipeline import D1, D2, D3, V1, V2, run  # noqa: E402

# ----------------------------------------------------------------- .env loading
def test_env_file_reads_values_ignores_comments_and_bom(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("﻿# comment\nM3_A=one\nM3_B = 'two words'\n\nM3_C=\"x=y\"\n", encoding="utf-8")
    for k in ("M3_A", "M3_B", "M3_C"):
        monkeypatch.delenv(k, raising=False)
    assert load_env_file(env) is True
    assert (os.environ["M3_A"], os.environ["M3_B"], os.environ["M3_C"]) == ("one", "two words", "x=y")


def test_real_environment_wins_over_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("M3_D=from_file\n", encoding="utf-8")
    monkeypatch.setenv("M3_D", "from_scheduler")
    load_env_file(env)
    assert os.environ["M3_D"] == "from_scheduler"


def test_missing_env_file_is_not_an_error(tmp_path):
    assert load_env_file(tmp_path / "nope.env") is False


# ----------------------------------------------------------------- schema name
def test_schema_defaults_to_visa():
    assert schema_name({}) == "visa"


@pytest.mark.parametrize("other", ["intel", "pricing", "partnership"])
def test_other_modules_schemas_are_refused(other):
    with pytest.raises(ValueError, match="belongs to"):
        schema_name({"DB_SCHEMA": other})


@pytest.mark.parametrize("bad", ["Visa", "visa;drop", "1visa", "visa-x"])
def test_unsafe_schema_names_are_refused(bad):
    with pytest.raises(ValueError):
        schema_name({"DB_SCHEMA": bad})


# ----------------------------------------------------------------- connection settings
def test_no_db_host_means_not_configured():
    assert db.config_from_env({}) is None


def test_missing_password_is_reported_by_name():
    with pytest.raises(db.DatabaseNotConfigured, match="DB_PASSWORD"):
        db.config_from_env({"DB_HOST": "localhost", "DB_USER": "u"})


def test_config_uses_keywords_and_never_a_url():
    cfg = db.config_from_env({"DB_HOST": "localhost", "DB_PORT": "5433", "DB_USER": "u", "DB_PASSWORD": "secret"})
    assert cfg["port"] == 5433 and cfg["dbname"] == "medibank" and cfg["sslmode"] == "require"
    assert "secret" not in db.describe(cfg)
    assert not any("://" in str(v) for v in cfg.values())


def test_storage_backend_follows_db_host(monkeypatch):
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("DB_HOST", "")
    assert Settings().storage_backend == "sqlite"
    monkeypatch.setenv("DB_HOST", "localhost")
    assert Settings().storage_backend == "postgres"
    monkeypatch.setenv("STORAGE_BACKEND", "SQLite")
    assert Settings().storage_backend == "sqlite"


# ----------------------------------------------------------------- tunnel
def test_tunnel_only_used_for_localhost():
    assert tunnel_settings({"TUNNEL_SSH_HOST": "1.2.3.4", "DB_HOST": "db.aws.com"}) is None
    t = tunnel_settings({"TUNNEL_SSH_HOST": "1.2.3.4", "DB_HOST": "localhost", "DB_PORT": "5433"})
    assert t["local_port"] == 5433 and t["ssh_user"] == "ec2-user"


def test_ssh_errors_are_explained_in_plain_english():
    assert "security group" in explain_ssh_error("ssh: connect to host 1.2.3.4 port 22: Connection timed out")
    assert "rejected the key" in explain_ssh_error("Permission denied (publickey).")


# ----------------------------------------------------------------- real PostgreSQL
PG = {k: os.getenv(f"TEST_DB_{k.upper()}") for k in ("host", "port", "user", "password", "name")}
needs_pg = pytest.mark.skipif(not PG["host"], reason="set TEST_DB_HOST to run PostgreSQL tests")
TEST_SCHEMA = "visa_test"


@pytest.fixture
def pg_store():
    cfg = {"host": PG["host"], "port": int(PG["port"] or 5432), "dbname": PG["name"] or "postgres",
           "user": PG["user"] or "postgres", "password": PG["password"] or "", "sslmode": "prefer",
           "connect_timeout": 10}
    admin = db.connect(cfg)
    admin.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
    admin.commit()
    store = PostgresStore(db.connect(cfg, TEST_SCHEMA))
    db.apply_schema(store.conn, TEST_SCHEMA)
    yield store
    store.close()
    admin.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
    admin.commit()
    admin.close()


def _count(store, sql):
    return store.query(sql)[0][0]


@needs_pg
def test_tables_and_views_are_created_inside_the_schema(pg_store):
    names = {r[0] for r in pg_store.query(
        "select table_name from information_schema.tables where table_schema = %s", (TEST_SCHEMA,))}
    assert {"sources", "page_snapshots", "regulatory_records", "run_log", "alerts",
            "v_regulatory_feed", "v_records_by_visa", "v_daily_kpis",
            "tableau_records", "tableau_visa_by_record", "tableau_run_log", "tableau_source_status"} <= names


@needs_pg
def test_pipeline_history_on_postgres(pg_store):
    run(pg_store, D1, V1)              # baselines
    run(pg_store, D2, V1)              # nothing new
    s3 = run(pg_store, D3, V2)         # changes
    assert s3["changes"] == 2
    assert _count(pg_store, f"select count(*) from {TEST_SCHEMA}.regulatory_records where record_type='baseline'") == 2
    assert _count(pg_store, f"select count(*) from {TEST_SCHEMA}.v_regulatory_feed where record_type='change'") == 2
    assert _count(pg_store, f"select count(*) from {TEST_SCHEMA}.run_log") == 3


@needs_pg
def test_rerun_same_day_on_postgres_adds_nothing(pg_store):
    run(pg_store, D1, V1)
    before = _count(pg_store, "select count(*) from regulatory_records")
    run(pg_store, D1, V1)
    assert _count(pg_store, "select count(*) from regulatory_records") == before


@needs_pg
def test_schema_sql_is_safe_to_apply_again(pg_store):
    run(pg_store, D1, V1)
    db.apply_schema(pg_store.conn, TEST_SCHEMA)        # every run does this
    assert _count(pg_store, "select count(*) from regulatory_records") == 2


# ----------------------------------------------------------------- Tableau views (3.5)
from datetime import date  # noqa: E402

from visa_monitor.scraper import FetchResult  # noqa: E402

CSV_HEADERS = ["Record ID", "Record Date", "Record Type", "Title", "Change Summary", "Effective Date",
               "Visa Categories Affected", "Change Type", "Impact Level", "Impact Assessment",
               "Affects Health Cover", "Confidence", "Classifier", "Source Link", "Source", "Agency",
               "Source Category", "Detected At"]


def _columns(store, view):
    return [r[0] for r in store.query(
        "select column_name from information_schema.columns where table_schema = %s and table_name = %s "
        "order by ordinal_position", (TEST_SCHEMA, view))]


@needs_pg
def test_tableau_views_keep_the_csv_column_names(pg_store):
    """The workbook was built on CSV headers: the views must use exactly the same names."""
    assert _columns(pg_store, "tableau_records")[:18] == CSV_HEADERS
    assert _columns(pg_store, "tableau_visa_by_record") == ["Record ID", "Visa Subclass"]
    assert _columns(pg_store, "tableau_run_log")[:3] == ["run_date", "started_at", "finished_at"]


@needs_pg
def test_tableau_views_content(pg_store):
    run(pg_store, D1, V1)
    run(pg_store, D3, V2)
    rows = pg_store.query('select "Record Type", "Visa Basis", "Effective Date" from tableau_records '
                          'where "Record Type" = %s', ("change",))
    assert len(rows) == 2 and all(r[1] in ("stated", "inferred", "none") for r in rows)
    visas = pg_store.query('select distinct "Visa Subclass" from tableau_visa_by_record')
    assert visas == [(500,)]
    finished = pg_store.query("select finished_at from tableau_run_log order by run_date")[0][0]
    assert len(finished) == 19 and finished[10] == "T"


@needs_pg
def test_source_status_ok_blocked_unavailable(pg_store):
    ids = pg_store.sync_sources([
        {"key": k, "name": k, "url": f"https://example.gov.au/{k}", "priority": p}
        for k, p in (("ok", 1), ("blocked", 1), ("down", 2), ("new", 1))])
    day = date(2026, 11, 3)
    pg_store.save_snapshot(ids["ok"], day, FetchResult("u", True, 200, "text", "h"))
    pg_store.save_snapshot(ids["blocked"], day, FetchResult("u", False, 403, None, None, "HTTP 403 via static"))
    pg_store.save_snapshot(ids["down"], day, FetchResult("u", False, 503, None, None, "HTTP 503"))
    pg_store.commit()
    status = dict(pg_store.query('select "Source", "Status" from tableau_source_status'))
    assert status == {"ok": "OK", "blocked": "Scrape blocked", "down": "Unavailable", "new": "Not checked yet"}
    prio = dict(pg_store.query('select "Source", "Priority" from tableau_source_status'))
    assert prio["down"] == "Secondary" and prio["ok"] == "Core"
