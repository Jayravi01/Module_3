"""Connection to the AWS RDS PostgreSQL database, schema ``visa``.

WHERE THIS FITS
---------------
    sources -> scraper -> diff -> classifier -> storage --> AWS RDS  database "medibank"
                                                             schema "visa"  (this file)
                                                             -> Tableau views, CSV copies

It is the SAME database (``medibank``) as Modules 1, 2 and 4, reached through the SAME SSH
tunnel, but every Module 3 table lives in its own SCHEMA, ``visa``. A schema is a named
folder of tables inside one database, so intel.* (M1), pricing.* (M2), partnership.* (M4)
and visa.* (M3) can never overwrite each other.

The connection sets ``search_path`` to the schema: "when a query says ``sources``, look in
``visa`` first". That is why the SQL in storage.py and sql/001_schema.sql does not need
``visa.`` in front of every table name.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Iterator

from .env import ROOT, schema_name

SCHEMA_SQL = ROOT / "sql" / "001_schema.sql"


class DatabaseNotConfigured(RuntimeError):
    """The database is needed but a required .env setting is missing."""


def config_from_env(environ: dict | None = None) -> dict | None:
    """Connection settings as a dict, or None when DB_HOST is empty (AWS not configured).

    The settings are kept as separate keywords (host, port, ...) and never joined into one
    "postgresql://user:password@host" URL, so the password cannot leak into an error
    message, a log line, or your PowerShell history.
    """
    env = os.environ if environ is None else environ
    host = (env.get("DB_HOST") or "").strip()
    if not host:
        return None
    missing = [name for name in ("DB_USER", "DB_PASSWORD") if not env.get(name)]
    if missing:
        raise DatabaseNotConfigured("DB_HOST is set but these are missing in .env: " + ", ".join(missing))
    return {
        "host": host,
        "port": int(env.get("DB_PORT") or 5432),
        "dbname": env.get("DB_NAME") or "medibank",
        "user": env["DB_USER"],
        "password": env["DB_PASSWORD"],
        "sslmode": env.get("DB_SSLMODE") or "require",   # encrypted connection to AWS
        "connect_timeout": 15,                            # seconds: fail fast if the tunnel is down
    }


def describe(config: dict) -> str:
    """Safe one-line description for logs: everything EXCEPT the password."""
    return f"{config['user']}@{config['host']}:{config['port']}/{config['dbname']}"


def connect(config: dict, schema: str | None = None):
    """Open a psycopg connection whose search_path is ``schema`` (then public).

    psycopg is imported here, not at the top, so the tests and an offline SQLite run work
    on a laptop where the PostgreSQL driver is not installed.
    """
    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit("PostgreSQL driver missing: python -m pip install -r requirements.txt") from exc
    options = f"-c search_path={schema},public" if schema else None
    return psycopg.connect(**config, options=options) if options else psycopg.connect(**config)


def ensure_schema(conn, schema: str) -> None:
    """Create the schema if it does not exist yet (safe to run every time: IF NOT EXISTS)."""
    with conn.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    conn.commit()


def apply_schema(conn, schema: str, sql_path: Path = SCHEMA_SQL) -> None:
    """Create the schema, then the tables and views from sql/001_schema.sql (all IF NOT EXISTS /
    CREATE OR REPLACE, so it is safe on every run). search_path puts them in ``schema``."""
    ensure_schema(conn, schema)
    conn.execute(Path(sql_path).read_text(encoding="utf-8"))
    conn.commit()


def check(config: dict, schema: str | None = None, create: bool = True) -> dict:
    """Connect, optionally create the schema + tables, and report what is there."""
    schema = schema or schema_name()
    with connect(config, schema) as conn:
        if create:
            apply_schema(conn, schema)
        with conn.cursor() as cursor:
            cursor.execute("SHOW server_version")
            version = cursor.fetchone()[0]
            cursor.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,))
            exists = cursor.fetchone() is not None
            cursor.execute("SELECT table_name, table_type FROM information_schema.tables "
                           "WHERE table_schema = %s ORDER BY 1", (schema,))
            rows = cursor.fetchall()
    return {"server_version": version, "schema": schema, "schema_exists": exists,
            "tables": [t for t, kind in rows if kind == "BASE TABLE"],
            "views": [t for t, kind in rows if kind == "VIEW"]}


@contextlib.contextmanager
def open_store(settings) -> Iterator:
    """The ONE way scripts get a store. Use as ``with open_store(settings) as store:``.

    * STORAGE_BACKEND=postgres (the default when DB_HOST is set in .env):
      open the SSH tunnel if .env asks for one (an already-open tunnel on DB_PORT is reused
      and left running), connect with search_path=visa, create schema/tables/views if
      missing, yield the store; close the connection and the tunnel afterwards.
    * STORAGE_BACKEND=sqlite: the local file data/visa_monitor.db (offline runs and tests).
    """
    from .storage import PostgresStore, SQLiteStore       # local import: storage imports classifier
    from .tunnel import SshTunnel, tunnel_settings

    if settings.storage_backend != "postgres":
        store = SQLiteStore(settings.sqlite_path)
        try:
            yield store
        finally:
            store.close()
        return

    config = config_from_env()
    if config is None:
        raise DatabaseNotConfigured("STORAGE_BACKEND=postgres but DB_HOST is empty in .env (see .env.example)")
    schema = schema_name()
    tunnel = tunnel_settings()
    with (SshTunnel(tunnel) if tunnel else contextlib.nullcontext()):
        store = PostgresStore(connect(config, schema))
        try:
            apply_schema(store.conn, schema)
            yield store
        finally:
            store.close()
