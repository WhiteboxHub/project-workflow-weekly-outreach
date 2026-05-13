"""
Celery application configuration.
"""

from celery import Celery
from app.core.config import settings
from app.core.logging import configure_logging

# Configure logging
configure_logging()

# Create Celery app
celery_app = Celery(
    "email_outreach",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.email_worker"],
)

# Configure Celery
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,  # Acknowledge task after completion
    worker_prefetch_multiplier=1,  # Disable prefetching for better task distribution
    task_reject_on_worker_lost=True,
    task_soft_time_limit=300,  # 5 minutes soft limit
    task_time_limit=360,  # 6 minutes hard limit
)
