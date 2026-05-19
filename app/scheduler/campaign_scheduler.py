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
from datetime import date

import httpx
from app.core.logging import get_logger
from app.core.config import settings
from app.core.auth import APIAuth
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
    last_reset = cred.get("last_reset_date")
    if last_reset and str(last_reset) != str(date.today()):
        already_sent = 0
    else:
        already_sent = int(cred.get("current_day_sent") or 0)

    remaining = max(0, daily_cap - already_sent)
    return remaining


def _run_local_campaign_schedule(
    client: httpx.Client,
    schedule: Dict[str, Any],
    run_id: str,
    log_id: int,
) -> int:
    """
    Handle schedule using local DuckDB campaign execution.

    Flow:
    1. Initialize LocalCampaignService
    2. create_or_resume_campaign() → get campaign_id
    3. If new campaign:
       - create_default_steps()
       - enroll_recipients()
       - update_remote_run_parameters()
    4. generate_due_attempts() → create pending attempts
    5. Fetch SMTP credentials and calculate daily limit
    6. claim_attempts(limit=daily_limit) → get batch
    7. For each attempt: build payload with mode='local_duckdb_campaign', enqueue Celery task
    8. Store run metadata in Redis (for deferred reporting)
    9. Update run log with records_processed count

    Args:
        client: httpx client
        schedule: Schedule dict from API
        run_id: Unique run ID
        log_id: Run log ID

    Returns:
        Number of emails enqueued
    """
    from app.localdb.duckdb_client import DuckDBClient
    from app.services.local_campaign_service import LocalCampaignService
    from app.workers.email_worker import send_outreach_email

    sched_id = schedule.get("id")
    wf_id = schedule.get("automation_workflow_id")
    run_params = schedule.get("run_parameters") or {}
    candidate_id = run_params.get("candidate_id")

    # Extract required identity vars
    candidate_name = run_params.get("candidate_name")
    linkedin_url = run_params.get("linkedin_url")

    if not candidate_name or not linkedin_url:
        raise ValueError(
            f"Missing required identity variables (candidate_name or linkedin_url) "
            f"in schedule run_parameters for candidate {candidate_id}. "
            "Aborting local campaign dispatch."
        )

    # Initialize DuckDB client and service
    with DuckDBClient() as db_client:
        service = LocalCampaignService(db_client, client)

        # Create or resume campaign
        campaign_id = service.create_or_resume_campaign(
            schedule_id=sched_id,
            candidate_id=candidate_id,
            candidate_name=candidate_name,
            workflow_id=wf_id,
            run_parameters=run_params
        )

        # Check if campaign is new (not in run_parameters)
        is_new_campaign = not run_params.get("local_campaign_id")

        if is_new_campaign:
            # Create default weekly sequence steps
            service.create_default_steps(campaign_id)

            # Enroll recipients from remote API
            service.enroll_recipients(campaign_id, candidate_id)

            # Write local_campaign_id back to remote
            service.update_remote_run_parameters(sched_id, campaign_id)

        # Generate due attempts for recipients with next_send_at <= NOW()
        service.generate_due_attempts(campaign_id)

        # Fetch execution bundle for template and credentials
        bundle_resp = client.get(f"/automation-workflow/{wf_id}/execution-bundle")
        bundle_resp.raise_for_status()
        bundle = bundle_resp.json()

        template = bundle.get("template", {})
        smtp_creds = bundle.get("smtp_credentials") or []

        # Fallback: fetch creds directly by candidate
        if not smtp_creds:
            cred_resp = client.get(f"/orchestrator/candidate-credentials/{candidate_id}")
            if cred_resp.is_success:
                cred_data = cred_resp.json()
                smtp_creds = cred_data if isinstance(cred_data, list) else [cred_data]

        # Filter only healthy & active credentials
        smtp_creds = [
            c for c in smtp_creds
            if c.get("is_active") and c.get("is_healthy", True)
        ]

        if not smtp_creds:
            raise ValueError(f"No active healthy SMTP credentials for candidate {candidate_id}")

        # Show all rotated accounts in the report
        emails = [c.get("email") for c in smtp_creds if c.get("email")]
        smtp_account = "Multiple Accounts" if len(emails) > 1 else (emails[0] if emails else "unknown@smtp")

        # Calculate total daily limit (all accounts)
        total_limit = sum(_calculate_daily_limit(c) for c in smtp_creds)

        if total_limit == 0:
            logger.info(
                "daily_limit_reached",
                campaign_id=campaign_id,
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
            return 0

        # Claim pending attempts atomically
        claimed_attempts = service.claim_attempts(campaign_id, total_limit, "scheduler")

        if not claimed_attempts:
            logger.info(
                "no_pending_attempts",
                campaign_id=campaign_id,
                candidate_id=candidate_id
            )
            if log_id:
                client.put(
                    f"/orchestrator/logs/{log_id}",
                    json={
                        "status": "success",
                        "records_processed": 0,
                        "error_summary": "No pending attempts",
                    }
                )
            return 0

        # Store run metadata in Redis BEFORE dispatching tasks
        target_count = len(claimed_attempts)
        report_stored = store_run_metadata(
            log_id=log_id,
            total=target_count,
            candidate_name=candidate_name,
            candidate_id=int(candidate_id),
            smtp_account=smtp_account,
            pending_remaining=0,
        )

        # Build & enqueue Celery tasks
        for idx, attempt in enumerate(claimed_attempts):
            # Round-robin across SMTP accounts
            cred = smtp_creds[idx % len(smtp_creds)]

            payload = {
                "mode": "local_duckdb_campaign",
                "attempt_id": attempt["attempt_id"],
                "campaign_id": campaign_id,
                "recipient_id": attempt["recipient_id"],
                "step_number": attempt["step_number"],
                "vendor_email": attempt["vendor_email"],
                "workflow_id": wf_id,
                "candidate_id": candidate_id,
                "log_id": log_id,
                "credential": cred,
                "template_subject": template.get("subject", ""),
                "template_body_html": template.get("content_html", ""),
                "variables": {
                    **run_params,
                    "candidate_name": candidate_name,
                    "linkedin_url": linkedin_url,
                    "vendor_email": attempt["vendor_email"],
                    "recipient_email": attempt["vendor_email"],
                },
            }

            delay = random.randint(
                settings.min_delay_seconds,
                settings.max_delay_seconds,
            )
            send_outreach_email.apply_async(
                kwargs={"payload": payload},
                countdown=delay,
            )

        logger.info(
            "local_campaign_tasks_enqueued",
            campaign_id=campaign_id,
            count=target_count,
            candidate_id=candidate_id
        )

        # Update run log
        if log_id:
            client.put(
                f"/orchestrator/logs/{log_id}",
                json={
                    "status": "success",
                    "records_processed": target_count,
                }
            )

        return target_count




def run_scheduler() -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "schedules_processed": 0,
        "emails_enqueued": 0,
        "errors": 0,
    }

    base_url = settings.api_url

    try:
        with httpx.Client(
            base_url=base_url, auth=APIAuth(), timeout=60.0
        ) as client:

            # ── 1. Pull Due Schedules ──────────────────────────────
            due_resp = client.get("/orchestrator/schedules/due")


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

                    # ── FEATURE FLAG: Local DuckDB Campaign ─────────
                    if settings.use_local_duckdb_campaigns:
                        enqueued = _run_local_campaign_schedule(
                            client=client,
                            schedule=schedule,
                            run_id=run_id,
                            log_id=log_id
                        )
                        stats["emails_enqueued"] += enqueued
                        continue  # Skip remote flow

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
                        smtp_account = "Multiple Accounts"
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
