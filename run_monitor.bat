@echo off
REM Windows: double-click to run the live monitor, or point Task Scheduler at this file.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python scripts\run_daily.py
