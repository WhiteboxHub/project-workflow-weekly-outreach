#!/usr/bin/env python3
"""
Scheduler runner - runs the campaign scheduler continuously or as a one-off job.

Usage:
    # Run once
    python run_scheduler.py --once

    # Run continuously (every 60 seconds)
    python run_scheduler.py

    # Run with custom interval
    python run_scheduler.py --interval 30
"""

import argparse
import time
import sys
from pathlib import Path

# Add app directory to path
sys.path.insert(0, str(Path(__file__).parent))

from app.core.logging import configure_logging, get_logger
from app.scheduler import run_scheduler
from app.core.config import settings

# Configure logging
configure_logging()
logger = get_logger(__name__)


def main() -> None:
    """Main scheduler runner."""
    parser = argparse.ArgumentParser(description="Email Outreach Campaign Scheduler")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run scheduler once and exit",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=settings.scheduler_interval_seconds,
        help="Interval between runs in seconds (default: 60)",
    )

    args = parser.parse_args()

    logger.info(
        "scheduler_starting",
        run_once=args.once,
        interval_seconds=args.interval,
    )

    try:
        if args.once:
            # Run once and exit
            _run_scheduler_once()
        else:
            # Run continuously
            _run_scheduler_continuously(args.interval)

    except KeyboardInterrupt:
        logger.info("scheduler_stopped_by_user")
    except Exception as e:
        logger.error("scheduler_fatal_error", error=str(e), exc_info=True)
        sys.exit(1)


def _run_scheduler_once() -> None:
    """Run scheduler once."""
    logger.info("scheduler_run_starting")

    try:
        stats = run_scheduler()
        logger.info("scheduler_run_completed", **stats)
    except Exception as e:
        logger.error("scheduler_critical_failure", error=str(e))


def _run_scheduler_continuously(interval_seconds: int) -> None:
    """
    Run scheduler continuously at specified interval.

    Args:
        interval_seconds: Seconds between each run
    """
    logger.info("scheduler_continuous_mode", interval_seconds=interval_seconds)

    while True:
        start_time = time.time()

        try:
            _run_scheduler_once()
        except Exception as e:
            logger.error("scheduler_run_error", error=str(e), exc_info=True)

        # Calculate sleep time to maintain interval
        elapsed = time.time() - start_time
        sleep_time = max(0, interval_seconds - elapsed)

        if sleep_time > 0:
            logger.debug("scheduler_sleeping", seconds=sleep_time)
            time.sleep(sleep_time)
        else:
            logger.warning(
                "scheduler_run_exceeded_interval",
                elapsed_seconds=elapsed,
                interval_seconds=interval_seconds,
            )


if __name__ == "__main__":
    main()
