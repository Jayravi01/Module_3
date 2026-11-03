"""Shared start-up for every script: import path, project folder, .env, and the store.

Lets scripts run from anywhere without installing the package.
"""
import atexit
import contextlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)   # relative paths (data/, exports/) always resolve to the project folder

from visa_monitor.env import load_env_file  # noqa: E402

load_env_file()  # .env -> environment (the real environment wins; BOM-safe)


def open_store():
    """The store this script works with: AWS RDS (schema visa) when DB_HOST is set in .env,
    otherwise the local SQLite file. The connection and the SSH tunnel are closed
    automatically when the script ends, even after an error."""
    from visa_monitor.config import Settings
    from visa_monitor.db import open_store as _open_store

    stack = contextlib.ExitStack()
    atexit.register(stack.close)
    return stack.enter_context(_open_store(Settings()))
