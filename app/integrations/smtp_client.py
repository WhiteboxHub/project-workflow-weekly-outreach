"""
SMTP Client for sending emails via SMTP servers.

Gmail compliance (Feb 2024+ sender requirements):
  - List-Unsubscribe header (mandatory for bulk)
  - Proper From with display name
  - Valid Message-ID
  - MIME-Version + Date headers
  - One connection per send (avoids session reuse issues with Gmail)
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, formataddr, make_msgid
from typing import Optional, Dict, Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Gmail SMTP error codes that mean "stop retrying, you're blocked"
PERMANENT_BLOCK_CODES = {421, 450, 550, 553, 554}
PERMANENT_BLOCK_PHRASES = [
    "message rejected",
    "message blocked",
    "daily user sending limit",
    "too many messages",
    "suspected spam",
    "account has been disabled",
    "rate limit",
]


class SMTPPermanentError(Exception):
    """Raised when Gmail permanently rejects the message (do NOT retry)."""
    pass


class SMTPTransientError(Exception):
    """Raised on temporary failures (safe to retry)."""
    pass


class SMTPClient:
    """Client for sending emails via SMTP with Gmail compliance."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool = True,
    ) -> None:
        """
        Initialize SMTP client.

        Args:
            host: SMTP server host
            port: SMTP server port
            username: SMTP username
            password: SMTP password
            use_tls: Whether to use TLS
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls

    def send_email(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: Optional[str] = None,
        from_email: Optional[str] = None,
        from_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        message_id_header: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        unsubscribe_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an email via SMTP with full Gmail compliance headers.

        Args:
            to_email: Recipient email address
            subject: Email subject
            body_html: HTML body content
            body_text: Plain text body content (optional)
            from_email: Sender email (optional, uses username)
            from_name: Display name for From header (optional)
            reply_to: Reply-To header (optional)
            message_id_header: Message-ID header for tracking
            in_reply_to: In-Reply-To header for threading
            unsubscribe_email: Email for List-Unsubscribe (optional, defaults to from_email)

        Returns:
            Dictionary containing success status and message_id

        Raises:
            SMTPPermanentError: Gmail permanently blocked the message (do not retry)
            SMTPTransientError: Temporary failure (safe to retry)
        """
        sender = from_email or self.username
        domain = sender.split("@")[-1]

        # Generate a proper Message-ID if not provided
        msg_id = message_id_header or make_msgid(domain=domain)

        # Build the unsubscribe target
        unsub_email = unsubscribe_email or sender

        try:
            # ── Build MIME message ────────────────────────────────────
            message = MIMEMultipart("alternative")

            # Required headers
            message["MIME-Version"] = "1.0"
            message["Date"] = formatdate(localtime=True)
            message["Message-ID"] = msg_id

            # From with display name (looks more legitimate to Gmail)
            if from_name:
                message["From"] = formataddr((from_name, sender))
            else:
                message["From"] = sender

            message["To"] = to_email
            message["Subject"] = subject

            # ── Gmail compliance headers (Feb 2024 bulk sender rules) ─
            # List-Unsubscribe is MANDATORY for bulk senders.
            # Without it, Gmail blocks outbound messages proactively.
            message["List-Unsubscribe"] = (
                f"<mailto:{unsub_email}?subject=unsubscribe>"
            )
            message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
            message["Precedence"] = "bulk"

            # Optional threading/reply headers
            if reply_to:
                message["Reply-To"] = reply_to

            if in_reply_to:
                message["In-Reply-To"] = in_reply_to
                message["References"] = in_reply_to

            # ── Attach body parts ────────────────────────────────────
            if body_text:
                message.attach(MIMEText(body_text, "plain", "utf-8"))

            message.attach(MIMEText(body_html, "html", "utf-8"))

            # ── Send via SMTP ────────────────────────────────────────
            if self.use_tls:
                with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(self.username, self.password)
                    server.send_message(message)
            else:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as server:
                    server.ehlo()
                    server.login(self.username, self.password)
                    server.send_message(message)

            logger.info(
                "smtp_email_sent",
                to_email=to_email,
                from_email=sender,
                host=self.host,
            )

            return {
                "success": True,
                "message_id": msg_id,
            }

        except smtplib.SMTPException as e:
            error_str = str(e).lower()
            error_code = getattr(e, "smtp_code", 0) or 0

            is_permanent = (
                error_code in PERMANENT_BLOCK_CODES
                or any(phrase in error_str for phrase in PERMANENT_BLOCK_PHRASES)
            )

            logger.error(
                "smtp_send_error",
                to_email=to_email,
                error=str(e),
                error_type=type(e).__name__,
                smtp_code=error_code,
                permanent=is_permanent,
            )

            if is_permanent:
                raise SMTPPermanentError(
                    f"Gmail permanently blocked: {str(e)}"
                ) from e
            else:
                raise SMTPTransientError(
                    f"SMTP transient error: {str(e)}"
                ) from e

        except Exception as e:
            logger.error(
                "smtp_send_error",
                to_email=to_email,
                error=str(e),
                exc_info=True,
            )
            raise SMTPTransientError(f"Unexpected error: {str(e)}") from e
