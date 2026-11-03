r"""One-time setup: virtual environment, packages, headless browser, .env, code checks.

    python setup_project.py

The daily schedule is installed separately on Windows (same as Modules 1, 2 and 4):
    powershell -ExecutionPolicy Bypass -File scripts\install_schedule_windows.ps1
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WINDOWS = platform.system() == "Windows"
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def step(msg: str):
    print(f"\n==> {msg}")


def run(cmd: list[str], **kw):
    print("   $", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, cwd=ROOT, **kw)


def install(skip_browser: bool, skip_tests: bool):
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10+ is needed; this is {platform.python_version()}. "
                 "Install a newer Python from python.org.")
    print(f"Python {platform.python_version()} on {platform.system()}")

    step("Creating virtual environment (.venv)")
    if not VENV_PY.exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    else:
        print("   already exists — reusing it")

    step("Installing packages")
    run([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip", "-q"])
    run([str(VENV_PY), "-m", "pip", "install", "-r", "requirements.txt", "-q"])

    if not skip_browser:
        step("Installing headless browser for JavaScript pages")
        run([str(VENV_PY), "-m", "playwright", "install", "chromium"])

    step("Settings file (.env)")
    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        print("   .env already exists — left unchanged")
    else:
        shutil.copy(example, env)
        print("   created .env from .env.example")

    if not skip_tests:
        step("Running code checks")
        run([str(VENV_PY), "-m", "pytest", "-q", "tests"])


def main():
    p = argparse.ArgumentParser(description="Set up Module 3 (Windows, macOS or Linux)")
    p.add_argument("--skip-browser", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--skip-tests", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args()

    try:
        install(a.skip_browser, a.skip_tests)
    except subprocess.CalledProcessError as exc:
        sys.exit(f"\nSetup stopped: this step failed → {' '.join(map(str, exc.cmd))}\n"
                 "Scroll up for the error message.")

    activate = r".venv\Scripts\activate" if IS_WINDOWS else "source .venv/bin/activate"
    sep = "\\" if IS_WINDOWS else "/"
    print(f"""
Setup complete.
Next:
  1. fill the DB_* and TUNNEL_* lines in .env (copy them from Module 2 or 4)
  2. {activate}
  3. python scripts{sep}db_check.py
  4. python scripts{sep}run_daily.py
  5. Windows daily schedule: powershell -ExecutionPolicy Bypass -File scripts\\install_schedule_windows.ps1
""")


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
