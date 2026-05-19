"""
Native Orchestrator Celery Worker

Processes fully hydrated task payloads from the scheduler.
On success: marks campaign_email as 'sent' via API + increments Redis counter.
On failure: marks campaign_email as 'failed' via API + increments Redis counter.
When the LAST task for a run finishes, sends the real accurate HTML report.
Retries up to 3 times with 5-minute delays before giving up.
"""

from datetime import datetime
from typing import Dict, Any, Optional

import httpx

from app.integrations.smtp_client import SMTPPermanentError, SMTPTransientError

from app.workers.celery_app import celery_app
from app.core.config import settings
from app.core.auth import APIAuth
from app.core.logging import get_logger
from app.core.redis_client import record_task_outcome, claim_report_slot
from app.services.email_service import EmailService
from app.services.report_service import send_run_report

import re

logger = get_logger(__name__)

# ── Email validation ────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(
    r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
)

# SMTP codes that indicate a permanent hard bounce (address is gone forever)
_HARD_BOUNCE_CODES = {550, 551, 552, 553, 554}

# Phrases in error messages that confirm a hard bounce regardless of code
_HARD_BOUNCE_PHRASES = [
    "does not exist",
    "no such user",
    "user unknown",
    "invalid address",
    "address rejected",
    "mailbox not found",
    "recipient address rejected",
    "bad destination",
]


def _sanitize_email(email: Any) -> str:
    """
    Enterprise-grade email sanitization.
    Recursively removes common bullet points, invisible control characters,
    and standardized formatting to recover valid addresses from dirty datasets.
    """
    if not email or not isinstance(email, str):
        return ""

    # 1. Strip basic whitespace and invisible control characters
    email = email.strip()
    
    # 2. Handle bracketed formats: "Name <email@domain.com>" -> "email@domain.com"
    if "<" in email and ">" in email:
        match = re.search(r'<(.*?)>', email)
        if match:
            email = match.group(1)

    # 3. Recursively remove common bullet/list prefixes: "- ", "* ", "1. ", etc.
    # We do this character-by-character to handle nested garbage like "- * • email@..."
    garbage_prefixes = "-*•+.:/ \t\n\r#|~"
    while email and email[0] in garbage_prefixes:
        email = email[1:].strip()
        
    # 4. Standardize to lowercase and final strip
    return email.lower().strip()


def _validate_email_format(email: str) -> bool:
    """Return True if the email address is syntactically valid."""
    if not email:
        return False
    return bool(_EMAIL_RE.match(email))


def _classify_bounce(exc: Exception) -> str:
    """
    Classify an SMTP exception into a bounce type.

    Returns:
        'hard'    — permanent rejection (address gone, never retry)
        'soft'    — temporary failure (mailbox full, server busy)
        'invalid' — should not be reached here (handled before send)
    """
    error_str = str(exc).lower()
    smtp_code = getattr(exc.__cause__, "smtp_code", 0) or 0

    if isinstance(exc, SMTPPermanentError):
        # 4xx codes mis-classified as permanent by Gmail rate limiting
        # — treat as soft (recipient may accept tomorrow)
        if smtp_code in (421, 450):
            return "soft"
        # 5xx codes + hard-bounce phrases → permanent address failure
        if smtp_code in _HARD_BOUNCE_CODES or any(
            phrase in error_str for phrase in _HARD_BOUNCE_PHRASES
        ):
            return "hard"
        # Unknown permanent error — default to hard (don't keep retrying)
        return "hard"

    # SMTPTransientError or any other exception → temporary failure
    if isinstance(exc, SMTPTransientError):
        return "soft"
    return "soft"


def _update_campaign_email_status(
    campaign_email_id: int,
    status: str,
    error_message: Optional[str] = None,
    bounce_type: Optional[str] = None,
) -> None:
    """
    Update a campaign_emails row via the REST API PUT endpoint.
    Simple and reliable — no SQL or workflow config needed.
    """
    if not campaign_email_id:
        return

    try:
        url = (
            f"{settings.api_url}"
            f"/campaign-emails/{campaign_email_id}"
        )

        body: Dict[str, Any] = {
            "status": status,
            "last_attempt_at": datetime.utcnow().isoformat(),
        }
        if error_message:
            body["error_message"] = error_message[:500]
        if bounce_type:
            body["bounce_type"] = bounce_type

        with httpx.Client() as client:
            resp = client.put(
                url,
                json=body,
                auth=APIAuth(),
                timeout=10.0,
            )
            resp.raise_for_status()

    except Exception as e:
        logger.error(
            "campaign_email_status_update_failed",
            campaign_email_id=campaign_email_id,
            status=status,
            error=str(e),
        )


def _increment_credential_sent(
    credential_id: int,
    workflow_id: int,
) -> None:
    """
    Increments the current_day_sent counter on the email_smtp_credentials table.
    Uses the backend's arbitrary SQL execution endpoint so we don't need a
    direct database connection in the worker.
    """
    if not credential_id or not workflow_id:
        return

    try:
        url = (
            f"{settings.api_url}"
            f"/credentials/{credential_id}/increment-sent"
        )
        
        body = {
            "sql_query": (
                "UPDATE email_smtp_credentials "
                "SET "
                "    current_day_sent = IF(last_reset_date = CURDATE(), current_day_sent + 1, 1), "
                "    last_reset_date = CURDATE() "
                "WHERE id = :credential_id"
            ),
            "parameters": {
                "credential_id": credential_id
            }
        }

        with httpx.Client() as client:
            resp = client.post(
                url,
                json={"workflow_id": workflow_id},
                auth=APIAuth(),
                timeout=10.0,
            )
            resp.raise_for_status()

    except Exception as e:
        logger.error(
            "credential_increment_failed",
            credential_id=credential_id,
            error=str(e),
        )


def _try_send_deferred_report(
    log_id: Optional[int],
    success: bool,
) -> None:
    """
    Increment the Redis run counter for this task outcome.

    If this is the LAST task to finish (sent + failed == total),
    send the real accurate HTML report with actual delivery numbers.

    Guarantees:
    - Only ONE report is sent per run (claim_report_slot uses SET NX).
    - If Redis is down, fails silently (email was still delivered).
    - Never raises — reporting is non-critical.
    """
    if log_id is None:
        return

    try:
        result = record_task_outcome(log_id=log_id, success=success)

        # result is None if Redis is down or key has no 'total' (race guard)
        if not result or not result["done"]:
            return

        # Atomically claim the "send report" slot.
        # Only one worker wins this even if two finish simultaneously.
        if not claim_report_slot(log_id):
            logger.debug(
                "deferred_report_already_claimed",
                log_id=log_id,
            )
            return

        meta = result["meta"]
        logger.info(
            "sending_deferred_run_report",
            log_id=log_id,
            total=result["total"],
            sent=result["sent"],
            failed=result["failed"],
        )

        send_run_report(
            candidate_name=meta.get("candidate_name", "Unknown"),
            candidate_id=int(meta.get("candidate_id", 0)),
            total_dispatched=result["total"],
            success_count=result["sent"],
            failed_count=result["failed"],
            bounce_count=0,
            pending_remaining=int(meta.get("pending_remaining", 0)),
            smtp_account=meta.get("smtp_account", ""),
        )

    except Exception as e:
        logger.warning(
            "deferred_report_error",
            log_id=log_id,
            error=str(e),
        )


def _send_local_campaign_email(task, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send email for local DuckDB campaign and update local tables.

    Payload structure:
    {
        "mode": "local_duckdb_campaign",
        "attempt_id": int,
        "campaign_id": int,
        "recipient_id": int,
        "step_number": int,
        "vendor_email": str,
        "credential": {...},
        "template_subject": str,
        "template_body_html": str,
        "variables": {...},
        "log_id": int,
    }
    """
    from datetime import date
    from app.localdb.duckdb_client import DuckDBClient
    from app.services.local_campaign_service import LocalCampaignService
    from app.core.auth import APIAuth
    import httpx

    # Extract payload fields
    attempt_id = payload.get("attempt_id")
    campaign_id = payload.get("campaign_id")
    recipient_id = payload.get("recipient_id")
    vendor_email = _sanitize_email(payload.get("vendor_email"))
    credential = payload.get("credential", {})
    variables = payload.get("variables", {})
    log_id = payload.get("log_id")
    credential_id = credential.get("id")

    logger.info(
        "local_campaign_worker_started",
        campaign_id=campaign_id,
        recipient_id=recipient_id,
        attempt_id=attempt_id,
        vendor_email=vendor_email
    )

    try:
        # Validate email format
        if not _validate_email_format(vendor_email):
            logger.warning(
                "local_campaign_invalid_email",
                vendor_email=vendor_email,
                recipient_id=recipient_id
            )

            with DuckDBClient() as db_client:
                # Update attempt status
                db_client.execute_query("""
                    UPDATE campaign_email_attempts
                    SET status = 'bounced',
                        bounce_type = 'invalid',
                        error_message = ?,
                        sent_at = ?
                    WHERE id = ?
                """, {
                    "1": f"Invalid email format: {vendor_email}",
                    "2": datetime.utcnow(),
                    "3": attempt_id
                })

                # Initialize service and mark recipient bounced
                with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=60.0) as api_client:
                    service = LocalCampaignService(db_client, api_client)
                    service.mark_recipient_bounced(recipient_id, "invalid")
                    service.update_daily_metrics(
                        campaign_id, date.today(),
                        bounced=1, bounce_type="invalid"
                    )

            _try_send_deferred_report(log_id=log_id, success=False)
            return {
                "success": False,
                "error": "invalid_email_format",
                "vendor_email": vendor_email,
                "mode": "local_duckdb_campaign"
            }

        # Ensure template variables are populated
        variables["vendor_email"] = vendor_email
        variables["recipient_email"] = vendor_email

        # Send email
        email_service = EmailService()
        smtp_host = credential.get("smtp_host") or f"smtp.{credential.get('email', 'gmail.com').split('@')[-1]}"

        email_service.send_outreach(
            smtp_host=smtp_host,
            from_email=credential.get("email"),
            from_name=credential.get("email").split("@")[0],
            password=credential.get("app_password") or credential.get("password"),
            to_email=vendor_email,
            template_subject=payload.get("template_subject"),
            template_body_html=payload.get("template_body_html"),
            variables=variables,
        )

        # Success: update local status
        with DuckDBClient() as db_client:
            # Update attempt status
            db_client.execute_query("""
                UPDATE campaign_email_attempts
                SET status = 'sent',
                    sent_at = ?
                WHERE id = ?
            """, {"1": datetime.utcnow(), "2": attempt_id})

            # Initialize service and advance recipient
            with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=60.0) as api_client:
                service = LocalCampaignService(db_client, api_client)
                service.advance_recipient(recipient_id, campaign_id)
                service.update_daily_metrics(
                    campaign_id, date.today(), sent=1
                )

        # Increment SMTP credential counter
        _increment_credential_sent(credential_id, payload.get("workflow_id"))

        # Try deferred report
        _try_send_deferred_report(log_id=log_id, success=True)

        logger.info(
            "local_campaign_email_sent_successfully",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            vendor_email=vendor_email
        )

        return {
            "success": True,
            "vendor_email": vendor_email,
            "mode": "local_duckdb_campaign"
        }

    except SMTPPermanentError as e:
        # Permanent bounce (hard)
        bounce_type = _classify_bounce(e)
        error_msg = str(e)[:500]

        logger.error(
            "local_campaign_permanent_bounce",
            vendor_email=vendor_email,
            recipient_id=recipient_id,
            bounce_type=bounce_type,
            error=error_msg
        )

        with DuckDBClient() as db_client:
            # Update attempt status
            db_client.execute_query("""
                UPDATE campaign_email_attempts
                SET status = 'bounced',
                    bounce_type = ?,
                    error_message = ?,
                    sent_at = ?
                WHERE id = ?
            """, {
                "1": bounce_type,
                "2": error_msg,
                "3": datetime.utcnow(),
                "4": attempt_id
            })

            # Mark recipient bounced
            with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=60.0) as api_client:
                service = LocalCampaignService(db_client, api_client)
                service.mark_recipient_bounced(recipient_id, bounce_type)
                service.update_daily_metrics(
                    campaign_id, date.today(),
                    bounced=1, bounce_type=bounce_type
                )

        _try_send_deferred_report(log_id=log_id, success=False)

        return {
            "success": False,
            "error": error_msg,
            "bounce_type": bounce_type,
            "mode": "local_duckdb_campaign"
        }

    except Exception as e:
        # Transient error - retry via Celery
        logger.warning(
            "local_campaign_transient_error",
            vendor_email=vendor_email,
            recipient_id=recipient_id,
            error=str(e),
            attempt=task.request.retries
        )

        try:
            raise task.retry(exc=e)
        except task.MaxRetriesExceededError:
            # All retries exhausted - mark as soft bounce
            error_msg = str(e)[:500]

            with DuckDBClient() as db_client:
                # Update attempt status
                db_client.execute_query("""
                    UPDATE campaign_email_attempts
                    SET status = 'bounced',
                        bounce_type = 'soft',
                        error_message = ?,
                        sent_at = ?
                    WHERE id = ?
                """, {
                    "1": error_msg,
                    "2": datetime.utcnow(),
                    "3": attempt_id
                })

                # Update metrics (soft bounce, keep recipient active)
                with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=60.0) as api_client:
                    service = LocalCampaignService(db_client, api_client)
                    service.update_daily_metrics(
                        campaign_id, date.today(),
                        bounced=1, bounce_type="soft"
                    )

            _try_send_deferred_report(log_id=log_id, success=False)

            return {
                "success": False,
                "error": error_msg,
                "bounce_type": "soft",
                "retries_exceeded": True,
                "mode": "local_duckdb_campaign"
            }


@celery_app.task(
    bind=True,
    name="send_outreach_email",
    max_retries=3,
    default_retry_delay=300,
)
def send_outreach_email(self, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sends one outreach email via SMTP and updates campaign_emails status.

    Supports dual modes:
    - remote: existing remote API flow (default)
    - local_duckdb_campaign: new local DuckDB campaign flow
    """
    # Detect mode
    mode = payload.get("mode", "remote")

    if mode == "local_duckdb_campaign":
        return _send_local_campaign_email(self, payload)

    # ── REMOTE MODE (existing flow) ────────────────────────────────
    # ── 0. Enterprise Sanitization ────────────────────────────────
    # Recovers emails from dirty data (hyphens, bullet points, brackets, etc.)
    vendor_email = _sanitize_email(payload.get("vendor_email"))

    campaign_email_id = payload.get("campaign_email_id")
    credential = payload.get("credential", {})
    variables = payload.get("variables", {})
    wf_id = payload.get("workflow_id")
    log_id = payload.get("log_id")            # used for deferred report
    update_sql = payload.get("recipient_update_sql")

    logger.info("worker_started", vendor_email=vendor_email)

    try:
        # ── 0. Validate email format BEFORE attempting send ────────
        if not _validate_email_format(vendor_email):
            logger.warning(
                "worker_invalid_email",
                vendor_email=vendor_email,
            )
            _update_campaign_email_status(
                campaign_email_id=campaign_email_id,
                status="bounced",
                bounce_type="invalid",
                error_message=f"Invalid email format: {vendor_email}",
            )
            _try_send_deferred_report(log_id=log_id, success=False)
            return {
                "success": False,
                "error": "invalid_email_format",
                "vendor_email": vendor_email,
            }

        # Ensure template variables are populated
        variables["vendor_email"] = vendor_email
        variables["recipient_email"] = vendor_email

        # ── DEBUG CHECKPOINT 4: verify vars at render time ────────
        logger.debug(
            "worker_variables_received",
            campaign_email_id=campaign_email_id,
            candidate_name=variables.get("candidate_name") or "<MISSING>",
            linkedin_url=variables.get("linkedin_url") or "<MISSING>",
            variables_keys=list(variables.keys()),
        )

        # ── 1. Resolve SMTP host ───────────────────────────────────
        smtp_host = credential.get("smtp_host")
        if not smtp_host:
            domain = credential.get("email", "").split("@")[-1]
            smtp_host = f"smtp.{domain}"

        password = (
            credential.get("app_password") or credential.get("password")
        )

        # ── 2. Send email (with Gmail compliance headers) ──────────
        email_service = EmailService()
        email_service.send_outreach(
            smtp_host=smtp_host,
            from_email=credential.get("email"),
            from_name=variables.get("candidate_name"),
            password=password,
            to_email=vendor_email,
            template_subject=payload.get("template_subject", ""),
            template_body_html=payload.get("template_body_html", ""),
            variables=variables,
        )

        # ── 3. Mark as sent ───────────────────────────────────────
        _update_campaign_email_status(
            campaign_email_id=campaign_email_id,
            status="sent",
        )

        # ── 3.5 Increment SMTP credential daily counter ────────────
        if credential.get("id"):
            _increment_credential_sent(
                credential_id=credential.get("id"),
                workflow_id=wf_id,
            )

        # ── 4. Increment Redis counter; send report if last task ──
        _try_send_deferred_report(log_id=log_id, success=True)

        logger.info("worker_completed", vendor_email=vendor_email)
        return {"success": True, "vendor_email": vendor_email}

    except SMTPPermanentError as e:
        # ── Permanent SMTP failure — classify and do NOT retry ─────
        bounce = _classify_bounce(e)
        logger.error(
            "worker_permanent_bounce",
            error=str(e),
            bounce_type=bounce,
            vendor_email=vendor_email,
        )
        _update_campaign_email_status(
            campaign_email_id=campaign_email_id,
            status="bounced",
            bounce_type=bounce,
            error_message=str(e)[:400],
        )
        _try_send_deferred_report(log_id=log_id, success=False)
        return {
            "success": False,
            "error": str(e),
            "bounce_type": bounce,
        }

    except Exception as e:
        # ── Transient failure — retry via Celery ──────────────────
        logger.error(
            "worker_transient_error",
            error=str(e),
            vendor_email=vendor_email,
            exc_info=True,
        )

        try:
            raise self.retry(exc=e)
        except self.MaxRetriesExceededError:
            # All retries exhausted — classify as soft bounce
            _update_campaign_email_status(
                campaign_email_id=campaign_email_id,
                status="bounced",
                bounce_type="soft",
                error_message=str(e)[:400],
            )
            _try_send_deferred_report(log_id=log_id, success=False)
            return {
                "success": False,
                "error": str(e),
                "bounce_type": "soft",
                "retries_exceeded": True,
            }
