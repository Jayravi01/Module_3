"""Private settings: reading the local .env file, and the database schema name.

WHY THIS FILE EXISTS
--------------------
The database password, the jumpbox address and the Gmail app password must never be
written in code, in Git, or in a command (PowerShell keeps a history of what you type).
They live in a file called ``.env`` in the project folder. ``.gitignore`` excludes it, so
it only ever exists on your laptop.

``load_env_file`` copies the file's KEY=VALUE lines into ``os.environ`` (the environment
variables of the running Python process). ``config.Settings``, ``db`` and ``tunnel`` read
them from there. The .env format is the SAME as Modules 1, 2 and 4, so the DB_*, TUNNEL_*
and ALERT_* lines can be copied from those projects unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # project folder (src/visa_monitor/ -> ../..)
ENV_PATH = ROOT / ".env"
LOG_DIR = ROOT / "logs"                         # tunnel.log (ssh error text) goes here; not in Git

DEFAULT_SCHEMA = "visa"
# Schemas that belong to the other modules in the same database. Writing Module 3 tables
# into one of them because of a typo would mix two modules' data, so they are refused.
OTHER_MODULE_SCHEMAS = {
    "intel": "Module 1 (promotions)",
    "pricing": "Module 2 (pricing)",
    "partnership": "Module 4 (partnerships)",
}


def load_env_file(path: Path | str = ENV_PATH) -> bool:
    """Read KEY=VALUE lines from ``path`` into the environment.

    Rules (same as the other modules):
      * blank lines and lines starting with ``#`` are ignored;
      * a value may be wrapped in single or double quotes;
      * a variable that is ALREADY set in the real environment wins (``setdefault``),
        so Task Scheduler or a test can override .env without editing it.

    Returns True when a file was read, False when there is no .env (not an error: the
    monitor then uses the local SQLite file instead of AWS).
    """
    path = Path(path)
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8-sig").splitlines():   # -sig: ignore a BOM from Notepad
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return True


def schema_name(environ: dict | None = None) -> str:
    """The PostgreSQL schema for Module 3 tables: ``DB_SCHEMA`` or ``visa``.

    The name is placed directly into SQL (a schema name cannot be passed as a query
    parameter), so it is validated: lowercase letters, digits and underscores only, not
    starting with a digit, and not another module's schema.
    """
    env = os.environ if environ is None else environ
    name = (env.get("DB_SCHEMA") or DEFAULT_SCHEMA).strip()
    if not name or not name.replace("_", "").isalnum() or not name.islower() or name[0].isdigit():
        raise ValueError(f"DB_SCHEMA must be lowercase letters, digits and _ only, got {name!r}")
    if name in OTHER_MODULE_SCHEMAS:
        raise ValueError(f"DB_SCHEMA must not be {name!r}: that schema belongs to {OTHER_MODULE_SCHEMAS[name]}")
    return name
