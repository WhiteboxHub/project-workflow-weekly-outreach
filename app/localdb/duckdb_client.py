"""
DuckDB Client for Local Campaign Execution

Provides centralized connection management, schema initialization,
and safe query helpers for the local campaign execution engine.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import contextmanager
from datetime import datetime

import duckdb
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class DuckDBClient:
    """
    DuckDB client for local campaign execution.

    Manages connection, schema, and provides safe query helpers.
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize DuckDB connection.

        Args:
            db_path: Path to DuckDB file. Defaults to env var or ./data/campaigns.duckdb
        """
        self.db_path = db_path or settings.duckdb_campaign_path
        self._ensure_directory()
        self.conn = duckdb.connect(self.db_path)
        self._initialize_schema()
        logger.info("duckdb_client_initialized", db_path=self.db_path)

    def _ensure_directory(self) -> None:
        """Create directory for DuckDB file if it doesn't exist."""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

    def _initialize_schema(self) -> None:
        """Create all required tables and indexes if they don't exist."""

        # Campaigns table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY,
                remote_schedule_id INTEGER NOT NULL,
                candidate_id INTEGER NOT NULL,
                candidate_name VARCHAR,
                workflow_id INTEGER NOT NULL,
                status VARCHAR DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                total_recipients INTEGER DEFAULT 0,
                active_recipients INTEGER DEFAULT 0,
                UNIQUE(remote_schedule_id, candidate_id)
            )
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_campaigns_status
            ON campaigns(status)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_campaigns_candidate
            ON campaigns(candidate_id)
        """)

        # Campaign steps table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS campaign_steps (
                id INTEGER PRIMARY KEY,
                campaign_id INTEGER NOT NULL,
                step_number INTEGER NOT NULL,
                step_name VARCHAR,
                delay_days INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(campaign_id, step_number),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
            )
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_steps_campaign
            ON campaign_steps(campaign_id)
        """)

        # Campaign recipients table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS campaign_recipients (
                id INTEGER PRIMARY KEY,
                campaign_id INTEGER NOT NULL,
                vendor_email VARCHAR NOT NULL,
                outreach_email_id INTEGER,
                status VARCHAR DEFAULT 'active',
                current_step_number INTEGER DEFAULT 1,
                next_send_at TIMESTAMP,
                enrolled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_attempt_at TIMESTAMP,
                bounce_type VARCHAR,
                UNIQUE(campaign_id, vendor_email),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
            )
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_recipients_campaign
            ON campaign_recipients(campaign_id)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_recipients_status
            ON campaign_recipients(campaign_id, status)
        """)

        # Campaign email attempts table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS campaign_email_attempts (
                id INTEGER PRIMARY KEY,
                campaign_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                step_number INTEGER NOT NULL,
                vendor_email VARCHAR NOT NULL,
                status VARCHAR DEFAULT 'pending',
                claimed_at TIMESTAMP,
                claimed_by VARCHAR,
                sent_at TIMESTAMP,
                error_message VARCHAR,
                bounce_type VARCHAR,
                credential_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
                FOREIGN KEY (recipient_id) REFERENCES campaign_recipients(id)
            )
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_attempts_recipient
            ON campaign_email_attempts(recipient_id)
        """)

        # Campaign daily metrics table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS campaign_daily_metrics (
                id INTEGER PRIMARY KEY,
                campaign_id INTEGER NOT NULL,
                metric_date DATE NOT NULL,
                emails_sent INTEGER DEFAULT 0,
                emails_failed INTEGER DEFAULT 0,
                emails_bounced INTEGER DEFAULT 0,
                hard_bounces INTEGER DEFAULT 0,
                soft_bounces INTEGER DEFAULT 0,
                invalid_emails INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(campaign_id, metric_date),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
            )
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_metrics_date
            ON campaign_daily_metrics(metric_date)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_metrics_campaign
            ON campaign_daily_metrics(campaign_id)
        """)

        logger.debug("duckdb_schema_initialized")

    def execute_query(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """
        Execute parameterized query with automatic error handling.

        Args:
            sql: SQL query string
            params: Dictionary of parameters (optional)

        Returns:
            Query result
        """
        try:
            if params:
                return self.conn.execute(sql, params)
            return self.conn.execute(sql)
        except Exception as e:
            logger.error("duckdb_query_failed", sql=sql[:100], error=str(e))
            raise

    def fetch_one(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
        """
        Fetch single row as dict.

        Args:
            sql: SQL query string
            params: Dictionary of parameters (optional)

        Returns:
            Single row as dict or None
        """
        result = self.execute_query(sql, params).fetchone()
        if result is None:
            return None

        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def fetch_all(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict]:
        """
        Fetch all rows as list of dicts.

        Args:
            sql: SQL query string
            params: Dictionary of parameters (optional)

        Returns:
            List of rows as dicts
        """
        result = self.execute_query(sql, params).fetchall()
        if not result:
            return []

        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in result]

    @contextmanager
    def transaction(self):
        """Context manager for transactions."""
        self.conn.execute("BEGIN TRANSACTION")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def claim_pending_attempts(
        self,
        campaign_id: int,
        limit: int,
        worker_id: str = "scheduler"
    ) -> List[Dict]:
        """
        Claim pending attempts atomically using ROWID-based update.

        Since DuckDB doesn't support FOR UPDATE SKIP LOCKED, we use a
        timestamp-based claiming strategy with ROWID to ensure atomicity.

        Args:
            campaign_id: Campaign ID
            limit: Maximum number of attempts to claim
            worker_id: Worker identifier for debugging

        Returns:
            List of claimed attempt records with full details
        """
        now = datetime.utcnow()

        with self.transaction():
            # Select pending attempts
            pending = self.fetch_all("""
                SELECT rowid, id, recipient_id, step_number, vendor_email
                FROM campaign_email_attempts
                WHERE campaign_id = ?
                  AND status = 'pending'
                  AND created_at <= ?
                ORDER BY created_at ASC
                LIMIT ?
            """, {"1": campaign_id, "2": now, "3": limit})

            if not pending:
                return []

            # Extract ROWIDs
            rowids = [row["rowid"] for row in pending]
            rowid_placeholders = ",".join(["?"] * len(rowids))

            # Update to claimed status
            update_params = {
                "1": now,
                "2": worker_id,
            }
            for i, rowid in enumerate(rowids, start=3):
                update_params[str(i)] = rowid

            self.execute_query(f"""
                UPDATE campaign_email_attempts
                SET status = 'claimed',
                    claimed_at = ?,
                    claimed_by = ?
                WHERE rowid IN ({rowid_placeholders})
                  AND status = 'pending'
            """, update_params)

            # Fetch full details for claimed attempts
            attempt_ids = [row["id"] for row in pending]
            id_placeholders = ",".join(["?"] * len(attempt_ids))

            id_params = {}
            for i, attempt_id in enumerate(attempt_ids, start=1):
                id_params[str(i)] = attempt_id

            claimed = self.fetch_all(f"""
                SELECT
                    a.id as attempt_id,
                    a.campaign_id,
                    a.recipient_id,
                    a.step_number,
                    a.vendor_email,
                    a.credential_id,
                    r.current_step_number,
                    r.status as recipient_status,
                    c.candidate_id,
                    c.candidate_name,
                    c.workflow_id
                FROM campaign_email_attempts a
                JOIN campaign_recipients r ON a.recipient_id = r.id
                JOIN campaigns c ON a.campaign_id = c.id
                WHERE a.id IN ({id_placeholders})
            """, id_params)

            logger.info(
                "attempts_claimed",
                campaign_id=campaign_id,
                count=len(claimed),
                worker_id=worker_id
            )

            return claimed

    def reset_stale_claims(self, minutes: int = 10) -> int:
        """
        Reset attempts that have been claimed but not completed.

        Handles worker crashes by resetting stale claimed attempts
        back to pending status.

        Args:
            minutes: Minutes after which a claimed attempt is considered stale

        Returns:
            Number of attempts reset
        """
        threshold = datetime.utcnow()

        result = self.execute_query("""
            UPDATE campaign_email_attempts
            SET status = 'pending',
                claimed_at = NULL,
                claimed_by = NULL
            WHERE status = 'claimed'
              AND claimed_at < (CURRENT_TIMESTAMP - INTERVAL ? MINUTE)
              AND sent_at IS NULL
        """, {"1": minutes})

        count = result.fetchone()[0] if result else 0

        if count > 0:
            logger.warning("stale_claims_reset", count=count, minutes=minutes)

        return count

    def close(self) -> None:
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            logger.debug("duckdb_connection_closed")

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
