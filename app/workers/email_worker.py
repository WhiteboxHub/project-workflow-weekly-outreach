"""
Native Orchestrator Celery Worker

Processes fully hydrated task payloads from the scheduler.
On success: marks campaign_email as 'sent' via API + increments Redis counter.
On failure: marks campaign_email as 'failed' via API + increments Redis counter.
When the LAST task for a run finishes, sends the real accurate HTML report.
Retries up to 3 times with 5-minute delays before giving up.
"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional

import httpx

from app.integrations.smtp_client import SMTPPermanentError, SMTPTransientError, SMTPAuthError, SMTPRateLimitError

from app.workers.celery_app import celery_app
from app.core.config import settings
from app.core.auth import APIAuth
from app.core.logging import get_logger
from app.core.redis_client import (
    record_task_outcome,
    claim_report_slot,
    mark_credential_rate_limited_redis,
    is_credential_rate_limited_redis
)
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

    NOTE: SMTPAuthError is NOT classified here because it is not a bounce
    at all — it is a credential failure.  Auth errors are handled in their
    own dedicated except block and marked as 'credential_auth_failure'.
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
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
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
            f"/orchestrator/workflows/{workflow_id}/execute-reset-sql"
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
                "credential_id": credential_id,
            },
        }

        with httpx.Client() as client:
            resp = client.post(
                url,
                json=body,
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
        "credential_id": int,
        "step_number": int,
        "vendor_email": str,
        "credential": {...},
        "template_subject": str,
        "template_body_html": str,
        "variables": {...},
        "log_id": int,
    }

    Recipient lifecycle states:
      - success          → attempt: sent       | recipient: active (advance step)
      - rate_limit       → attempt: rate_limited | recipient: deferred (retry tomorrow)
      - hard_bounce      → attempt: bounced     | recipient: hard_bounce (stop forever)
      - soft_bounce      → attempt: bounced     | recipient: active (allow retry)
      - auth_failure     → attempt: failed      | recipient: active (credential broken)
      - invalid_email    → attempt: bounced     | recipient: invalid (stop forever)
      - transient error  → Celery retry with exponential backoff (up to 3x)
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
    variables = payload.get("variables", {})
    log_id = payload.get("log_id")
    credential_id = payload.get("credential_id")
    retry_count = task.request.retries

    # Read the full credential object directly from the payload injected by the scheduler
    credential = payload.get("credential", {})
    if not credential and credential_id:
        # Fallback for payloads already in the queue before the update
        with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=30.0) as client:
            resp = client.get(f"/email-smtp-credentials/{credential_id}")
            if resp.is_success:
                credential = resp.json()

    if not credential or not credential.get("email"):
        raise ValueError(f"Failed to load valid SMTP credential for ID {credential_id}")

    # ── Fast-fail: check if this credential was already rate-limited ──────────
    # If the credential was marked is_healthy=False by a previous task, OR if
    # it was instantly flagged in Redis by a task running 2 milliseconds ago,
    # skip immediately without hitting Gmail. This flushes 100s of remaining queued
    # tasks in milliseconds instead of hammering the provider with blocked requests.
    is_healthy = credential.get("is_healthy", True)
    if not is_healthy or is_credential_rate_limited_redis(credential_id):
        logger.warning(
            "local_campaign_skipped_unhealthy_credential",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            attempt_id=attempt_id,
            credential_id=credential_id,
            vendor_email=vendor_email,
            hint="Credential is rate-limited. Recipient deferred to next healthy run.",
        )
        with DuckDBClient() as db_client:
            db_client.execute_query("""
                UPDATE campaign_email_attempts
                SET status = 'rate_limited',
                    error_message = ?,
                    sent_at = ?
                WHERE id = ?
            """, {
                "1": "Credential marked unhealthy (rate-limited). Deferred to next run.",
                "2": datetime.now(timezone.utc),
                "3": attempt_id
            })
            with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=60.0) as api_client:
                service = LocalCampaignService(db_client, api_client)
                service.mark_recipient_deferred(recipient_id)
                service.update_daily_metrics(campaign_id, date.today(), failed=1)

        _try_send_deferred_report(log_id=log_id, success=False)
        return {
            "success": False,
            "skipped": True,
            "reason": "credential_rate_limited",
            "credential_id": credential_id,
            "mode": "local_duckdb_campaign",
        }

    logger.info(
        "local_campaign_worker_started",
        campaign_id=campaign_id,
        recipient_id=recipient_id,
        attempt_id=attempt_id,
        credential_id=credential_id,
        vendor_email=vendor_email,
        retry_count=retry_count,
    )

    try:
        # ── Validate email format BEFORE any SMTP connection ──────────────────
        if not _validate_email_format(vendor_email):
            logger.warning(
                "local_campaign_invalid_email",
                vendor_email=vendor_email,
                recipient_id=recipient_id,
            )
            with DuckDBClient() as db_client:
                db_client.execute_query("""
                    UPDATE campaign_email_attempts
                    SET status = 'bounced',
                        bounce_type = 'invalid',
                        error_message = ?,
                        sent_at = ?
                    WHERE id = ?
                """, {
                    "1": f"Invalid email format: {vendor_email}",
                    "2": datetime.now(timezone.utc),
                    "3": attempt_id
                })
                with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=60.0) as api_client:
                    service = LocalCampaignService(db_client, api_client)
                    service.update_recipient_status(recipient_id, "invalid")
                    service.update_daily_metrics(campaign_id, date.today(), bounced=1, bounce_type="invalid")

            _try_send_deferred_report(log_id=log_id, success=False)
            return {
                "success": False,
                "error": "invalid_email_format",
                "vendor_email": vendor_email,
                "mode": "local_duckdb_campaign",
            }

        # ── Populate template variables ───────────────────────────────────────
        variables["vendor_email"] = vendor_email
        variables["recipient_email"] = vendor_email

        # ── Send email ────────────────────────────────────────────────────────
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

        # ── Success path ──────────────────────────────────────────────────────
        with DuckDBClient() as db_client:
            db_client.execute_query("""
                UPDATE campaign_email_attempts
                SET status = 'sent',
                    sent_at = ?
                WHERE id = ?
            """, {"1": datetime.now(timezone.utc), "2": attempt_id})

            with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=60.0) as api_client:
                service = LocalCampaignService(db_client, api_client)
                service.advance_recipient(recipient_id, campaign_id)
                service.update_daily_metrics(campaign_id, date.today(), sent=1)
                # Restore current_day_sent tracking — critical for proactive quota management
                service.increment_credential_sent(credential_id)

        _try_send_deferred_report(log_id=log_id, success=True)

        logger.info(
            "local_campaign_email_sent_successfully",
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            credential_id=credential_id,
            vendor_email=vendor_email,
        )

        return {
            "success": True,
            "vendor_email": vendor_email,
            "mode": "local_duckdb_campaign",
        }

    except SMTPRateLimitError as e:
        # ── Provider quota / rate-limit hit ───────────────────────────────────
        # This is a SENDER transport failure — NOT the recipient's fault.
        # 1. Mark the CREDENTIAL as unhealthy so remaining queued tasks fast-fail.
        # 2. Mark this RECIPIENT as deferred (retry tomorrow with a healthy account).
        # 3. Do NOT retry this task — the account won't un-limit for 24 hours.
        error_msg = str(e)[:500]

        logger.warning(
            "local_campaign_rate_limit_hit",
            vendor_email=vendor_email,
            recipient_id=recipient_id,
            campaign_id=campaign_id,
            credential_id=credential_id,
            error=error_msg,
            action="credential marked unhealthy, recipient deferred",
        )

        # Mark the attempt as rate_limited
        with DuckDBClient() as db_client:
            db_client.execute_query("""
                UPDATE campaign_email_attempts
                SET status = 'rate_limited',
                    error_message = ?,
                    sent_at = ?
                WHERE id = ?
            """, {
                "1": error_msg,
                "2": datetime.now(timezone.utc),
                "3": attempt_id
            })

            # Defer the recipient — they will be retried tomorrow
            with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=60.0) as api_client:
                service = LocalCampaignService(db_client, api_client)
                service.mark_recipient_deferred(recipient_id)
                service.update_daily_metrics(campaign_id, date.today(), failed=1)
                
                # Instantly mark in Redis so the next 800 tasks in the queue fast-fail
                # without making any API requests or hitting Gmail!
                mark_credential_rate_limited_redis(credential_id)
                
                # Mark this credential as unhealthy in PostgreSQL
                # Other tasks assigned to healthy credentials keep running!
                service.mark_credential_rate_limited(credential_id)

        _try_send_deferred_report(log_id=log_id, success=False)

        return {
            "success": False,
            "error": error_msg,
            "error_type": "rate_limit",
            "credential_id": credential_id,
            "recipient_deferred": True,
            "mode": "local_duckdb_campaign",
        }

    except SMTPPermanentError as e:
        # ── Hard bounce: recipient address is permanently bad ─────────────────
        error_msg = str(e)[:500]

        logger.error(
            "local_campaign_hard_bounce",
            vendor_email=vendor_email,
            recipient_id=recipient_id,
            credential_id=credential_id,
            error=error_msg,
        )

        with DuckDBClient() as db_client:
            db_client.execute_query("""
                UPDATE campaign_email_attempts
                SET status = 'bounced',
                    bounce_type = 'hard',
                    error_message = ?,
                    sent_at = ?
                WHERE id = ?
            """, {
                "1": error_msg,
                "2": datetime.now(timezone.utc),
                "3": attempt_id
            })

            with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=60.0) as api_client:
                service = LocalCampaignService(db_client, api_client)
                service.update_recipient_status(recipient_id, "hard_bounce", bounce_type="hard")
                service.update_daily_metrics(campaign_id, date.today(), bounced=1, bounce_type="hard")

        _try_send_deferred_report(log_id=log_id, success=False)

        return {
            "success": False,
            "error": error_msg,
            "error_type": "hard_bounce",
            "mode": "local_duckdb_campaign",
        }

    except SMTPAuthError as e:
        # ── Credential auth failure — do NOT retry ────────────────────────────
        # Gmail 534/535: bad or missing App Password. The credential is broken
        # until an admin re-generates it. Retrying 3x wastes 15 minutes.
        error_msg = str(e)[:500]

        logger.error(
            "local_campaign_auth_failure",
            vendor_email=vendor_email,
            recipient_id=recipient_id,
            campaign_id=campaign_id,
            credential_id=credential_id,
            error=error_msg,
            action_required="Re-generate App Password in Google Account settings",
        )

        with DuckDBClient() as db_client:
            db_client.execute_query("""
                UPDATE campaign_email_attempts
                SET status = 'failed',
                    error_message = ?,
                    sent_at = ?
                WHERE id = ?
            """, {
                "1": f"[AUTH_FAILURE] {error_msg}",
                "2": datetime.now(timezone.utc),
                "3": attempt_id
            })

        _try_send_deferred_report(log_id=log_id, success=False)

        return {
            "success": False,
            "error": error_msg,
            "error_type": "credential_auth_failure",
            "retried": False,
            "mode": "local_duckdb_campaign",
        }

    except SMTPTransientError as e:
        # ── Transient failure — retry with exponential backoff ────────────────
        # Mailbox full, server temporarily busy, network hiccup.
        # Backoff: 1m → 2m → 4m (2^retry * 60s)
        backoff_seconds = (2 ** retry_count) * 60
        error_msg = str(e)[:500]

        logger.warning(
            "local_campaign_transient_error",
            vendor_email=vendor_email,
            recipient_id=recipient_id,
            credential_id=credential_id,
            error=error_msg,
            retry_count=retry_count,
            next_retry_in_seconds=backoff_seconds,
        )

        try:
            raise task.retry(exc=e, countdown=backoff_seconds)
        except task.MaxRetriesExceededError:
            # All retries exhausted — classify as soft bounce, keep recipient active
            with DuckDBClient() as db_client:
                db_client.execute_query("""
                    UPDATE campaign_email_attempts
                    SET status = 'bounced',
                        bounce_type = 'soft',
                        error_message = ?,
                        sent_at = ?
                    WHERE id = ?
                """, {
                    "1": f"[MAX_RETRIES] {error_msg}",
                    "2": datetime.now(timezone.utc),
                    "3": attempt_id
                })
                with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=60.0) as api_client:
                    service = LocalCampaignService(db_client, api_client)
                    service.update_recipient_status(recipient_id, "soft_bounce", bounce_type="soft")
                    service.update_daily_metrics(campaign_id, date.today(), bounced=1, bounce_type="soft")

            _try_send_deferred_report(log_id=log_id, success=False)

            return {
                "success": False,
                "error": error_msg,
                "error_type": "soft_bounce",
                "retries_exceeded": True,
                "mode": "local_duckdb_campaign",
            }

    except Exception as e:
        # ── Unknown error — exponential backoff retry ─────────────────────────
        backoff_seconds = (2 ** retry_count) * 60
        error_msg = str(e)[:500]

        logger.error(
            "local_campaign_unexpected_error",
            vendor_email=vendor_email,
            recipient_id=recipient_id,
            credential_id=credential_id,
            error=error_msg,
            retry_count=retry_count,
            next_retry_in_seconds=backoff_seconds,
        )

        try:
            raise task.retry(exc=e, countdown=backoff_seconds)
        except task.MaxRetriesExceededError:
            with DuckDBClient() as db_client:
                db_client.execute_query("""
                    UPDATE campaign_email_attempts
                    SET status = 'bounced',
                        bounce_type = 'soft',
                        error_message = ?,
                        sent_at = ?
                    WHERE id = ?
                """, {
                    "1": f"[UNKNOWN_MAX_RETRIES] {error_msg}",
                    "2": datetime.now(timezone.utc),
                    "3": attempt_id
                })
                with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=60.0) as api_client:
                    service = LocalCampaignService(db_client, api_client)
                    service.update_daily_metrics(campaign_id, date.today(), bounced=1, bounce_type="soft")

            _try_send_deferred_report(log_id=log_id, success=False)

            return {
                "success": False,
                "error": error_msg,
                "error_type": "unknown",
                "retries_exceeded": True,
                "mode": "local_duckdb_campaign",
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
    variables = payload.get("variables", {})
    wf_id = payload.get("workflow_id")
    log_id = payload.get("log_id")            # used for deferred report
    update_sql = payload.get("recipient_update_sql")

    credential_id = payload.get("credential_id")
    
    # Read the full credential object directly from the payload injected by the scheduler
    credential = payload.get("credential", {})
    if not credential and credential_id:
        # Fallback for payloads already in the queue before the update
        with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=30.0) as client:
            resp = client.get(f"/email-smtp-credentials/{credential_id}")
            if resp.is_success:
                credential = resp.json()

    if not credential or not credential.get("email"):
        raise ValueError(f"Failed to load valid SMTP credential for ID {credential_id}")

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
        # Restored: critical for proactive quota management so the scheduler
        # knows when a credential is approaching its limit before Gmail blocks.
        if credential_id:
            _increment_credential_sent(credential_id, wf_id)

        # ── 4. Increment Redis counter; send report if last task ──
        _try_send_deferred_report(log_id=log_id, success=True)

        logger.info("worker_completed", vendor_email=vendor_email)
        return {"success": True, "vendor_email": vendor_email}

    except SMTPRateLimitError as e:
        # ── Provider rate-limit — NOT a bounce, mark as deferred ───
        logger.warning(
            "worker_rate_limit_hit",
            error=str(e),
            vendor_email=vendor_email,
            credential_id=credential_id,
            smtp_account=credential.get("email"),
        )
        _update_campaign_email_status(
            campaign_email_id=campaign_email_id,
            status="deferred",
            error_message=f"[RATE_LIMIT] {str(e)[:400]}",
        )
        # Instantly block this credential across all workers for 24 hours
        if credential_id:
            mark_credential_rate_limited_redis(credential_id)
        _try_send_deferred_report(log_id=log_id, success=False)
        return {
            "success": False,
            "error": str(e),
            "error_type": "rate_limit",
            "recipient_deferred": True,
        }

    except SMTPPermanentError as e:
        # ── Permanent SMTP failure — hard bounce, do NOT retry ─────
        bounce = _classify_bounce(e)
        logger.error(
            "worker_hard_bounce",
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
            "error_type": "hard_bounce",
            "bounce_type": bounce,
        }

    except SMTPAuthError as e:
        # ── Credential auth failure — do NOT retry ──────────────
        logger.error(
            "worker_auth_failure",
            error=str(e),
            vendor_email=vendor_email,
            smtp_account=credential.get("email"),
            action_required="Re-generate App Password in Google Account settings",
        )
        _update_campaign_email_status(
            campaign_email_id=campaign_email_id,
            status="failed",
            error_message=f"[AUTH_FAILURE] {str(e)[:400]}",
        )
        
        # Mark credential permanently unhealthy in DB so scheduler ignores it
        if credential_id:
            try:
                with httpx.Client(base_url=settings.api_url, auth=APIAuth(), timeout=10.0) as client:
                    client.put(
                        f"/email-smtp-credentials/{credential_id}",
                        json={"is_healthy": False},
                    )
            except Exception:
                pass
                
        _try_send_deferred_report(log_id=log_id, success=False)
        return {
            "success": False,
            "error": str(e),
            "error_type": "credential_auth_failure",
            "retried": False,
            "vendor_email": vendor_email,
        }

    except Exception as e:
        # ── Transient failure — retry with exponential backoff ─────
        retry_count = self.request.retries
        backoff_seconds = (2 ** retry_count) * 60
        logger.error(
            "worker_transient_error",
            error=str(e),
            vendor_email=vendor_email,
            retry_count=retry_count,
            next_retry_in_seconds=backoff_seconds,
        )

        try:
            raise self.retry(exc=e, countdown=backoff_seconds)
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
                "error_type": "soft_bounce",
                "retries_exceeded": True,
            }
