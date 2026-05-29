"""
Redis client for deferred accurate run reporting.

Design:
  - Scheduler stores run metadata (total expected tasks) BEFORE enqueuing.
  - Each Celery worker atomically increments sent/failed on task completion.
  - When sent + failed == total, the first worker to notice claims a report
    slot (SET NX) and sends the real HTML report with accurate numbers.
"""

import redis
from typing import Optional
from app.core.config import settings

# 48-hour TTL — plenty of time for any batch to complete, then auto-cleans up
RUN_KEY_TTL = 60 * 60 * 48


def get_redis() -> redis.Redis:
    """Return a Redis client from the configured URL."""
    return redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )


def store_run_metadata(
    log_id: int,
    total: int,
    candidate_name: str,
    candidate_id: int,
    smtp_account: str,
    pending_remaining: int = 0,
) -> bool:
    """
    Store run metadata in Redis BEFORE tasks are enqueued.

    Must be called before any worker tasks are dispatched to avoid a race
    where a fast worker increments a counter before the key exists.

    Returns True on success, False if Redis is unavailable.
    """
    # Explicit None check: log_id=0 is falsy but technically valid
    if log_id is None or total == 0:
        return False
    try:
        r = get_redis()
        key = f"outreach_run:{log_id}"
        r.hset(key, mapping={
            "total":             str(total),
            "sent":              "0",
            "failed":            "0",
            "candidate_name":    str(candidate_name),
            "candidate_id":      str(candidate_id),
            "smtp_account":      str(smtp_account),
            "pending_remaining": str(pending_remaining),
        })
        r.expire(key, RUN_KEY_TTL)
        return True
    except Exception:
        return False


def update_pending_remaining(log_id: int, pending_remaining: int) -> None:
    """
    Update just the pending_remaining field after it has been computed.
    Called after store_run_metadata once the API count is known.
    Silently ignores all errors.
    """
    if log_id is None:
        return
    try:
        r = get_redis()
        key = f"outreach_run:{log_id}"
        r.hset(key, "pending_remaining", str(pending_remaining))
    except Exception:
        pass


def record_task_outcome(
    log_id: int,
    success: bool,
) -> Optional[dict]:
    """
    Atomically increment sent/failed counter for a run.

    Returns a dict: { total, sent, failed, done (bool), meta (dict) }
    Returns None if Redis is unavailable, the key has expired, or the
    'total' field is missing (metadata not yet stored — key created by
    a racing worker before the scheduler called store_run_metadata).
    """
    if log_id is None:
        return None
    try:
        r = get_redis()
        key = f"outreach_run:{log_id}"

        # Atomically increment the right counter (Redis HINCRBY is atomic)
        field = "sent" if success else "failed"
        r.hincrby(key, field, 1)

        # Read current state
        data = r.hgetall(key)

        # Guard: if key doesn't exist OR 'total' was never stored (race where
        # worker started before store_run_metadata was called), bail out.
        if not data or "total" not in data:
            return None

        total  = int(data["total"])
        sent   = int(data.get("sent",   "0"))
        failed = int(data.get("failed", "0"))
        done   = (sent + failed) >= total and total > 0

        return {
            "total":  total,
            "sent":   sent,
            "failed": failed,
            "done":   done,
            "meta":   data,
        }
    except Exception:
        return None


def claim_report_slot(log_id: int) -> bool:
    """
    Atomically claim the right to send the final report.

    Uses SET NX so only the FIRST caller returns True, preventing duplicate
    reports when two workers finish at exactly the same millisecond.

    Returns True if this caller should send the report, False otherwise.
    Falls back to True on Redis error (report may duplicate, but won't drop).
    """
    if log_id is None:
        return False
    try:
        r = get_redis()
        slot_key = f"outreach_run:{log_id}:report_sent"
        claimed = r.set(slot_key, "1", nx=True, ex=RUN_KEY_TTL)
        return bool(claimed)
    except Exception:
        return True  # Redis down: allow the report rather than silently drop it


def mark_credential_rate_limited_redis(credential_id: int) -> None:
    """
    Instantly flags a credential as rate-limited in Redis (24-hour TTL).
    Allows Celery workers to fast-fail the remaining 800 tasks in the queue
    without needing to make 800 HTTP GET requests to the backend API.
    """
    if not credential_id:
        return
    try:
        r = get_redis()
        # 24 hours (86400 seconds) matches Gmail's typical quota reset window
        r.set(f"credential_rate_limited:{credential_id}", "1", ex=86400)
    except Exception:
        pass


def is_credential_rate_limited_redis(credential_id: int) -> bool:
    """Returns True if the credential was recently rate-limited."""
    if not credential_id:
        return False
    try:
        r = get_redis()
        return bool(r.get(f"credential_rate_limited:{credential_id}"))
    except Exception:
        return False

