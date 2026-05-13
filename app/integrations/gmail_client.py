"""
Gmail API Client for sending emails via Gmail.
"""

from typing import Optional, Dict, Any
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class GmailClient:
    """Client for sending emails via Gmail API."""

    def __init__(self, refresh_token: str) -> None:
        """
        Initialize Gmail client with refresh token.

        Args:
            refresh_token: OAuth2 refresh token for Gmail API
        """
        self.refresh_token = refresh_token
        self.service = None

    def _get_service(self) -> Any:
        """Get or create Gmail API service."""
        if self.service:
            return self.service

        try:
            # Create credentials from refresh token
            creds = Credentials(
                token=None,
                refresh_token=self.refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
            )

            # Refresh the token if needed
            if not creds.valid:
                creds.refresh(Request())

            # Build the Gmail service
            self.service = build("gmail", "v1", credentials=creds)
            return self.service

        except Exception as e:
            logger.error("gmail_service_creation_error", error=str(e), exc_info=True)
            raise

    def send_email(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: Optional[str] = None,
        from_email: Optional[str] = None,
        thread_id: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an email via Gmail API.

        Args:
            to_email: Recipient email address
            subject: Email subject
            body_html: HTML body content
            body_text: Plain text body content (optional)
            from_email: Sender email (optional, uses authenticated account)
            thread_id: Gmail thread ID for reply (optional)
            reply_to: Reply-To header (optional)

        Returns:
            Dictionary containing message_id and thread_id

        Raises:
            Exception: If email sending fails
        """
        try:
            service = self._get_service()

            # Create message
            message = MIMEMultipart("alternative")
            message["To"] = to_email
            message["Subject"] = subject

            if from_email:
                message["From"] = from_email

            if reply_to:
                message["Reply-To"] = reply_to

            # Add plain text and HTML parts
            if body_text:
                part1 = MIMEText(body_text, "plain")
                message.attach(part1)

            part2 = MIMEText(body_html, "html")
            message.attach(part2)

            # For replies, add necessary headers
            if thread_id:
                message["In-Reply-To"] = thread_id
                message["References"] = thread_id

            # Encode message
            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

            # Create send request body
            send_body = {"raw": raw_message}

            # If replying, include thread ID
            if thread_id:
                send_body["threadId"] = thread_id

            # Send the message
            result = service.users().messages().send(userId="me", body=send_body).execute()

            logger.info(
                "gmail_email_sent",
                to_email=to_email,
                message_id=result.get("id"),
                thread_id=result.get("threadId"),
            )

            return {
                "message_id": result.get("id"),
                "thread_id": result.get("threadId"),
                "success": True,
            }

        except HttpError as e:
            logger.error(
                "gmail_send_error",
                to_email=to_email,
                error=str(e),
                error_details=e.error_details if hasattr(e, "error_details") else None,
                exc_info=True,
            )
            raise Exception(f"Gmail API error: {str(e)}")

        except Exception as e:
            logger.error("gmail_send_error", to_email=to_email, error=str(e), exc_info=True)
            raise

    def get_thread_messages(self, thread_id: str) -> list:
        """
        Get all messages in a thread (for reply detection).

        Args:
            thread_id: Gmail thread ID

        Returns:
            List of messages in the thread
        """
        try:
            service = self._get_service()
            thread = service.users().threads().get(userId="me", id=thread_id).execute()
            return thread.get("messages", [])

        except Exception as e:
            logger.error(
                "gmail_get_thread_error",
                thread_id=thread_id,
                error=str(e),
                exc_info=True,
            )
            return []

    def check_for_replies(self, thread_id: str, original_message_id: str) -> bool:
        """
        Check if there are any replies in the thread after our message.

        Args:
            thread_id: Gmail thread ID
            original_message_id: Our original message ID

        Returns:
            True if there are replies, False otherwise
        """
        try:
            messages = self.get_thread_messages(thread_id)

            # Find our message index
            our_message_index = -1
            for i, msg in enumerate(messages):
                if msg.get("id") == original_message_id:
                    our_message_index = i
                    break

            # Check if there are messages after ours
            if our_message_index >= 0 and our_message_index < len(messages) - 1:
                return True

            return False

        except Exception as e:
            logger.error(
                "gmail_check_replies_error",
                thread_id=thread_id,
                error=str(e),
                exc_info=True,
            )
            return False
