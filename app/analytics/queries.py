"""
DuckDB Analytics

Runs analytical queries on exported Parquet files.

All queries use DuckDB's read_parquet() which scans *.parquet files
in the export directory — no data import step needed.

Usage:
    from app.analytics.queries import CampaignAnalytics

    analytics = CampaignAnalytics("./data/analytics")
    print(analytics.bounce_summary())
    print(analytics.suppression_list())
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd


class CampaignAnalytics:
    """
    Analytical queries over exported campaign_emails Parquet files.

    All methods return a pandas DataFrame for easy printing / further
    processing (CSV export, email report, etc.).
    """

    def __init__(self, export_dir: str = "./data/analytics"):
        self.export_dir = Path(export_dir)
        # Glob pattern — DuckDB reads ALL parquet files in the directory
        self._parquet = str(self.export_dir / "*.parquet")

    def _q(self, sql: str) -> pd.DataFrame:
        """Execute a DuckDB SQL query and return a DataFrame."""
        return duckdb.sql(sql).df()

    # ── Core Reports ────────────────────────────────────────────────────────

    def bounce_summary(self) -> pd.DataFrame:
        """
        Per-candidate delivery and bounce summary.

        Columns: candidate_id, delivered, soft_bounces, hard_bounces,
                 invalid_emails, total, delivery_rate_pct
        """
        return self._q(f"""
            SELECT
                candidate_id,
                COUNT(*) FILTER (WHERE status = 'sent')           AS delivered,
                COUNT(*) FILTER (WHERE bounce_type = 'soft')      AS soft_bounces,
                COUNT(*) FILTER (WHERE bounce_type = 'hard')      AS hard_bounces,
                COUNT(*) FILTER (WHERE bounce_type = 'invalid')   AS invalid_emails,
                COUNT(*)                                           AS total,
                ROUND(
                    100.0 * COUNT(*) FILTER (WHERE status = 'sent') / COUNT(*),
                    2
                )                                                  AS delivery_rate_pct
            FROM read_parquet('{self._parquet}')
            GROUP BY candidate_id
            ORDER BY candidate_id
        """)

    def suppression_list(self) -> pd.DataFrame:
        """
        Emails that must never be contacted again.
        Includes hard bounces and invalid emails.

        Use this to build your global suppression / blacklist.
        """
        return self._q(f"""
            SELECT
                vendor_email,
                bounce_type,
                candidate_id,
                MAX(last_attempt_at) AS last_attempt_at
            FROM read_parquet('{self._parquet}')
            WHERE bounce_type IN ('hard', 'invalid')
            GROUP BY vendor_email, bounce_type, candidate_id
            ORDER BY last_attempt_at DESC
        """)

    def soft_bounce_retryable(self) -> pd.DataFrame:
        """
        Soft-bounced emails that can be retried.
        Filters to rows where retry_count < 3 (still within retry budget).
        """
        return self._q(f"""
            SELECT
                vendor_email,
                candidate_id,
                retry_count,
                last_attempt_at,
                error_message
            FROM read_parquet('{self._parquet}')
            WHERE bounce_type = 'soft'
              AND (retry_count IS NULL OR retry_count < 3)
            ORDER BY last_attempt_at ASC
        """)

    def daily_send_volume(self) -> pd.DataFrame:
        """
        Daily email volume per SMTP credential account.
        Useful for monitoring warmup progress and daily limit compliance.
        """
        return self._q(f"""
            SELECT
                CAST(last_attempt_at AS DATE)                      AS send_date,
                credential_id,
                COUNT(*) FILTER (WHERE status = 'sent')            AS sent,
                COUNT(*) FILTER (WHERE status = 'failed')          AS failed,
                COUNT(*) FILTER (WHERE bounce_type = 'soft')       AS soft_bounces,
                COUNT(*) FILTER (WHERE bounce_type = 'hard')       AS hard_bounces,
                COUNT(*)                                            AS total
            FROM read_parquet('{self._parquet}')
            WHERE last_attempt_at IS NOT NULL
            GROUP BY 1, 2
            ORDER BY 1 DESC, 2
        """)

    def campaign_progress(self) -> pd.DataFrame:
        """
        Overall campaign progress per candidate.
        Shows how many emails remain pending vs completed.
        """
        return self._q(f"""
            SELECT
                candidate_id,
                COUNT(*) FILTER (WHERE status = 'sent')    AS sent,
                COUNT(*) FILTER (WHERE status = 'failed')  AS failed,
                COUNT(*) FILTER (WHERE status = 'bounced') AS bounced,
                COUNT(*) FILTER (WHERE status = 'pending') AS still_pending,
                COUNT(*)                                    AS total_snapshot
            FROM read_parquet('{self._parquet}')
            GROUP BY candidate_id
            ORDER BY candidate_id
        """)

    def smtp_account_health(self) -> pd.DataFrame:
        """
        Delivery rate per SMTP credential account.
        Helps identify which accounts are performing poorly.
        """
        return self._q(f"""
            SELECT
                credential_id,
                COUNT(*) FILTER (WHERE status = 'sent')          AS sent,
                COUNT(*) FILTER (WHERE status = 'failed')        AS failed,
                COUNT(*) FILTER (WHERE bounce_type = 'hard')     AS hard_bounces,
                COUNT(*)                                          AS total,
                ROUND(
                    100.0 * COUNT(*) FILTER (WHERE status = 'sent') / COUNT(*),
                    2
                )                                                  AS delivery_rate_pct
            FROM read_parquet('{self._parquet}')
            WHERE credential_id IS NOT NULL
            GROUP BY credential_id
            ORDER BY delivery_rate_pct ASC
        """)

    def export_suppression_csv(self, output_path: str = "./data/suppression_list.csv") -> str:
        """
        Export the suppression list to a CSV file.
        Can be imported into any email platform or used to update
        the email_suppression_list table in MySQL.
        """
        df = self.suppression_list()
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False)
        return output_path
