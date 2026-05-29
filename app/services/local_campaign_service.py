"""
Local Campaign Service

Core orchestration layer for local DuckDB campaign lifecycle management.
Handles campaign creation, recipient enrollment, sequence progression,
and metrics tracking.
"""

from datetime import datetime, date, timezone
from typing import Any, Dict, List, Optional

import httpx
from app.localdb.duckdb_client import DuckDBClient
from app.utils.business_days import (
    calculate_next_send_at,
    calculate_immediate_send_at,
    add_business_days,
    next_send_window_datetime,
    is_business_day,
)
from app.core.logging import get_logger
from app.core.auth import APIAuth
from app.core.config import settings

logger = get_logger(__name__)


class LocalCampaignService:
    """
    Service for managing local DuckDB campaigns.

    Provides methods for campaign lifecycle: creation, enrollment,
    attempt generation, progression, and metrics tracking.
    """

    def __init__(self, db_client: DuckDBClient, api_client: httpx.Client):
        """
        Initialize service with database and API clients.

        Args:
            db_client: DuckDB client instance
            api_client: httpx client for remote API calls
        """
        self.db = db_client
        self.api = api_client

    def create_or_resume_campaign(
        self,
        schedule_id: int,
        candidate_id: int,
        candidate_name: str,
        workflow_id: int,
        run_parameters: Dict[str, Any]
    ) -> int:
        """
        Create new campaign or resume existing one (idempotent).

        Checks run_parameters for existing local_campaign_id. If found and
        campaign is active, resumes. Otherwise creates new campaign.

        Args:
            schedule_id: Remote schedule ID
            candidate_id: Candidate ID
            candidate_name: Candidate name
            workflow_id: Workflow ID
            run_parameters: Schedule run parameters

        Returns:
            Local campaign ID
        """
        # Resume active campaign by candidate_id (prevents duplicate active campaigns if schedule_id changes)
        existing = self.db.fetch_one("""
            SELECT id, status, remote_schedule_id
            FROM campaigns
            WHERE candidate_id = ? AND status = 'active'
        """, {"1": candidate_id})

        if existing:
            campaign_id = int(existing["id"])
            
            # If the user deleted the old schedule and created a new one for the same candidate,
            # we need to update the remote_schedule_id so completion metrics sync back correctly.
            if existing["remote_schedule_id"] != schedule_id:
                try:
                    self.db.execute_query("""
                        UPDATE campaigns
                        SET remote_schedule_id = ?
                        WHERE id = ?
                    """, {"1": schedule_id, "2": campaign_id})
                    logger.info(
                        "local_campaign_schedule_id_updated",
                        campaign_id=campaign_id,
                        old_schedule_id=existing["remote_schedule_id"],
                        new_schedule_id=schedule_id
                    )
                except Exception as e:
                    logger.warning(
                        "duckdb_update_blocked_by_fk",
                        campaign_id=campaign_id,
                        error=str(e),
                        hint="DuckDB blocks UPDATE on rows with foreign keys. Ignoring schedule ID update."
                    )

            logger.info(
                "local_campaign_resumed",
                campaign_id=campaign_id,
                status=existing["status"],
                candidate_id=candidate_id,
                schedule_id=schedule_id,
            )
            return campaign_id



        # Create new campaign
        result = self.db.execute_query("""
            INSERT INTO campaigns (
                id, remote_schedule_id, candidate_id, candidate_name,
                workflow_id, status, created_at, total_recipients, active_recipients
            ) VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM campaigns), ?, ?, ?, ?, 'active', ?, 0, 0)
            RETURNING id
        """, {
            "1": schedule_id,
            "2": candidate_id,
            "3": candidate_name,
            "4": workflow_id,
            "5": datetime.now(timezone.utc)
        })

        campaign_id = result.fetchone()[0]

        logger.info(
            "local_campaign_created",
            campaign_id=campaign_id,
            candidate_id=candidate_id,
            candidate_name=candidate_name,
            schedule_id=schedule_id
        )

        return campaign_id

    def create_default_steps(self, campaign_id: int) -> None:
        """
        Create 4-step weekly sequence (idempotent).

        Steps:
        - Step 1: Immediate (delay_days=0)
        - Step 2: +3 business days
        - Step 3: +5 business days
        - Step 4: +7 business days

        Args:
            campaign_id: Campaign ID
        """
        steps = [
            {"step_number": 1, "step_name": "Initial Outreach", "delay_days": 0},
            {"step_number": 2, "step_name": "Follow-up 1", "delay_days": 3},
            {"step_number": 3, "step_name": "Follow-up 2", "delay_days": 5},
            {"step_number": 4, "step_name": "Final Follow-up", "delay_days": 7},
        ]

        for step in steps:
            # Check if step already exists
            existing = self.db.fetch_one("""
                SELECT id FROM campaign_steps
                WHERE campaign_id = ? AND step_number = ?
            """, {"1": campaign_id, "2": step["step_number"]})

            if not existing:
                self.db.execute_query("""
                    INSERT INTO campaign_steps (
                        id, campaign_id, step_number, step_name, delay_days, created_at
                    ) VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM campaign_steps), ?, ?, ?, ?, ?)
                """, {
                    "1": campaign_id,
                    "2": step["step_number"],
                    "3": step["step_name"],
                    "4": step["delay_days"],
                    "5": datetime.now(timezone.utc)
                })

        logger.info("local_campaign_steps_created", campaign_id=campaign_id, count=len(steps))

    def enroll_recipients(
        self,
        campaign_id: int,
        candidate_id: int
    ) -> int:
        """
        Enroll recipients from remote outreach_emails (idempotent).

        Fetches eligible contacts from remote API and copies them into
        local campaign_recipients table. Sets initial next_send_at with
        business day logic and jitter.

        Args:
            campaign_id: Campaign ID
            candidate_id: Candidate ID

        Returns:
            Number of NEW recipients enrolled (excludes existing)
        """
        # Fetch eligible contacts from remote API
        try:
            resp = self.api.get(f"/orchestrator/candidates/{candidate_id}/outreach-emails")
            resp.raise_for_status()
            remote_emails = resp.json()
        except Exception as e:
            logger.error(
                "failed_to_fetch_outreach_emails",
                candidate_id=candidate_id,
                error=str(e)
            )
            return 0

        if not remote_emails:
            logger.warning("no_outreach_emails_found", candidate_id=candidate_id)
            return 0

        # ------------------------------------------------------------------ #
        # O1 fix: Step 1 must be IMMEDIATE — no business-day or send-window   #
        # enforcement.  Using calculate_next_send_at(delay_days=0) would      #
        # silently push Saturday/Sunday enrollments to Monday 9 AM.           #
        # calculate_immediate_send_at() applies only a small jitter           #
        # (30–120 s) to avoid SMTP thundering-herd on bulk enrollments.       #
        # ------------------------------------------------------------------ #
        now = datetime.now(timezone.utc)
        next_send = calculate_immediate_send_at(now)
        _is_weekend = not is_business_day(now)

        new_count = 0

        for email_data in remote_emails:
            vendor_email = email_data.get("vendor_email")
            outreach_email_id = email_data.get("id")

            if not vendor_email:
                continue

            # Check if already enrolled (idempotent)
            existing = self.db.fetch_one("""
                SELECT id FROM campaign_recipients
                WHERE campaign_id = ? AND vendor_email = ?
            """, {"1": campaign_id, "2": vendor_email})

            if existing:
                continue

            # Insert new recipient
            self.db.execute_query("""
                INSERT INTO campaign_recipients (
                    id, campaign_id, vendor_email, outreach_email_id,
                    status, current_step_number, next_send_at, enrolled_at
                ) VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM campaign_recipients), ?, ?, ?, 'active', 1, ?, ?)
            """, {
                "1": campaign_id,
                "2": vendor_email,
                "3": outreach_email_id,
                "4": next_send,
                "5": datetime.now(timezone.utc)
            })

            new_count += 1

        # Update campaign recipient counts
        self.db.execute_query("""
            UPDATE campaigns
            SET total_recipients = (
                    SELECT COUNT(*) FROM campaign_recipients
                    WHERE campaign_id = ?
                ),
                active_recipients = (
                    SELECT COUNT(*) FROM campaign_recipients
                    WHERE campaign_id = ? AND status = 'active'
                )
            WHERE id = ?
        """, {"1": campaign_id, "2": campaign_id, "3": campaign_id})

        logger.info(
            "local_campaign_recipients_enrolled",
            campaign_id=campaign_id,
            new=new_count,
            total=len(remote_emails),
            # O1 observability: confirm immediate path was used
            step=1,
            scheduled_immediately=True,
            is_weekend=_is_weekend,
            day_of_week=now.strftime("%A"),
            next_send_at=next_send.isoformat(),
        )

        return new_count

    def generate_due_attempts(self, campaign_id: int) -> int:
        """
        Generate pending email attempts for due recipients.

        Creates attempts for recipients with next_send_at <= NOW() and
        status='active'. Idempotent: checks for existing attempts before creating.

        Args:
            campaign_id: Campaign ID

        Returns:
            Number of new attempts created
        """
        now = datetime.now(timezone.utc)

        # Find recipients due for next step
        due_recipients = self.db.fetch_all("""
            SELECT
                r.id as recipient_id,
                r.vendor_email,
                r.current_step_number,
                r.next_send_at
            FROM campaign_recipients r
            WHERE r.campaign_id = ?
              AND r.status = 'active'
              AND r.next_send_at <= ?
        """, {"1": campaign_id, "2": now})

        if not due_recipients:
            logger.debug("no_due_recipients", campaign_id=campaign_id)
            return 0

        new_count = 0

        for recipient in due_recipients:
            recipient_id = recipient["recipient_id"]
            step_number = recipient["current_step_number"]
            vendor_email = recipient["vendor_email"]

            # Check if attempt already exists for this recipient/step
            existing = self.db.fetch_one("""
                SELECT id FROM campaign_email_attempts
                WHERE recipient_id = ? AND step_number = ?
            """, {"1": recipient_id, "2": step_number})

            if existing:
                continue

            # Create new attempt
            self.db.execute_query("""
                INSERT INTO campaign_email_attempts (
                    id, campaign_id, recipient_id, step_number, vendor_email,
                    status, created_at
                ) VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM campaign_email_attempts), ?, ?, ?, ?, 'pending', ?)
            """, {
                "1": campaign_id,
                "2": recipient_id,
                "3": step_number,
                "4": vendor_email,
                "5": datetime.now(timezone.utc)
            })

            new_count += 1

        logger.info(
            "local_campaign_attempts_generated",
            campaign_id=campaign_id,
            count=new_count
        )

        return new_count

    def claim_attempts(
        self,
        campaign_id: int,
        limit: int,
        worker_id: str = "scheduler"
    ) -> List[Dict]:
        """
        Claim pending attempts atomically.

        Uses DuckDB client's ROWID-based claiming strategy.

        Args:
            campaign_id: Campaign ID
            limit: Maximum attempts to claim
            worker_id: Worker identifier

        Returns:
            List of claimed attempt records with full details
        """
        reset_count = self.db.reset_stale_claims(
            minutes=settings.stale_claim_minutes
        )
        if reset_count:
            logger.info(
                "stale_claims_reset_before_claim",
                campaign_id=campaign_id,
                count=reset_count,
            )
        return self.db.claim_pending_attempts(campaign_id, limit, worker_id)

    def advance_recipient(
        self,
        recipient_id: int,
        campaign_id: int
    ) -> None:
        """
        Advance recipient to next step after successful send.

        If current_step_number >= 4, marks recipient as completed.
        Otherwise increments step and calculates next_send_at.

        Args:
            recipient_id: Recipient ID
            campaign_id: Campaign ID
        """
        # Get current recipient state
        recipient = self.db.fetch_one("""
            SELECT current_step_number, status
            FROM campaign_recipients
            WHERE id = ?
        """, {"1": recipient_id})

        if not recipient or recipient["status"] != "active":
            return

        current_step = recipient["current_step_number"]

        # Check if this was the final step
        if current_step >= 4:
            self.db.execute_query("""
                UPDATE campaign_recipients
                SET status = 'completed',
                    last_attempt_at = ?
                WHERE id = ?
            """, {"1": datetime.now(timezone.utc), "2": recipient_id})

            logger.info(
                "local_campaign_recipient_completed",
                recipient_id=recipient_id,
                campaign_id=campaign_id
            )

            # Check if campaign is fully completed
            self._check_campaign_completion(campaign_id)
            return

        # Advance to next step
        next_step = current_step + 1

        # Get next step delay
        step = self.db.fetch_one("""
            SELECT delay_days FROM campaign_steps
            WHERE campaign_id = ? AND step_number = ?
        """, {"1": campaign_id, "2": next_step})

        if not step:
            logger.error(
                "next_step_not_found",
                campaign_id=campaign_id,
                next_step=next_step
            )
            return

        # Calculate next_send_at
        now = datetime.now(timezone.utc)
        next_send = calculate_next_send_at(now, delay_days=step["delay_days"])

        # Update recipient
        self.db.execute_query("""
            UPDATE campaign_recipients
            SET current_step_number = ?,
                next_send_at = ?,
                last_attempt_at = ?
            WHERE id = ?
        """, {
            "1": next_step,
            "2": next_send,
            "3": datetime.now(timezone.utc),
            "4": recipient_id
        })

        logger.info(
            "local_campaign_recipient_advanced",
            recipient_id=recipient_id,
            campaign_id=campaign_id,
            from_step=current_step,
            to_step=next_step,
            next_send_at=next_send.isoformat()
        )

    def update_recipient_status(
        self,
        recipient_id: int,
        lifecycle_status: str,
        bounce_type: Optional[str] = None,
    ) -> None:
        """
        Update a recipient's lifecycle status in DuckDB.

        Lifecycle states:
          - 'active'       : default, ready to send next step
          - 'sent'         : successfully sent all steps (completed)
          - 'deferred'     : temporarily skipped due to rate-limit; retry tomorrow
          - 'rate_limited' : same as deferred but named explicitly for reporting
          - 'soft_bounce'  : transient failure (mailbox full, server busy)
          - 'hard_bounce'  : permanent failure (address does not exist)
          - 'invalid'      : failed email format validation
          - 'bounced'      : legacy alias kept for backward compat

        Hard/invalid bounces permanently stop future sends (status = 'hard_bounce').
        Soft bounces and deferred keep status = 'active' so they are retried.

        Args:
            recipient_id: Recipient ID
            lifecycle_status: One of the states listed above
            bounce_type: Optional raw bounce label for metrics (hard/soft/invalid)
        """
        # Map lifecycle states to the DB status column
        terminal_states = {"hard_bounce", "invalid", "bounced"}
        if lifecycle_status in terminal_states:
            db_status = "hard_bounce"
        elif lifecycle_status in ("soft_bounce", "deferred", "rate_limited"):
            db_status = "active"   # Keep eligible for retry
        else:
            db_status = lifecycle_status   # 'active', 'sent', etc.

        self.db.execute_query("""
            UPDATE campaign_recipients
            SET status = ?,
                bounce_type = ?,
                last_attempt_at = ?
            WHERE id = ?
        """, {
            "1": db_status,
            "2": bounce_type or lifecycle_status,
            "3": datetime.now(timezone.utc),
            "4": recipient_id
        })

        logger.info(
            "local_campaign_recipient_status_updated",
            recipient_id=recipient_id,
            lifecycle_status=lifecycle_status,
            db_status=db_status,
            bounce_type=bounce_type,
        )

    def mark_recipient_bounced(
        self,
        recipient_id: int,
        bounce_type: str
    ) -> None:
        """Legacy alias for update_recipient_status. Kept for backward compat."""
        self.update_recipient_status(
            recipient_id=recipient_id,
            lifecycle_status=bounce_type,
            bounce_type=bounce_type,
        )

    def mark_recipient_deferred(self, recipient_id: int) -> None:
        """
        Mark a recipient as deferred due to a rate-limit on the sending credential.

        The recipient is NOT bounced — they will be retried tomorrow when the
        sending credential's daily quota resets and is_healthy becomes True again.
        """
        self.update_recipient_status(
            recipient_id=recipient_id,
            lifecycle_status="deferred",
        )
        logger.info(
            "local_campaign_recipient_deferred",
            recipient_id=recipient_id,
            reason="credential_rate_limited",
        )

    def mark_credential_rate_limited(self, credential_id: int) -> None:
        """
        Mark a sending credential as unhealthy due to hitting a provider rate limit.

        Calls PUT /api/email-smtp-credentials/{id} to set is_healthy=False in the
        main PostgreSQL database. The scheduler will automatically exclude unhealthy
        credentials from the rotation on the next run, allowing the other 19 (or
        however many) accounts to continue sending without interruption.

        The credential will need to be manually re-enabled (or auto-healed by a
        nightly job) once the provider's quota window resets (usually 24 hours).
        """
        try:
            resp = self.api.put(
                f"/email-smtp-credentials/{credential_id}",
                json={
                    "is_healthy": False,
                },
            )
            if resp.is_success:
                logger.warning(
                    "credential_marked_rate_limited",
                    credential_id=credential_id,
                    action="is_healthy set to False in PostgreSQL",
                )
            else:
                logger.error(
                    "credential_rate_limit_update_failed",
                    credential_id=credential_id,
                    status_code=resp.status_code,
                    response=resp.text[:200],
                )
        except Exception as e:
            logger.error(
                "credential_rate_limit_update_error",
                credential_id=credential_id,
                error=str(e),
            )

    def increment_credential_sent(self, credential_id: int) -> None:
        """
        Atomically increment current_day_sent for an SMTP credential.

        Uses the dedicated API endpoint which handles date rollover automatically:
          - If last_reset_date == today: increment current_day_sent by 1
          - If last_reset_date < today: reset current_day_sent to 1

        This is critical for the scheduler to know when a credential is approaching
        its daily_limit BEFORE Gmail blocks it with a 5.4.5 rate-limit error.
        """
        if not credential_id:
            return
        try:
            resp = self.api.post(
                f"/email-smtp-credentials/{credential_id}/increment-sent",
            )
            if not resp.is_success:
                logger.warning(
                    "credential_increment_sent_failed",
                    credential_id=credential_id,
                    status_code=resp.status_code,
                )
        except Exception as e:
            logger.error(
                "credential_increment_sent_error",
                credential_id=credential_id,
                error=str(e),
            )



    def update_daily_metrics(
        self,
        campaign_id: int,
        metric_date: date,
        sent: int = 0,
        failed: int = 0,
        bounced: int = 0,
        bounce_type: Optional[str] = None
    ) -> None:
        """
        Update daily metrics (incremental).

        Uses INSERT OR REPLACE to handle concurrent updates.

        Args:
            campaign_id: Campaign ID
            metric_date: Date for metrics
            sent: Number sent (increment)
            failed: Number failed (increment)
            bounced: Number bounced (increment)
            bounce_type: Bounce type for classification (optional)
        """
        # Fetch existing metrics
        existing = self.db.fetch_one("""
            SELECT emails_sent, emails_failed, emails_bounced,
                   hard_bounces, soft_bounces, invalid_emails
            FROM campaign_daily_metrics
            WHERE campaign_id = ? AND metric_date = ?
        """, {"1": campaign_id, "2": metric_date})

        if existing:
            new_sent = existing["emails_sent"] + sent
            new_failed = existing["emails_failed"] + failed
            new_bounced = existing["emails_bounced"] + bounced
            new_hard = existing["hard_bounces"]
            new_soft = existing["soft_bounces"]
            new_invalid = existing["invalid_emails"]
        else:
            new_sent = sent
            new_failed = failed
            new_bounced = bounced
            new_hard = 0
            new_soft = 0
            new_invalid = 0

        # Update bounce type counters
        if bounce_type == "hard":
            new_hard += 1
        elif bounce_type == "soft":
            new_soft += 1
        elif bounce_type == "invalid":
            new_invalid += 1

        # Upsert metrics
        self.db.execute_query("""
            INSERT INTO campaign_daily_metrics (
                id, campaign_id, metric_date, emails_sent, emails_failed,
                emails_bounced, hard_bounces, soft_bounces, invalid_emails,
                updated_at
            ) VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM campaign_daily_metrics), ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (campaign_id, metric_date)
            DO UPDATE SET
                emails_sent = ?,
                emails_failed = ?,
                emails_bounced = ?,
                hard_bounces = ?,
                soft_bounces = ?,
                invalid_emails = ?,
                updated_at = ?
        """, {
            "1": campaign_id, "2": metric_date, "3": new_sent, "4": new_failed,
            "5": new_bounced, "6": new_hard, "7": new_soft, "8": new_invalid,
            "9": datetime.now(timezone.utc),
            "10": new_sent, "11": new_failed, "12": new_bounced,
            "13": new_hard, "14": new_soft, "15": new_invalid,
            "16": datetime.now(timezone.utc)
        })

    def get_campaign_metrics(self, campaign_id: int) -> Dict[str, Any]:
        """
        Get aggregated campaign stats for reporting.

        Args:
            campaign_id: Campaign ID

        Returns:
            Dict with campaign metrics
        """
        campaign = self.db.fetch_one("""
            SELECT total_recipients, active_recipients, status
            FROM campaigns
            WHERE id = ?
        """, {"1": campaign_id})

        if not campaign:
            return {}

        metrics = self.db.fetch_one("""
            SELECT
                SUM(emails_sent) as total_sent,
                SUM(emails_failed) as total_failed,
                SUM(emails_bounced) as total_bounced,
                SUM(hard_bounces) as hard_bounces,
                SUM(soft_bounces) as soft_bounces,
                SUM(invalid_emails) as invalid_emails
            FROM campaign_daily_metrics
            WHERE campaign_id = ?
        """, {"1": campaign_id})

        completed_count = self.db.fetch_one("""
            SELECT COUNT(*) as count
            FROM campaign_recipients
            WHERE campaign_id = ? AND status = 'completed'
        """, {"1": campaign_id})

        bounced_count = self.db.fetch_one("""
            SELECT COUNT(*) as count
            FROM campaign_recipients
            WHERE campaign_id = ? AND status = 'bounced'
        """, {"1": campaign_id})

        return {
            "campaign_id": campaign_id,
            "status": campaign["status"],
            "total_recipients": campaign["total_recipients"] or 0,
            "active_recipients": campaign["active_recipients"] or 0,
            "completed_recipients": completed_count["count"] or 0,
            "bounced_recipients": bounced_count["count"] or 0,
            "total_sent": metrics["total_sent"] or 0,
            "total_failed": metrics["total_failed"] or 0,
            "total_bounced": metrics["total_bounced"] or 0,
            "hard_bounces": metrics["hard_bounces"] or 0,
            "soft_bounces": metrics["soft_bounces"] or 0,
            "invalid_emails": metrics["invalid_emails"] or 0
        }

    def update_remote_run_parameters(
        self,
        schedule_id: int,
        local_campaign_id: int
    ) -> None:
        """
        Write local_campaign_id back to remote schedule.run_parameters.

        Args:
            schedule_id: Remote schedule ID
            local_campaign_id: Local campaign ID
        """
        try:
            # Fetch current run_parameters
            resp = self.api.get(f"/orchestrator/schedules/{schedule_id}")
            resp.raise_for_status()
            schedule = resp.json()

            # Update with local campaign ID
            run_params = schedule.get("run_parameters", {})
            run_params["local_campaign_id"] = local_campaign_id
            run_params["local_db_path"] = settings.duckdb_campaign_path
            run_params["local_campaign_created_at"] = datetime.now(timezone.utc).isoformat()

            # Write back to remote (PATCH preferred; PUT fallback for older APIs)
            payload = {"run_parameters": run_params}
            resp = self.api.patch(
                f"/orchestrator/schedules/{schedule_id}",
                json=payload,
            )
            if resp.status_code == 405:
                resp = self.api.put(
                    f"/orchestrator/schedules/{schedule_id}",
                    json=payload,
                )
            resp.raise_for_status()

            logger.info(
                "local_campaign_id_synced_to_remote",
                schedule_id=schedule_id,
                local_campaign_id=local_campaign_id
            )

        except Exception as e:
            logger.error(
                "failed_to_sync_local_campaign_id",
                schedule_id=schedule_id,
                local_campaign_id=local_campaign_id,
                error=str(e)
            )

    def _check_campaign_completion(self, campaign_id: int) -> None:
        """
        Check if campaign is fully completed and update status.

        Internal helper called after recipient completion.

        Args:
            campaign_id: Campaign ID
        """
        # Update active recipient count
        self.db.execute_query("""
            UPDATE campaigns
            SET active_recipients = (
                SELECT COUNT(*) FROM campaign_recipients
                WHERE campaign_id = ? AND status = 'active'
            )
            WHERE id = ?
        """, {"1": campaign_id, "2": campaign_id})

        # Check if campaign is complete
        campaign = self.db.fetch_one("""
            SELECT active_recipients, remote_schedule_id
            FROM campaigns
            WHERE id = ?
        """, {"1": campaign_id})

        if campaign and campaign["active_recipients"] == 0:
            self.db.execute_query("""
                UPDATE campaigns
                SET status = 'completed',
                    completed_at = ?
                WHERE id = ?
            """, {"1": datetime.now(timezone.utc), "2": campaign_id})

            logger.info(
                "local_campaign_completed",
                campaign_id=campaign_id
            )

            # Update remote run_parameters with completion
            try:
                metrics = self.get_campaign_metrics(campaign_id)
                schedule_id = campaign["remote_schedule_id"]

                resp = self.api.get(f"/orchestrator/schedules/{schedule_id}")
                resp.raise_for_status()
                schedule = resp.json()

                run_params = schedule.get("run_parameters", {})
                run_params["local_campaign_status"] = "completed"
                run_params["local_campaign_completed_at"] = datetime.now(timezone.utc).isoformat()
                run_params["final_sent_count"] = metrics["total_sent"]
                run_params["final_bounced_count"] = metrics["total_bounced"]
                run_params["final_failed_count"] = metrics["total_failed"]

                resp = self.api.patch(
                    f"/orchestrator/schedules/{schedule_id}",
                    json={"run_parameters": run_params}
                )
                resp.raise_for_status()

            except Exception as e:
                logger.error(
                    "failed_to_sync_campaign_completion",
                    campaign_id=campaign_id,
                    error=str(e)
                )
