#!/usr/bin/env python3
"""
Daily Report Daemon

Runs as a long-lived background service inside Docker.
Wakes up every 60 seconds, checks if it is 17:00 (5:00 PM) Pacific time,
and fires the daily outreach report exactly once per day.

Deploy via docker-compose — no Windows Task Scheduler or cron needed.
"""

import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from app.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

# Fire the report at this hour/minute (24h clock, Pacific time)
# Container must have TZ=America/Los_Angeles set in docker-compose.yml
REPORT_HOUR = 17     # 5 PM
REPORT_MINUTE = 0    # :00

def _run_report() -> None:
    """
    Run the daily report in a subprocess.

    Using subprocess instead of importlib.reload() avoids side-effects:
    - configure_logging() in run_daily_report.py is only called once (in the child)
    - module-level httpx clients and loggers start fresh
    - a crash in the report never kills this daemon
    """
    try:
        result = subprocess.run(
            [sys.executable, "run_daily_report.py"],
            cwd=str(Path(__file__).parent),
            capture_output=False,   # let stdout/stderr flow to Docker logs
            timeout=300,            # 5 minute hard timeout
        )
        if result.returncode == 0:
            logger.info("report_service_report_sent", date=str(date.today()))
        else:
            logger.error(
                "report_service_report_nonzero_exit",
                returncode=result.returncode,
                date=str(date.today()),
            )
    except subprocess.TimeoutExpired:
        logger.error("report_service_report_timeout", date=str(date.today()))
    except Exception as e:
        logger.error("report_service_report_failed", error=str(e), exc_info=True)


def main() -> None:
    logger.info("report_service_started", report_hour=REPORT_HOUR, report_minute=REPORT_MINUTE)

    last_run_date: Optional[date] = None  # track which date we last fired

    while True:
        now = datetime.now()

        if now.hour == REPORT_HOUR and now.minute >= REPORT_MINUTE and last_run_date != now.date():
            logger.info("report_service_firing", time=now.isoformat())
            _run_report()
            last_run_date = now.date()

        # Sleep 60 s before checking again — lightweight polling
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("report_service_stopped")
    except Exception as e:
        logger.error("report_service_fatal", error=str(e), exc_info=True)
        sys.exit(1)
