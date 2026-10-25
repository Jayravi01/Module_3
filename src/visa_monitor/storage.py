"""Storage layer. Two interchangeable back ends with the same interface:

* PostgresStore — Supabase (production). Uses sql/001_schema.sql.
* SQLiteStore   — local demo and automated tests, zero setup.

Every write is an INSERT; history is never overwritten. The only exception is
re-trying a source that FAILED earlier the same day, which replaces that
failed snapshot so a transient outage doesn't block the day's record.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .classifier import RegulatoryRecord

SQLITE_SCHEMA = """
create table if not exists sources (
    id integer primary key autoincrement, key text unique not null, name text not null,
    agency text, url text unique not null, category text, visa_hint text default '[]',
    render text default 'static', selector text, priority int default 1,
    active int default 1, added_at text default current_timestamp);
create table if not exists page_snapshots (
    id integer primary key autoincrement, source_id int not null references sources(id),
    snapshot_date text not null, content_hash text, content text, http_status int,
    fetch_ok int not null, error text, scraped_at text default current_timestamp,
    unique (source_id, snapshot_date));
create table if not exists regulatory_records (
    id integer primary key autoincrement, source_id int not null references sources(id),
    record_date text not null, record_type text not null, title text, change_summary text,
    effective_date text, visa_categories text default '[]', change_type text,
    impact_level text, impact_reason text, affects_health_cover int default 0,
    confidence real, classifier text, source_url text, diff_excerpt text,
    detected_at text default current_timestamp,
    qa_is_real_change int, qa_impact_correct int, qa_categories_correct int, qa_reviewed_by text,
    unique (source_id, record_date, record_type));
create table if not exists run_log (
    run_date text primary key, started_at text, finished_at text, sources_checked int,
    sources_failed int, core_sources_total int, core_sources_ok int,
    baselines_created int, changes_found int, alerts_sent int, status text);
create table if not exists alerts (
    id integer primary key autoincrement, record_id int, alert_date text not null,
    impact_level text, channel text, sent_at text default current_timestamp, delivered int);
"""

FEED_COLUMNS = ["id", "record_date", "record_type", "title", "change_summary", "effective_date",
                "visa_categories", "change_type", "impact_level", "impact_reason",
                "affects_health_cover", "confidence", "classifier", "source_url",
                "source_name", "agency", "source_category", "detected_at"]


class BaseStore:
    ph = "?"              # placeholder style
    array_as_json = True  # sqlite stores lists as JSON text

    def __init__(self, conn):
        self.conn = conn

    # -- helpers ----------------------------------------------------------
    def _sql(self, q: str) -> str:
        return q.replace("?", self.ph)

    def _arr(self, items: list[str]) -> Any:
        return json.dumps(items) if self.array_as_json else list(items)

    def _exec(self, q: str, params: Iterable = ()):
        cur = self.conn.cursor()
        cur.execute(self._sql(q), tuple(params))
        return cur

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- sources ----------------------------------------------------------
    def sync_sources(self, sources: list[dict]) -> dict[str, int]:
        """Insert new sources, refresh metadata of existing ones. Returns key -> id."""
        for s in sources:
            row = self._exec("select id from sources where key = ?", (s["key"],)).fetchone()
            values = (s["name"], s.get("agency"), s["url"], s.get("category"),
                      self._arr(s.get("visa_hint", [])), s.get("render", "static"),
                      s.get("selector"), s.get("priority", 1))
            if row:
                self._exec("update sources set name=?, agency=?, url=?, category=?, visa_hint=?, "
                           "render=?, selector=?, priority=?, active=? where key=?",
                           (*values, self._true(), s["key"]))
            else:
                self._exec("insert into sources (name, agency, url, category, visa_hint, render, "
                           "selector, priority, active, key) values (?,?,?,?,?,?,?,?,?,?)",
                           (*values, self._true(), s["key"]))
        # Sources removed from the YAML are deactivated, never deleted (history stays linked)
        keys = [s["key"] for s in sources]
        for (key,) in self._exec("select key from sources").fetchall():
            if key not in keys:
                self._exec("update sources set active=? where key=?", (self._false(), key))
        self.commit()
        return {k: i for k, i in self._exec("select key, id from sources").fetchall()}

    def _d(self, d):
        """Dates: ISO text for SQLite, native date for Postgres."""
        if d is None:
            return None
        if isinstance(d, str):
            d = date.fromisoformat(d)
        return d.isoformat() if self.array_as_json else d

    def _ts(self, dt: datetime):
        """Timestamps: local 'YYYY-MM-DD HH:MM:SS' text for SQLite/CSV, aware datetime for Postgres."""
        return dt.strftime("%Y-%m-%d %H:%M:%S") if self.array_as_json else dt

    def _true(self):
        return 1 if self.array_as_json else True

    def _false(self):
        return 0 if self.array_as_json else False

    # -- snapshots --------------------------------------------------------
    def snapshot_ok_today(self, source_id: int, day: date) -> bool:
        row = self._exec("select fetch_ok from page_snapshots where source_id=? and snapshot_date=?",
                         (source_id, self._d(day))).fetchone()
        return bool(row and row[0])

    def previous_snapshot(self, source_id: int, before: date) -> tuple[str, str] | None:
        row = self._exec(
            "select content_hash, content from page_snapshots where source_id=? and fetch_ok=? "
            "and snapshot_date < ? order by snapshot_date desc limit 1",
            (source_id, self._true(), self._d(before))).fetchone()
        return (row[0], row[1]) if row else None

    def save_snapshot(self, source_id: int, day: date, fetch) -> None:
        # Replace only a FAILED snapshot from earlier today (retry); otherwise insert.
        self._exec("delete from page_snapshots where source_id=? and snapshot_date=? and fetch_ok=?",
                   (source_id, self._d(day), self._false()))
        self._exec("insert into page_snapshots (source_id, snapshot_date, content_hash, content, "
                   "http_status, fetch_ok, error) values (?,?,?,?,?,?,?)",
                   (source_id, self._d(day), fetch.content_hash,
                    fetch.text if fetch.ok else None, fetch.status,
                    self._true() if fetch.ok else self._false(), fetch.error))

    # -- records ----------------------------------------------------------
    def insert_record(self, source_id: int, day: date, record_type: str,
                      rec: RegulatoryRecord, source_url: str, diff_excerpt: str | None,
                      detected_at: datetime | None = None) -> int | None:
        exists = self._exec("select id from regulatory_records where source_id=? and record_date=? "
                            "and record_type=?", (source_id, self._d(day), record_type)).fetchone()
        if exists:
            return None
        cur = self._exec(
            "insert into regulatory_records (source_id, record_date, record_type, title, "
            "change_summary, effective_date, visa_categories, change_type, impact_level, "
            "impact_reason, affects_health_cover, confidence, classifier, source_url, diff_excerpt, "
            "detected_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)" + self._returning(),
            (source_id, self._d(day), record_type, rec.title, rec.change_summary,
             self._d(rec.effective_date) if rec.effective_date else None,
             self._arr(rec.visa_categories), rec.change_type, rec.impact_level,
             rec.impact_reason, self._true() if rec.affects_health_cover else self._false(),
             rec.confidence, rec.classifier, source_url, (diff_excerpt or "")[:4000],
             self._ts(detected_at or datetime.now().astimezone())))
        return self._last_id(cur)

    def _returning(self) -> str:
        return ""

    def _last_id(self, cur) -> int:
        return cur.lastrowid

    # -- runs & alerts ----------------------------------------------------
    def log_run(self, run: dict) -> None:
        run = {**run, "run_date": self._d(run["run_date"])}
        self._exec("delete from run_log where run_date=?", (run["run_date"],))
        cols = list(run)
        self._exec(f"insert into run_log ({', '.join(cols)}) values ({', '.join('?' * len(cols))})",
                   [run[c] for c in cols])

    def log_alert(self, record_id: int, day: date, level: str, channel: str, delivered: bool):
        self._exec("insert into alerts (record_id, alert_date, impact_level, channel, delivered) "
                   "values (?,?,?,?,?)", (record_id, self._d(day), level, channel,
                                          self._true() if delivered else self._false()))

    # -- reads for export / KPIs -------------------------------------------
    def feed(self, start: date | None = None, end: date | None = None) -> list[dict]:
        q = ("select r.id, r.record_date, r.record_type, r.title, r.change_summary, r.effective_date, "
             "r.visa_categories, r.change_type, r.impact_level, r.impact_reason, "
             "r.affects_health_cover, r.confidence, r.classifier, r.source_url, s.name, s.agency, "
             "s.category, r.detected_at from regulatory_records r join sources s on s.id = r.source_id "
             "where 1=1")
        params: list = []
        if start:
            q += " and r.record_date >= ?"
            params.append(self._d(start))
        if end:
            q += " and r.record_date <= ?"
            params.append(self._d(end))
        q += " order by r.record_date desc, r.impact_level, s.name"
        rows = []
        for row in self._exec(q, params).fetchall():
            d = dict(zip(FEED_COLUMNS, row))
            cats = d["visa_categories"]
            if isinstance(cats, str):
                cats = json.loads(cats or "[]")
            d["visa_categories"] = ", ".join(cats or [])
            d["affects_health_cover"] = bool(d["affects_health_cover"])
            for k in ("record_date", "effective_date", "detected_at"):
                if isinstance(d[k], (date, datetime)):
                    d[k] = d[k].isoformat()
            rows.append(d)
        return rows

    def runs(self) -> list[dict]:
        cur = self._exec("select * from run_log order by run_date")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def query(self, q: str, params: Iterable = ()) -> list[tuple]:
        return self._exec(q, params).fetchall()


class SQLiteStore(BaseStore):
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.executescript(SQLITE_SCHEMA)
        super().__init__(conn)


class PostgresStore(BaseStore):
    ph = "%s"
    array_as_json = False

    def __init__(self, dsn: str):
        import psycopg  # psycopg 3
        super().__init__(psycopg.connect(**pg_connect_kwargs(dsn)))

    def apply_schema(self, schema_path: str | Path) -> None:
        """Create tables and views from sql/001_schema.sql (safe to run again)."""
        self.conn.execute(Path(schema_path).read_text(encoding="utf-8"))
        self.commit()


    def _returning(self) -> str:
        return " returning id"

    def _last_id(self, cur) -> int:
        return cur.fetchone()[0]


def pg_connect_kwargs(dsn: str) -> dict:
    """Connection options that work with Supabase, locally and on AWS Lambda.

    * Supabase's pooler (Supavisor, port 6543, transaction mode) does not support
      server-side prepared statements. psycopg 3 prepares a query after it has run
      5 times, which then fails mid-run with 'prepared statement ... does not exist'.
      prepare_threshold=None turns that off. Harmless on a direct connection.
    * Supabase requires SSL; sslmode=require is added for supabase hosts unless the
      URL already sets it.
    * connect_timeout stops a Lambda from hanging for 15 minutes on a bad URL.
    """
    from urllib.parse import parse_qs, urlparse

    dsn = dsn.strip()
    query = parse_qs(urlparse(dsn).query) if "://" in dsn else {}
    kwargs: dict = {"conninfo": dsn, "prepare_threshold": None}
    host = (urlparse(dsn).hostname or "") if "://" in dsn else ""
    if "supabase." in host and "sslmode" not in query:
        kwargs["sslmode"] = "require"
    if "connect_timeout" not in query:
        kwargs["connect_timeout"] = 15
    return kwargs


def get_store(settings) -> BaseStore:
    if settings.storage_backend == "postgres":
        if not settings.database_url:
            raise RuntimeError("STORAGE_BACKEND=postgres but DATABASE_URL is empty")
        return PostgresStore(settings.database_url)
    return SQLiteStore(settings.sqlite_path)
