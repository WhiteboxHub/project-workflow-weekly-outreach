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

from app.integrations.smtp_client import SMTPPermanentError

from app.workers.celery_app import celery_app
from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis_client import record_task_outcome, claim_report_slot
from app.services.email_service import EmailService
from app.services.report_service import send_run_report

logger = get_logger(__name__)


def _update_campaign_email_status(
    campaign_email_id: int,
    status: str,
    error_message: str = None,
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
        headers = (
            {"Authorization": f"Bearer {settings.api_bearer_token}"}
            if settings.api_bearer_token else {}
        )

        body: Dict[str, Any] = {
            "status": status,
            "last_attempt_at": datetime.utcnow().isoformat(),
        }
        if error_message:
            body["error_message"] = error_message[:500]

        with httpx.Client() as client:
            resp = client.put(
                url,
                json=body,
                headers=headers,
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
        headers = (
            {"Authorization": f"Bearer {settings.api_bearer_token}"}
            if settings.api_bearer_token else {}
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
                json=body,
                headers=headers,
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


@celery_app.task(
    bind=True,
    name="send_outreach_email",
    max_retries=3,
    default_retry_delay=300,
)
def send_outreach_email(self, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sends one outreach email via SMTP and updates campaign_emails status.
    """
    vendor_email = payload.get("vendor_email")
    campaign_email_id = payload.get("campaign_email_id")
    credential = payload.get("credential", {})
    variables = payload.get("variables", {})
    wf_id = payload.get("workflow_id")
    log_id = payload.get("log_id")            # used for deferred report
    update_sql = payload.get("recipient_update_sql")

    logger.info("worker_started", vendor_email=vendor_email)

    try:
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
        # ── Gmail permanently blocked this message — do NOT retry ──
        logger.error(
            "worker_permanent_block",
            error=str(e),
            vendor_email=vendor_email,
        )
        _update_campaign_email_status(
            campaign_email_id=campaign_email_id,
            status="failed",
            error_message=f"Permanent block: {str(e)[:400]}",
        )
        _try_send_deferred_report(log_id=log_id, success=False)
        return {
            "success": False,
            "error": str(e),
            "permanent_block": True,
        }

    except Exception as e:
        logger.error(
            "worker_error",
            error=str(e),
            vendor_email=vendor_email,
            exc_info=True,
        )

        try:
            raise self.retry(exc=e)
        except self.MaxRetriesExceededError:
            # Mark permanently failed after all retries exhausted
            _update_campaign_email_status(
                campaign_email_id=campaign_email_id,
                status="failed",
                error_message=str(e),
            )

            # ── Increment Redis counter; send report if last task ──
            _try_send_deferred_report(log_id=log_id, success=False)

            return {
                "success": False,
                "error": str(e),
                "retries_exceeded": True,
            }
