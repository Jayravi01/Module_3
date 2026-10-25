"""One setup script for macOS AND Windows (and Linux).

    python setup_project.py                  # install everything
    python setup_project.py --schedule 07:00 # install + run the monitor daily at 7:00 am
    python setup_project.py --unschedule     # remove the daily schedule

Use python3 on macOS if `python` isn't found.
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
TASK_NAME = "Module3VisaMonitor"
CRON_TAG = "# module3-visa-monitor"


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


# ------------------------------------------------------------------ scheduling
def _launcher() -> Path:
    if IS_WINDOWS:
        return ROOT / "run_monitor.bat"
    sh = ROOT / "run_monitor.sh"
    sh.chmod(0o755)
    return sh


def schedule(time_hhmm: str):
    hh, mm = (int(x) for x in time_hhmm.split(":"))
    step(f"Scheduling the monitor daily at {hh:02d}:{mm:02d}")
    launcher = _launcher()
    if IS_WINDOWS:
        run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/TN", TASK_NAME,
             "/TR", f'"{launcher}"', "/ST", f"{hh:02d}:{mm:02d}"])
        print("   Tip: in Task Scheduler → task Properties → Settings, tick "
              "'Run task as soon as possible after a scheduled start is missed'.")
    else:
        current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        lines = [l for l in current.splitlines() if CRON_TAG not in l]
        log = ROOT / "logs" / "cron.log"
        log.parent.mkdir(exist_ok=True)
        lines.append(f'{mm} {hh} * * * "{launcher}" >> "{log}" 2>&1 {CRON_TAG}')
        subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
        print("   added to crontab (check with: crontab -l)")
        if platform.system() == "Darwin" and any(p in str(ROOT) for p in
                                                 ("/Downloads", "/Documents", "/Desktop")):
            print("   macOS note: the project is in a protected folder. If runs don't happen, "
                  "allow /usr/sbin/cron in System Settings → Privacy & Security → Full Disk Access.")
        print("   The computer must be awake at that time; missed days can be run by hand.")


def unschedule():
    step("Removing the daily schedule")
    if IS_WINDOWS:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    else:
        current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        lines = [l for l in current.splitlines() if CRON_TAG not in l]
        subprocess.run(["crontab", "-"], input="\n".join(lines) + ("\n" if lines else ""),
                       text=True, check=True)
    print("   done")


def main():
    p = argparse.ArgumentParser(description="Set up Module 3 on macOS or Windows")
    p.add_argument("--schedule", metavar="HH:MM", help="also run the monitor every day at this time")
    p.add_argument("--unschedule", action="store_true", help="remove the daily schedule and exit")
    p.add_argument("--skip-browser", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--skip-tests", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args()

    if a.unschedule:
        unschedule()
        return
    try:
        install(a.skip_browser, a.skip_tests)
        if a.schedule:
            schedule(a.schedule)
    except subprocess.CalledProcessError as exc:
        sys.exit(f"\nSetup stopped: this step failed → {' '.join(map(str, exc.cmd))}\n"
                 "Scroll up for the error message.")

    activate = r".venv\Scripts\activate" if IS_WINDOWS else "source .venv/bin/activate"
    sep = "\\" if IS_WINDOWS else "/"
    print(f"""
Setup complete.
Next:
  1. (optional) add OPENAI_API_KEY to .env
  2. {activate}
  3. python scripts{sep}run_daily.py
""")


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
