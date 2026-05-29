"""
Campaign Email Exporter

Fetches completed campaign_email records (sent / failed / bounced)
from the Orchestrator REST API and writes them as dated Parquet files.

Flow:
    Orchestrator API  →  pandas DataFrame  →  Parquet file  →  DuckDB
"""

from __future__ import annotations

import httpx
import pandas as pd
from datetime import date
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.core.auth import APIAuth

logger = get_logger(__name__)

# Terminal statuses — only export rows that are done (no longer pending/processing)
TERMINAL_STATUSES = ["sent", "failed", "bounced"]


class CampaignEmailExporter:
    """
    Pulls campaign_emails data from the Orchestrator API and saves
    it as Parquet files ready for DuckDB analysis.
    """

    def __init__(
        self,
        export_dir: str = "./data/analytics",
        api_url: Optional[str] = None,
        token: Optional[str] = None,
    ):
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)

        self.api_url = (api_url or settings.api_url).rstrip("/")

    # ── Internal helpers ────────────────────────────────────────────────────


    def _fetch_page(
        self,
        candidate_id: Optional[int],
        status: str,
        offset: int,
        limit: int,
    ) -> list[dict]:
        """Fetch one page of campaign_emails for a given status."""
        params: dict = {
            "status": status,
            "limit": limit,
            "offset": offset,
        }
        if candidate_id is not None:
            params["candidate_id"] = candidate_id

        try:
            resp = httpx.get(
                f"{self.api_url}/campaign-emails",
                params=params,
                auth=APIAuth(),
                timeout=60.0,
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
            # Handle both list response and {records: [...]} shape
            return data if isinstance(data, list) else data.get("records", [])
        except Exception as e:
            logger.warning(
                "exporter_fetch_page_failed",
                status=status,
                offset=offset,
                error=str(e),
            )
            return []

    def _fetch_local_all(self, candidate_id: Optional[int] = None) -> list[dict]:
        """Fetch all terminal-status attempts from local DuckDB."""
        if not settings.use_local_duckdb_campaigns:
            return []
            
        from app.localdb.duckdb_client import DuckDBClient
        
        try:
            with DuckDBClient() as db_client:
                sql = """
                    SELECT
                        c.candidate_id,
                        a.vendor_email,
                        a.status,
                        a.bounce_type,
                        a.error_message,
                        a.sent_at,
                        a.created_at,
                        a.credential_id
                    FROM campaign_email_attempts a
                    JOIN campaigns c ON a.campaign_id = c.id
                    WHERE a.status IN ('sent', 'failed', 'bounced')
                """
                params = {}
                if candidate_id is not None:
                    sql += " AND c.candidate_id = ?"
                    params = {"1": candidate_id}
                
                records = db_client.fetch_all(sql, params)
                
                transformed = []
                for row in records:
                    last_ts = row.get("sent_at") or row.get("created_at")
                    transformed.append({
                        "candidate_id": row.get("candidate_id"),
                        "vendor_email": row.get("vendor_email"),
                        "status": row.get("status"),
                        "bounce_type": row.get("bounce_type", "none"),
                        "error_message": row.get("error_message"),
                        "last_attempt_at": last_ts.isoformat() if last_ts else None,
                        "credential_id": row.get("credential_id"),
                        "retry_count": 0,
                    })
                return transformed
        except Exception as e:
            logger.warning("exporter_local_fetch_failed", error=str(e))
            return []

    def _fetch_all(
        self,
        candidate_id: Optional[int] = None,
        page_size: int = 500,
    ) -> list[dict]:
        """
        Paginate through all terminal-status campaign_emails.
        Returns a flat list of all records across all statuses.
        """
        all_records: list[dict] = []
        
        # 1. Fetch remote records
        for status in TERMINAL_STATUSES:
            offset = 0
            while True:
                page = self._fetch_page(
                    candidate_id=candidate_id,
                    status=status,
                    offset=offset,
                    limit=page_size,
                )
                if not page:
                    break

                all_records.extend(page)
                logger.debug(
                    "exporter_page_fetched",
                    status=status,
                    offset=offset,
                    count=len(page),
                )

                if len(page) < page_size:
                    break  # Last page
                offset += page_size

        # 2. Fetch local DuckDB records if enabled
        if settings.use_local_duckdb_campaigns:
            local_records = self._fetch_local_all(candidate_id=candidate_id)
            if local_records:
                all_records.extend(local_records)
                logger.info(
                    "exporter_combined_local_records",
                    remote=len(all_records) - len(local_records),
                    local=len(local_records),
                    total=len(all_records),
                )

        return all_records

    # ── Public API ──────────────────────────────────────────────────────────

    def export(
        self,
        candidate_id: Optional[int] = None,
        page_size: int = 500,
    ) -> Optional[Path]:
        """
        Export all terminal campaign_email records to a dated Parquet file.

        Args:
            candidate_id: Filter to one candidate. None = all candidates.
            page_size:    Records per API request.

        Returns:
            Path to the written Parquet file, or None if no data.
        """
        logger.info("exporter_starting", candidate_id=candidate_id)
        records = self._fetch_all(candidate_id=candidate_id, page_size=page_size)

        if not records:
            logger.info("exporter_no_records", candidate_id=candidate_id)
            return None

        df = pd.DataFrame(records)

        # ── Normalise columns ────────────────────────────────────────────────
        # Ensure bounce_type exists (may be missing from older records)
        if "bounce_type" not in df.columns:
            df["bounce_type"] = "none"

        # Parse datetime strings to proper dtype for DuckDB TIMESTAMP support
        for col in ["created_at", "updated_at", "last_attempt_at"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)

        # Cast numeric columns that may arrive as strings
        for col in ["candidate_id", "workflow_id", "scheduler_id",
                    "credential_id", "retry_count"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # ── Write Parquet ────────────────────────────────────────────────────
        suffix = f"_cand{candidate_id}" if candidate_id else ""
        filename = f"campaign_emails{suffix}_{date.today().strftime('%Y_%m_%d')}.parquet"
        path = self.export_dir / filename

        df.to_parquet(path, index=False, engine="pyarrow")

        logger.info(
            "exporter_done",
            rows=len(df),
            path=str(path),
        )
        return path
