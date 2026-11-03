r"""The command Windows Task Scheduler runs every day (see src/visa_monitor/daily.py).

    python scripts\daily.py              run if due, then email what needs attention
    python scripts\daily.py --dry-run    only say whether a run is due
    python scripts\daily.py --force      run now, ignoring the once-a-day rules
"""
import _bootstrap  # noqa: F401  (import path, project folder, .env)
from visa_monitor.daily import cli

if __name__ == "__main__":
    cli()
