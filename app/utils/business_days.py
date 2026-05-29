"""
Business Day Helper Module

Provides functions for calculating next send times with business day logic,
time window enforcement, and randomized jitter for email sending.
"""

import random
from datetime import datetime, timedelta, timezone
from typing import Optional


def is_business_day(dt: datetime) -> bool:
    """
    Check if date is a business day (Monday-Friday).

    Args:
        dt: Datetime to check

    Returns:
        True if Monday-Friday, False if weekend
    """
    return dt.weekday() < 5


def add_business_days(start: datetime, days: int) -> datetime:
    """
    Add N business days to a datetime (skip weekends).

    Args:
        start: Starting datetime
        days: Number of business days to add

    Returns:
        Datetime N business days in the future
    """
    if days == 0:
        return start

    current = start
    remaining = days

    while remaining > 0:
        current += timedelta(days=1)
        if is_business_day(current):
            remaining -= 1

    return current


def next_send_window_datetime(
    from_dt: datetime,
    start_hour: int = 9,
    end_hour: int = 17
) -> datetime:
    """
    Clamp datetime to business hours send window (default 9 AM - 5 PM).

    Rules:
    - If before start_hour: return start_hour same day
    - If after end_hour: return start_hour next business day
    - If during window: return as-is
    - If weekend: return start_hour next Monday

    Args:
        from_dt: Input datetime
        start_hour: Start of send window (default 9 AM)
        end_hour: End of send window (default 5 PM)

    Returns:
        Datetime within business hours send window
    """
    # If weekend, move to next Monday
    if not is_business_day(from_dt):
        next_monday = add_business_days(from_dt, 1)
        return next_monday.replace(hour=start_hour, minute=0, second=0, microsecond=0)

    hour = from_dt.hour

    # Before send window: move to start of window today
    if hour < start_hour:
        return from_dt.replace(hour=start_hour, minute=0, second=0, microsecond=0)

    # After send window: move to start of window next business day
    if hour >= end_hour:
        next_day = add_business_days(from_dt, 1)
        return next_day.replace(hour=start_hour, minute=0, second=0, microsecond=0)

    # Within window: return as-is
    return from_dt


def apply_jitter(
    dt: datetime,
    min_seconds: int = 30,
    max_seconds: int = 120
) -> datetime:
    """
    Add random jitter to avoid spam detection patterns.

    Args:
        dt: Base datetime
        min_seconds: Minimum jitter in seconds (default 30)
        max_seconds: Maximum jitter in seconds (default 120)

    Returns:
        Datetime with random jitter applied
    """
    jitter = random.randint(min_seconds, max_seconds)
    return dt + timedelta(seconds=jitter)


def calculate_next_send_at(
    from_dt: datetime,
    delay_days: int,
    start_hour: int = 9,
    end_hour: int = 17,
    min_jitter: int = 30,
    max_jitter: int = 120
) -> datetime:
    """
    Calculate next send time with business days, time window, and jitter.

    USE THIS FOR STEPS 2–4 (follow-ups) only.
    Steps 2–4 must land on weekdays inside the 9 AM–5 PM send window.

    For Step 1 (initial outreach) use calculate_immediate_send_at() instead —
    Step 1 must be sent immediately regardless of day or time.

    Args:
        from_dt: Starting datetime
        delay_days: Number of business days to wait
        start_hour: Send window start (default 9 AM)
        end_hour: Send window end (default 5 PM)
        min_jitter: Minimum jitter seconds (default 30)
        max_jitter: Maximum jitter seconds (default 120)

    Returns:
        Next send datetime with all logic applied
    """
    # Add business days
    next_dt = add_business_days(from_dt, delay_days)

    # Clamp to send window
    next_dt = next_send_window_datetime(next_dt, start_hour, end_hour)

    # Apply jitter
    next_dt = apply_jitter(next_dt, min_jitter, max_jitter)

    return next_dt


def calculate_immediate_send_at(
    from_dt: datetime,
    min_jitter: int = 30,
    max_jitter: int = 120
) -> datetime:
    """
    Calculate an IMMEDIATE send time — jitter only, NO business-day or
    send-window enforcement.

    USE THIS FOR STEP 1 (initial outreach) ONLY.

    Step 1 must go out right away regardless of whether it is a weekend,
    a public holiday, or outside the 9 AM–5 PM window.  Applying
    business-day logic here would silently defer all Saturday/Sunday
    enrollments to Monday 9 AM, causing emails_enqueued = 0 for the
    entire weekend (Bug O1).

    The small jitter (30–120 s) is kept to prevent a thundering-herd of
    simultaneous SMTP connections when thousands of recipients are
    enrolled at once.

    Args:
        from_dt:    Reference datetime (normally datetime.now(timezone.utc))
        min_jitter: Minimum jitter in seconds (default 30)
        max_jitter: Maximum jitter in seconds (default 120)

    Returns:
        from_dt + random jitter — always in the near future, never
        deferred to the next business day.
    """
    return apply_jitter(from_dt, min_jitter, max_jitter)
