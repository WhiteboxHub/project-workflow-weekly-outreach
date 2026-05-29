#!/usr/bin/env python3
"""
Daily Outreach Report Runner

Fetches today's campaign_email records from the Orchestrator API,
aggregates stats per candidate, and sends one consolidated HTML
report email to the configured admin address.

Usage:
    python run_daily_report.py

Schedule via Windows Task Scheduler or cron to run once daily
at end of business (e.g. 5:15 PM):

  # Linux/Mac cron (5:15 PM daily)
  15 17 * * * cd /path/to/project && venv/bin/python run_daily_report.py

  # Windows Task Scheduler:
  Program : C:\\path\\to\\venv\\Scripts\\python.exe
  Arguments: C:\\path\\to\\project\\run_daily_report.py
  Trigger  : Daily at 17:15
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.auth import APIAuth  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.services.report_service import send_daily_report  # noqa: E402

configure_logging()
logger = get_logger(__name__)

PAGE_SIZE = 500
TERMINAL_STATUSES = ["sent", "failed", "bounced"]





def _fetch_local_campaign_records() -> list[dict]:
    """
    Query local DuckDB for today's campaign attempts.

    Transforms local campaign_email_attempts to match remote API schema
    for compatibility with _aggregate() function.
    """
    from app.localdb.duckdb_client import DuckDBClient

    today = date.today()

    try:
        with DuckDBClient() as db_client:
            records = db_client.fetch_all("""
                SELECT
                    c.candidate_id,
                    c.candidate_name,
                    a.status,
                    a.bounce_type,
                    a.sent_at as last_attempt_at
                FROM campaign_email_attempts a
                JOIN campaigns c ON a.campaign_id = c.id
                WHERE (DATE(a.sent_at) = ? OR DATE(a.created_at) = ?)
                  AND a.status IN ('sent', 'failed', 'bounced')
            """, {"1": today, "2": today})

            # Transform to match remote schema
            transformed = []
            for row in records:
                transformed.append({
                    "candidate_id": row.get("candidate_id"),
                    "candidate_name": row.get("candidate_name"),
                    "status": row.get("status"),
                    "bounce_type": row.get("bounce_type", "none"),
                    "last_attempt_at": (
                        row.get("last_attempt_at").isoformat()
                        if row.get("last_attempt_at") else ""
                    ),
                })

            logger.info("daily_report_local_fetched", total=len(transformed))
            return transformed

    except Exception as e:
        logger.error("daily_report_local_fetch_failed", error=str(e))
        return []


def _fetch_todays_records() -> list[dict]:
    """
    Fetch all terminal campaign_emails updated today from both
    remote API and local DuckDB (if enabled).
    """
    today = date.today().isoformat()
    all_records: list[dict] = []

    # Fetch from remote API
    for status in TERMINAL_STATUSES:
        offset = 0
        while True:
            try:
                resp = httpx.get(
                    f"{settings.api_url}/campaign-emails/",
                    params={
                        "status": status,
                        "limit": PAGE_SIZE,
                        "offset": offset,
                    },
                    auth=APIAuth(),
                    timeout=60.0,
                    follow_redirects=True,
                )
                resp.raise_for_status()
                data = resp.json()
                page = data if isinstance(data, list) else data.get("records", [])
            except Exception as e:
                logger.warning(
                    "daily_report_fetch_failed",
                    status=status,
                    offset=offset,
                    error=str(e),
                )
                break

            # Filter to records updated today
            for row in page:
                ts = row.get("last_attempt_at") or row.get("updated_at") or ""
                if ts.startswith(today):
                    all_records.append(row)

            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

    # Fetch from local DuckDB if enabled
    if settings.use_local_duckdb_campaigns:
        local_records = _fetch_local_campaign_records()
        all_records.extend(local_records)
        logger.info(
            "daily_report_combined",
            remote=len(all_records) - len(local_records),
            local=len(local_records),
            total=len(all_records)
        )
    else:
        logger.info("daily_report_fetched", total=len(all_records), date=today)

    return all_records


def _aggregate(records: list[dict]) -> list[dict]:
    """
    Group records by candidate_id and aggregate counts.
    Returns a list of candidate dicts ready for send_daily_report().
    """
    # candidate_id -> aggregated dict
    buckets: dict[int, dict] = {}

    for row in records:
        cid = row.get("candidate_id") or 0
        if cid not in buckets:
            buckets[cid] = {
                "id": cid,
                "name": row.get("candidate_name") or f"Candidate {cid}",
                "sent": 0,
                "failed": 0,
                "bounced": 0,
                "hard": 0,
                "soft": 0,
                "invalid": 0,
            }

        status = (row.get("status") or "").lower()
        bounce_type = (row.get("bounce_type") or "none").lower()

        if status == "sent":
            buckets[cid]["sent"] += 1
        elif status == "failed":
            buckets[cid]["failed"] += 1
        elif status == "bounced":
            buckets[cid]["bounced"] += 1
            if bounce_type == "hard":
                buckets[cid]["hard"] += 1
            elif bounce_type == "soft":
                buckets[cid]["soft"] += 1
            elif bounce_type == "invalid":
                buckets[cid]["invalid"] += 1

    return list(buckets.values())


def main() -> None:
    today = date.today().strftime("%A, %B %d %Y")
    logger.info("daily_report_starting", date=today)

    records = _fetch_todays_records()

    if not records:
        logger.info("daily_report_no_activity", date=today)
        print(f"No outreach activity found for {today}. No report sent.")
        return

    candidates = _aggregate(records)

    total = sum(c["sent"] + c["failed"] + c["bounced"] for c in candidates)
    total_sent = sum(c["sent"] for c in candidates)

    print(f"\nDaily Report — {today}")
    print(f"  Records fetched : {len(records)}")
    print(f"  Candidates      : {len(candidates)}")
    print(f"  Total sent      : {total_sent}/{total}")
    print(f"  Sending email to: {settings.report_recipient_email}\n")

    send_daily_report(candidates=candidates)

    logger.info(
        "daily_report_complete",
        candidates=len(candidates),
        total=total,
        sent=total_sent,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("daily_report_interrupted")
    except Exception as e:
        logger.error("daily_report_fatal", error=str(e), exc_info=True)
        sys.exit(1)
