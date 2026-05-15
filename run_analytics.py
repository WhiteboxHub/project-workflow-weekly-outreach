#!/usr/bin/env python3
"""
Analytics Export Runner

Exports campaign_email data from the Orchestrator API to Parquet files
and runs DuckDB analytics queries.

Usage:
    # Export all candidates + show all reports
    python run_analytics.py

    # Export one candidate only
    python run_analytics.py --candidate-id 570

    # Export only (skip report printing)
    python run_analytics.py --export-only

    # Report only (skip export — use existing Parquet files)
    python run_analytics.py --report-only

    # Export suppression list to CSV
    python run_analytics.py --suppression-csv

    # Custom export directory
    python run_analytics.py --export-dir /data/analytics
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.core.logging import configure_logging, get_logger
from app.analytics.exporter import CampaignEmailExporter
from app.analytics.queries import CampaignAnalytics

configure_logging()
logger = get_logger(__name__)

DEFAULT_EXPORT_DIR = "./data/analytics"


def run_export(
    export_dir: str,
    candidate_id: int | None,
) -> bool:
    """Run the Parquet export. Returns True if data was exported."""
    logger.info("analytics_export_starting", candidate_id=candidate_id)

    exporter = CampaignEmailExporter(export_dir=export_dir)
    path = exporter.export(candidate_id=candidate_id)

    if path:
        logger.info("analytics_export_complete", path=str(path))
        return True
    else:
        logger.warning("analytics_export_no_data")
        return False


def run_reports(export_dir: str, suppression_csv: bool) -> None:
    """Run DuckDB analytics and print results to stdout."""
    analytics = CampaignAnalytics(export_dir=export_dir)

    separator = "─" * 70

    # ── 1. Bounce Summary ──────────────────────────────────────────────────
    print(f"\n{separator}")
    print("  BOUNCE SUMMARY  (per candidate)")
    print(separator)
    try:
        df = analytics.bounce_summary()
        if df.empty:
            print("  No data.")
        else:
            print(df.to_string(index=False))
    except Exception as e:
        print(f"  Error: {e}")

    # ── 2. Campaign Progress ───────────────────────────────────────────────
    print(f"\n{separator}")
    print("  CAMPAIGN PROGRESS")
    print(separator)
    try:
        df = analytics.campaign_progress()
        if df.empty:
            print("  No data.")
        else:
            print(df.to_string(index=False))
    except Exception as e:
        print(f"  Error: {e}")

    # ── 3. SMTP Account Health ─────────────────────────────────────────────
    print(f"\n{separator}")
    print("  SMTP ACCOUNT HEALTH  (delivery rate per account)")
    print(separator)
    try:
        df = analytics.smtp_account_health()
        if df.empty:
            print("  No data.")
        else:
            print(df.to_string(index=False))
    except Exception as e:
        print(f"  Error: {e}")

    # ── 4. Daily Send Volume ───────────────────────────────────────────────
    print(f"\n{separator}")
    print("  DAILY SEND VOLUME  (last 10 days)")
    print(separator)
    try:
        df = analytics.daily_send_volume().head(10)
        if df.empty:
            print("  No data.")
        else:
            print(df.to_string(index=False))
    except Exception as e:
        print(f"  Error: {e}")

    # ── 5. Soft Bounce Retryable ───────────────────────────────────────────
    print(f"\n{separator}")
    print("  SOFT BOUNCES — RETRYABLE")
    print(separator)
    try:
        df = analytics.soft_bounce_retryable()
        if df.empty:
            print("  None — all soft bounces have been retried or resolved.")
        else:
            print(df.to_string(index=False))
    except Exception as e:
        print(f"  Error: {e}")

    # ── 6. Suppression List ────────────────────────────────────────────────
    print(f"\n{separator}")
    print("  SUPPRESSION LIST  (hard bounce + invalid — never contact again)")
    print(separator)
    try:
        df = analytics.suppression_list()
        if df.empty:
            print("  None.")
        else:
            print(df.head(20).to_string(index=False))
            if len(df) > 20:
                print(f"  ... {len(df) - 20} more rows. Use --suppression-csv to export all.")
    except Exception as e:
        print(f"  Error: {e}")

    # ── 7. Suppression CSV export (optional) ──────────────────────────────
    if suppression_csv:
        print(f"\n{separator}")
        try:
            csv_path = analytics.export_suppression_csv()
            print(f"  Suppression list exported → {csv_path}")
        except Exception as e:
            print(f"  CSV export failed: {e}")

    print(f"\n{separator}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export campaign email data to Parquet and run DuckDB analytics."
    )
    parser.add_argument(
        "--candidate-id",
        type=int,
        default=None,
        help="Export only this candidate's emails (default: all candidates)",
    )
    parser.add_argument(
        "--export-dir",
        type=str,
        default=DEFAULT_EXPORT_DIR,
        help=f"Directory for Parquet files (default: {DEFAULT_EXPORT_DIR})",
    )
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Run export only — skip analytics report",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Run analytics report only — skip export (uses existing Parquet files)",
    )
    parser.add_argument(
        "--suppression-csv",
        action="store_true",
        default=True,
        help="Export suppression list to CSV (default: enabled)",
    )

    args = parser.parse_args()

    try:
        if not args.report_only:
            run_export(
                export_dir=args.export_dir,
                candidate_id=args.candidate_id,
            )

        if not args.export_only:
            run_reports(
                export_dir=args.export_dir,
                suppression_csv=args.suppression_csv,
            )

    except KeyboardInterrupt:
        logger.info("analytics_stopped_by_user")
    except Exception as e:
        logger.error("analytics_fatal_error", error=str(e), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
