@echo off
:: Kill any existing background instances to prevent duplicates if this is run daily
taskkill /F /FI "WINDOWTITLE eq Outreach Worker*" /T >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Outreach Scheduler*" /T >nul 2>&1

:: Navigate to your Weekly Outreach folder
cd "C:\Users\hr\OneDrive\Desktop\weekly outreach\Project-Weekly-Outreach"

:: Activate the Python virtual environment
call venv\Scripts\activate.bat

:: Start the Celery Worker (using --pool=solo which is required for Windows)
start "Outreach Worker" /MIN celery -A app.workers.celery_app worker --loglevel=info --pool=solo

:: Start the Scheduler
start "Outreach Scheduler" /MIN python run_scheduler.py
