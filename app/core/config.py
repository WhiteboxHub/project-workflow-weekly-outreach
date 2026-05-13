from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # Application
    app_name: str = "email-outreach-service"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_workers: int = 4

    # API Connection to Main Backend Server
    main_api_base_url: str = "http://localhost:8000"
    api_bearer_token: Optional[str] = None
    # Optional: set these so the scheduler can auto-refresh the token on 401
    api_login_email: Optional[str] = None
    api_login_password: Optional[str] = None
    api_login_path: str = "/auth/login"  # POST endpoint that returns access_token

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"

    # Gmail API (optional – not required for SMTP-only outreach)
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None
    google_redirect_uri: Optional[str] = None

    # Rate Limiting (fallback defaults – per-account limits are read from DB)
    min_delay_seconds: int = 30
    max_delay_seconds: int = 120

    # Scheduler
    scheduler_interval_seconds: int = 60
    scheduler_batch_size: int = 100  # Max vendors per candidate per run

    # Monitoring
    sentry_dsn: Optional[str] = None

    # Reporting (separate SMTP account for sending run reports)
    report_smtp_host: str = "smtp.gmail.com"
    report_smtp_port: int = 587
    report_from_email: Optional[str] = None      # e.g. reports@gmail.com
    report_from_password: Optional[str] = None   # App password
    report_recipient_email: Optional[str] = None  # Admin inbox

    @property
    def api_url(self) -> str:
        """Get the base URL properly stripped of trailing slashes."""
        return str(self.main_api_base_url).rstrip("/")

    @property
    def redis_url_str(self) -> str:
        """Get Redis URL as string."""
        return str(self.redis_url)

settings = Settings()

