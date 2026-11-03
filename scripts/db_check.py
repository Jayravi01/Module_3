"""Check the AWS database connection and create the ``visa`` schema, tables and views.

    python scripts\\db_check.py              # connect, create what is missing, report
    python scripts\\db_check.py --no-create  # connect and report only

Exit codes (useful for Task Scheduler and scripts):
    0  connection OK
    1  connection or tunnel failed (the message says why)
    2  database not configured in .env (DB_HOST empty) or settings incomplete

Steps:
    1. read .env                                (_bootstrap -> visa_monitor.env.load_env_file)
    2. open the SSH tunnel if .env asks for it  (visa_monitor.tunnel.SshTunnel; an already-open
       tunnel on DB_PORT, e.g. another module's, is reused and left running)
    3. connect with search_path=visa, CREATE SCHEMA / tables / views IF NOT EXISTS, list them
"""
import argparse
import contextlib
import sys

import _bootstrap
from visa_monitor import db
from visa_monitor.env import ENV_PATH, schema_name
from visa_monitor.tunnel import SshTunnel, TunnelError, tunnel_settings


def run(create: bool = True) -> int:
    try:
        config = db.config_from_env()
        schema = schema_name()
    except (db.DatabaseNotConfigured, ValueError) as exc:
        print(f"NOT CONFIGURED: {exc}")
        return 2
    if config is None:
        where = ENV_PATH if ENV_PATH.exists() else f"{ENV_PATH} (file not found)"
        print(f"NOT CONFIGURED: DB_HOST is empty. Fill the DB_* settings in {where}; see .env.example.")
        return 2

    settings = tunnel_settings()
    print(f"Database : {db.describe(config)}  schema={schema}")
    print(f"Tunnel   : {'via ' + settings['ssh_host'] if settings else 'not used (DB_HOST is not localhost or TUNNEL_SSH_HOST empty)'}")
    try:
        with (SshTunnel(settings) if settings else contextlib.nullcontext()):
            report = db.check(config, schema, create=create)
    except TunnelError as exc:
        print(f"TUNNEL FAILED: {exc}")
        return 1
    except Exception as exc:                       # psycopg errors never contain the password
        print(f"DATABASE FAILED: {type(exc).__name__}: {exc}")
        return 1

    print(f"PostgreSQL {report['server_version']}")
    print(f"Schema '{report['schema']}': {'exists' if report['schema_exists'] else 'MISSING'}")
    print(f"  tables ({len(report['tables'])}): {', '.join(report['tables']) or '-'}")
    print(f"  views  ({len(report['views'])}): {', '.join(report['views']) or '-'}")
    print("DB CHECK OK")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check the AWS PostgreSQL connection for Module 3.")
    parser.add_argument("--no-create", action="store_true", help="do not create anything, only report")
    sys.exit(run(create=not parser.parse_args().no_create))
