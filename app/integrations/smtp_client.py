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

# ---------------------------------------------------------------------------
# Gmail SMTP error taxonomy
# ---------------------------------------------------------------------------

# ── SMTP Error code taxonomy ────────────────────────────────────────────────

# Codes that Gmail uses for SENDER quota / rate-limit (not the recipient's fault)
# 421 = Service temporarily unavailable
# 450 = Requested action not taken (often rate limit)
# 550 = Can be EITHER rate-limit OR hard-bounce — must inspect the phrase too
RATE_LIMIT_CODES = {421, 450}

# Phrases that confirm this is a SENDER quota / provider rate-limit error.
# These take priority over HARD_BOUNCE_PHRASES when the code alone is ambiguous.
RATE_LIMIT_PHRASES = [
    "daily user sending limit",
    "daily sending limit",
    "too many messages",
    "rate limit",
    "quota exceeded",
    "exceeded sending limit",
    "sending limit exceeded",
    "too many recipients",
    "4.7.28",   # Gmail DMARC rate-limit code
    "4.7.29",   # Gmail DKIM rate-limit code
    "5.4.5",    # Gmail daily limit SMTP status
]

# Codes that mean a RECIPIENT-level permanent block (wrong address, domain gone)
PERMANENT_BLOCK_CODES = {550, 551, 553, 554}

# Phrases that confirm a RECIPIENT hard-bounce (address does not exist).
# Only used when the error is NOT a rate-limit first.
HARD_BOUNCE_PHRASES = [
    "user does not exist",
    "does not exist",
    "no such user",
    "user unknown",
    "unknown user",
    "mailbox not found",
    "mailbox unavailable",
    "invalid address",
    "address rejected",
    "recipient address rejected",
    "bad destination",
    "message rejected",
    "message blocked",
    "account has been disabled",
    "account disabled",
    "suspected spam",
]

# Codes that mean a CREDENTIAL-level auth failure (wrong/missing App Password)
# 534 = Application-specific password required (needs App Password enabled)
# 535 = Username and password not accepted (wrong App Password)
# These are NEVER transient — retrying 3x wastes 15 minutes with zero chance
# of success until the credential is corrected in the admin panel.
SMTP_AUTH_CODES = {534, 535}

SMTP_AUTH_PHRASES = [
    "application-specific password required",
    "application specific password",
    "web login required",
    "username and password not accepted",
    "invalid credentials",
    "authentication failed",
    "authentication unsuccessful",
    "please log in via your web browser",  # Gmail 534 message body
    "534-5.7.9",                           # Gmail ESMTP prefix for App PW errors
    "535-5.7.8",                           # Gmail ESMTP prefix for wrong password
]


class SMTPPermanentError(Exception):
    """Raised when the message is PERMANENTLY rejected at the RECIPIENT level.

    This means the address is bad (does not exist, mailbox not found, etc.).
    Do NOT retry — mark the recipient as hard_bounce and stop sending to them.
    """
    pass


class SMTPRateLimitError(Exception):
    """Raised when the SENDING ACCOUNT hits a provider quota / rate-limit.

    This is a TRANSPORT error, NOT a recipient error. The recipient is fine.
    The sending credential should be marked as unhealthy (is_healthy=False)
    and the recipient left in 'deferred' state to be retried tomorrow.

    Examples:
      - Gmail 5.4.5: Daily user sending limit exceeded
      - Gmail 4.7.28: Rate limit
      - Any 421/450 transient overload
    """
    pass


class SMTPAuthError(Exception):
    """Raised when Gmail rejects the CREDENTIAL (wrong/missing App Password).

    This is distinct from SMTPPermanentError (recipient rejection) and
    SMTPTransientError (network/mailbox-full).  Auth errors MUST NOT be
    retried — the credential is broken until an admin fixes it.

    Callers should:
      1. Mark the attempt as 'credential_auth_failure' (not 'soft' bounce)
      2. Stop dispatching further emails for this run
      3. Alert the admin to re-generate or enable the App Password
    """
    pass


class SMTPTransientError(Exception):
    """Raised on temporary failures (mailbox full, server busy). Safe to retry."""
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

        except smtplib.SMTPAuthenticationError as e:
            # ── O2 fix: auth failures are NEVER transient ────────────────
            # Gmail 534: App Password required (2FA enabled but no App PW)
            # Gmail 535: Wrong App Password
            # Both are permanent credential errors — retrying wastes time.
            error_str = str(e).lower()
            auth_code = getattr(e, "smtp_code", 0) or 0

            logger.error(
                "smtp_auth_failure",
                to_email=to_email,
                from_email=sender,
                host=self.host,
                smtp_code=auth_code,
                error=str(e),
            )
            raise SMTPAuthError(
                f"SMTP authentication failed (code {auth_code}): {str(e)}"
            ) from e

        except smtplib.SMTPException as e:
            error_str = str(e).lower()
            error_code = getattr(e, "smtp_code", 0) or 0

            # ── Priority 1: Auth failures ─────────────────────────────────────
            # Check auth FIRST — some auth errors arrive as generic SMTPException
            is_auth = (
                error_code in SMTP_AUTH_CODES
                or any(phrase in error_str for phrase in SMTP_AUTH_PHRASES)
            )
            if is_auth:
                logger.error(
                    "smtp_auth_failure_via_phrase",
                    to_email=to_email,
                    from_email=sender,
                    host=self.host,
                    smtp_code=error_code,
                    error=str(e),
                )
                raise SMTPAuthError(
                    f"SMTP authentication failed (code {error_code}): {str(e)}"
                ) from e

            # ── Priority 2: Rate-limit / quota errors (SENDER transport failure)
            # MUST be checked before hard-bounce phrases because Gmail's 550
            # rate-limit message contains "5.4.5" but NOT hard-bounce phrases.
            is_rate_limit = (
                error_code in RATE_LIMIT_CODES
                or any(phrase in error_str for phrase in RATE_LIMIT_PHRASES)
            )
            if is_rate_limit:
                logger.warning(
                    "smtp_rate_limit_hit",
                    to_email=to_email,
                    from_email=sender,
                    host=self.host,
                    smtp_code=error_code,
                    error=str(e),
                )
                raise SMTPRateLimitError(
                    f"Provider rate limit exceeded (code {error_code}): {str(e)}"
                ) from e

            # ── Priority 3: Hard bounce (RECIPIENT permanent failure) ──────────
            is_hard_bounce = (
                error_code in PERMANENT_BLOCK_CODES
                and any(phrase in error_str for phrase in HARD_BOUNCE_PHRASES)
            )
            # Fallback: 55x with no matching phrase — assume hard bounce
            is_permanent = is_hard_bounce or error_code in PERMANENT_BLOCK_CODES

            logger.error(
                "smtp_send_error",
                to_email=to_email,
                error=str(e),
                error_type=type(e).__name__,
                smtp_code=error_code,
                is_hard_bounce=is_hard_bounce,
                is_permanent=is_permanent,
            )

            if is_permanent:
                raise SMTPPermanentError(
                    f"Recipient permanently rejected (code {error_code}): {str(e)}"
                ) from e
            else:
                raise SMTPTransientError(
                    f"SMTP transient error (code {error_code}): {str(e)}"
                ) from e

        except Exception as e:
            logger.error(
                "smtp_unexpected_error",
                to_email=to_email,
                error=str(e),
                exc_info=True,
            )
            raise SMTPTransientError(f"Unexpected SMTP error: {str(e)}") from e
