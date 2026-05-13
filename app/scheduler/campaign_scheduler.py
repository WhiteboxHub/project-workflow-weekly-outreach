"""
Native Orchestrator Campaign Scheduler

Flow per schedule:
1. Fetch & lock due schedules via /orchestrator
2. Fetch SMTP credentials → calculate daily limit
3. Generate snapshot (INSERT IGNORE into campaign_emails)
4. Dispatch pending emails (FOR UPDATE SKIP LOCKED)
5. Hydrate & enqueue Celery tasks with staggered delays
6. Send HTML run report to admin
"""

import random
import uuid
from typing import Dict, Any

import httpx
from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis_client import (
    store_run_metadata,
    update_pending_remaining,
)
from app.services.report_service import send_run_report

logger = get_logger(__name__)


def _calculate_daily_limit(cred: Dict[str, Any]) -> int:
    """
    Return how many more emails this SMTP account can send today.
    Respects warmup vs full-send limits and subtracts already-sent count.
    """
    if cred.get("is_warming_up"):
        daily_cap = int(cred.get("warmup_daily_limit") or 5)
    else:
        daily_cap = int(cred.get("daily_limit") or 50)

    # Reset logic: if last_reset_date != today, current_day_sent is stale → treat as 0
    from datetime import date
    last_reset = cred.get("last_reset_date")
    if last_reset and str(last_reset) != str(date.today()):
        already_sent = 0
    else:
        already_sent = int(cred.get("current_day_sent") or 0)

    remaining = max(0, daily_cap - already_sent)
    return remaining



def _refresh_token(client: httpx.Client) -> str:
    """
    Attempt to fetch a fresh JWT by logging into the backend.
    Requires API_LOGIN_EMAIL and API_LOGIN_PASSWORD in .env.
    Returns the new token string, or None if login is not configured
    or the login request fails.
    """
    if not settings.api_login_email or not settings.api_login_password:
        return None
    try:
        resp = client.post(
            settings.api_login_path,
            json={
                "email": settings.api_login_email,
                "password": settings.api_login_password,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if token:
            logger.info(
                "token_refreshed_via_login",
                email=settings.api_login_email,
            )
        return token
    except Exception as e:
        logger.error(
            "token_refresh_failed",
            error=str(e),
        )
        return None


def run_scheduler() -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "schedules_processed": 0,
        "emails_enqueued": 0,
        "errors": 0,
    }

    base_url = settings.api_url
    headers = {}
    if settings.api_bearer_token:
        headers["Authorization"] = f"Bearer {settings.api_bearer_token}"

    try:
        with httpx.Client(
            base_url=base_url, headers=headers, timeout=60.0
        ) as client:

            # ── 1. Pull Due Schedules ──────────────────────────────
            due_resp = client.get("/orchestrator/schedules/due")

            # ── Auto token refresh on 401 ──────────────────────────
            # If the bearer token has expired, try to log in again
            # using API_LOGIN_EMAIL + API_LOGIN_PASSWORD from .env.
            # If those are not set, raise immediately so the error
            # is obvious in the logs.
            if due_resp.status_code == 401:
                new_token = _refresh_token(client)
                if new_token:
                    client.headers["Authorization"] = (
                        f"Bearer {new_token}"
                    )
                    logger.warning(
                        "bearer_token_refreshed",
                        hint=(
                            "Update API_BEARER_TOKEN in .env with "
                            "the new token to avoid re-login on restart"
                        ),
                    )
                    due_resp = client.get("/orchestrator/schedules/due")
                else:
                    logger.error(
                        "bearer_token_expired_no_credentials",
                        hint=(
                            "Set API_LOGIN_EMAIL and API_LOGIN_PASSWORD "
                            "in .env for automatic token refresh"
                        ),
                    )
                    due_resp.raise_for_status()

            due_resp.raise_for_status()
            schedules = due_resp.json()


            if not schedules:
                logger.debug("no_schedules_due")
                return stats

            from app.workers.email_worker import send_outreach_email

            for schedule in schedules:
                sched_id = schedule.get("id")
                wf_id = schedule.get("automation_workflow_id")
                run_params = schedule.get("run_parameters") or {}
                candidate_id = run_params.get("candidate_id")

                if not candidate_id:
                    logger.warning(
                        "schedule_missing_candidate_id",
                        schedule_id=sched_id
                    )
                    continue

                # ── 2. Lock the schedule ───────────────────────────
                lock_resp = client.post(
                    f"/orchestrator/schedules/{sched_id}/lock"
                )
                if not lock_resp.json().get("success"):
                    logger.warning(
                        "schedule_lock_failed",
                        schedule_id=sched_id,
                        status_code=lock_resp.status_code,
                        body=lock_resp.text[:200],
                    )
                    continue

                stats["schedules_processed"] += 1
                run_id = str(uuid.uuid4())
                log_id = None
                candidate_name = f"Candidate #{candidate_id}"

                try:
                    # ── 3. Create run log ──────────────────────────
                    log_resp = client.post("/orchestrator/logs", json={
                        "workflow_id": wf_id,
                        "schedule_id": sched_id,
                        "run_id": run_id,
                        "status": "running",
                        "parameters_used": run_params,
                    })
                    log_resp.raise_for_status()
                    log_id = log_resp.json().get("id")

                    # ── 4. Fetch execution bundle ──────────────────
                    bundle_resp = client.get(
                        f"/automation-workflow/{wf_id}/execution-bundle"
                    )
                    bundle_resp.raise_for_status()
                    bundle = bundle_resp.json()

                    # ── DEBUG CHECKPOINT 1: what did the bundle return? ──
                    logger.debug(
                        "bundle_received",
                        bundle_keys=list(bundle.keys()),
                        candidate_in_bundle=bool(
                            bundle.get("candidate")
                        ),
                        candidate_name_in_bundle=bundle.get(
                            "candidate", {}
                        ).get("candidate_name"),
                        linkedin_url_in_bundle=bundle.get(
                            "candidate", {}
                        ).get("linkedin_url"),
                        candidate_name_in_run_params=run_params.get(
                            "candidate_name"
                        ),
                        linkedin_url_in_run_params=run_params.get(
                            "linkedin_url"
                        ),
                        run_params_keys=list(run_params.keys()),
                    )

                    template = bundle.get("template", {})
                    smtp_creds = bundle.get("smtp_credentials") or []

                    # Fallback: fetch creds directly by candidate
                    if not smtp_creds:
                        cred_resp = client.get(
                            f"/orchestrator/candidate-credentials/"
                            f"{candidate_id}"
                        )
                        if cred_resp.is_success:
                            cred_data = cred_resp.json()
                            smtp_creds = (
                                cred_data
                                if isinstance(cred_data, list)
                                else [cred_data]
                            )

                    # Filter only healthy & active credentials
                    smtp_creds = [
                        c for c in smtp_creds
                        if c.get("is_active") and c.get("is_healthy", True)
                    ]

                    if not smtp_creds:
                        raise ValueError(
                            "No active healthy SMTP credentials for "
                            f"candidate {candidate_id}"
                        )

                    # Show all rotated accounts in the report
                    emails = [c.get("email") for c in smtp_creds if c.get("email")]
                    if len(emails) > 1:
                        smtp_account = f"Multiple ({len(emails)} Accounts): " + ", ".join(emails)
                    elif emails:
                        smtp_account = emails[0]
                    else:
                        smtp_account = "unknown@smtp"

                    # Strictly require candidate name and linkedin_url from run_parameters
                    candidate_name = run_params.get("candidate_name")
                    linkedin_url_resolved = run_params.get("linkedin_url")

                    if not candidate_name or not linkedin_url_resolved:
                        raise ValueError(
                            f"Missing required identity variables (candidate_name or linkedin_url) "
                            f"in schedule run_parameters for candidate {candidate_id}. "
                            "Aborting dispatch to avoid sending blank templates."
                        )

                    # ── DEBUG CHECKPOINT 2: resolved identity values ─────
                    logger.debug(
                        "template_vars_resolved",
                        candidate_id=candidate_id,
                        candidate_name=candidate_name,
                        linkedin_url=linkedin_url_resolved,
                    )

                    # ── 5. Calculate total daily limit (all accounts) ──
                    total_limit = sum(
                        _calculate_daily_limit(c) for c in smtp_creds
                    )

                    if total_limit == 0:
                        logger.info(
                            "daily_limit_reached",
                            candidate_id=candidate_id,
                            smtp_account=smtp_account,
                        )
                        if log_id:
                            client.put(
                                f"/orchestrator/logs/{log_id}",
                                json={
                                    "status": "success",
                                    "records_processed": 0,
                                    "error_summary": "Daily limit reached",
                                }
                            )
                        continue

                    # ── 6. Generate snapshot (idempotent) ─────────
                    snap_resp = client.post(
                        f"/campaign-emails/candidates/{candidate_id}"
                        f"/snapshot",
                        params={"workflow_id": wf_id},
                    )
                    if not snap_resp.is_success:
                        logger.warning(
                            "snapshot_failed",
                            candidate_id=candidate_id,
                            status=snap_resp.status_code,
                            body=snap_resp.text,
                        )

                    # ── 7. Dispatch pending (SKIP LOCKED) ──────────
                    dispatch_resp = client.post(
                        f"/campaign-emails/candidates/{candidate_id}"
                        f"/dispatch",
                        params={"limit": total_limit},
                    )
                    dispatch_resp.raise_for_status()
                    dispatch_data = dispatch_resp.json()
                    targets = dispatch_data.get("records", [])

                    if not targets:
                        # No pending emails — campaign complete
                        logger.info(
                            "campaign_complete",
                            candidate_id=candidate_id,
                        )
                        # Auto-stop: turn off the candidate's outreach flag
                        client.post(
                            f"/orchestrator/workflows/{wf_id}"
                            f"/execute-reset-sql",
                            json={
                                "sql_query": (
                                    "UPDATE candidate_marketing "
                                    "SET run_outreach_emails = 0 "
                                    "WHERE candidate_id = :candidate_id"
                                ),
                                "parameters": {
                                    "candidate_id": candidate_id
                                },
                            }
                        )
                        if log_id:
                            client.put(
                                f"/orchestrator/logs/{log_id}",
                                json={
                                    "status": "success",
                                    "records_processed": 0,
                                    "error_summary": "Campaign complete",
                                }
                            )
                        continue

                    # ── 8. Build & enqueue Celery tasks ───────────
                    param_config = (
                        bundle.get("workflow", {})
                        .get("parameters_config") or {}
                    )
                    update_sql = param_config.get("recipient_update_sql")

                    # Pre-filter targets that have a valid email so we know
                    # the exact total BEFORE enqueuing (needed for Redis).
                    valid_targets = [
                        t for t in targets
                        if t.get("vendor_email")
                    ]
                    target_count = len(valid_targets)

                    # Store run metadata in Redis BEFORE dispatching tasks.
                    # This must happen first — if a worker starts before this
                    # call, it would see a key with no 'total' field and
                    # silently skip the report counter.
                    report_stored = store_run_metadata(
                        log_id=log_id,
                        total=target_count,
                        candidate_name=candidate_name,
                        candidate_id=int(candidate_id),
                        smtp_account=smtp_account,
                        pending_remaining=0,  # updated after step 10
                    )

                    for idx, target in enumerate(valid_targets):
                        campaign_email_id = target.get("id")
                        vendor_email = target.get("vendor_email")

                        # Round-robin across SMTP accounts
                        cred = smtp_creds[idx % len(smtp_creds)]

                        payload = {
                            "campaign_email_id": campaign_email_id,
                            "vendor_email": vendor_email,
                            "workflow_id": wf_id,
                            "candidate_id": candidate_id,
                            "log_id": log_id,
                            "recipient_update_sql": update_sql,
                            "credential": cred,
                            "template_subject": template.get(
                                "subject", ""
                            ),
                            "template_body_html": template.get(
                                "content_html", ""
                            ),
                            "variables": {
                                **run_params,
                                # Explicitly inject required template vars so
                                # they are always present even if run_params
                                # omits them (the #1 cause of blank emails).
                                "candidate_name": candidate_name,
                                "linkedin_url": linkedin_url_resolved,
                                "vendor_email": vendor_email,
                                "recipient_email": vendor_email,
                            },
                        }

                        # ── DEBUG CHECKPOINT 3: final payload vars ───────
                        logger.debug(
                            "task_payload_variables",
                            campaign_email_id=campaign_email_id,
                            vendor_email=vendor_email,
                            candidate_name=candidate_name,
                            linkedin_url=linkedin_url_resolved or "<EMPTY>",
                            template_subject_preview=(
                                template.get("subject", "")[:80]
                            ),
                        )

                        delay = random.randint(
                            settings.min_delay_seconds,
                            settings.max_delay_seconds,
                        )
                        send_outreach_email.apply_async(
                            kwargs={"payload": payload},
                            countdown=delay,
                        )
                        stats["emails_enqueued"] += 1

                    # ── 9. Update run log ──────────────────────────
                    if log_id:
                        client.put(
                            f"/orchestrator/logs/{log_id}",
                            json={
                                "status": "success",
                                "records_processed": target_count,
                            }
                        )

                    # ── 10. Count pending remaining & update Redis ──
                    try:
                        count_resp = client.get(
                            f"/campaign-emails/candidates/{candidate_id}"
                            f"/pending-count"
                        )
                        pending_remaining = (
                            count_resp.json().get("count", 0)
                            if count_resp.is_success else 0
                        )
                    except Exception:
                        pending_remaining = 0

                    # Update Redis with the real pending count now that
                    # we have it (was stored as 0 at metadata write time).
                    update_pending_remaining(
                        log_id=log_id,
                        pending_remaining=pending_remaining,
                    )

                    # ── 11. Report status ─────────────────────────────
                    if report_stored:
                        logger.info(
                            "run_report_deferred_to_workers",
                            log_id=log_id,
                            total_enqueued=target_count,
                        )
                    else:
                        # Redis unavailable — send immediate fallback report.
                        # Numbers will say "enqueued" not "delivered" but at
                        # least the admin gets notified the run happened.
                        logger.warning(
                            "redis_unavailable_sending_immediate_report",
                            log_id=log_id,
                        )
                        send_run_report(
                            candidate_name=candidate_name,
                            candidate_id=int(candidate_id),
                            total_dispatched=target_count,
                            success_count=target_count,
                            failed_count=0,
                            bounce_count=0,
                            pending_remaining=pending_remaining,
                            smtp_account=smtp_account,
                        )

                except Exception as e:
                    logger.error(
                        "schedule_execution_error",
                        schedule_id=sched_id,
                        error=str(e),
                        exc_info=True,
                    )
                    stats["errors"] += 1
                    if log_id:
                        client.put(
                            f"/orchestrator/logs/{log_id}",
                            json={
                                "status": "failed",
                                "error_summary": str(e),
                            }
                        )

    except Exception as e:
        logger.error(
            "orchestrator_polling_error",
            error=str(e),
            exc_info=True,
        )
        stats["errors"] += 1

    return stats
