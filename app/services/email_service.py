"""
Headless Email Service

This service handles:
- Rendering Jinja2 templates natively parsing the API dictionary.
- Sending via SMTP using raw dictionary arguments.
- Returning message metadata to the worker.
"""

import re
import uuid
from typing import Dict, Any, Optional

from jinja2 import Template, TemplateError

from app.core.logging import get_logger
from app.integrations.smtp_client import SMTPClient

logger = get_logger(__name__)


class EmailService:
    """
    Coordinates email rendering and SMTP delivery.
    Operates completely completely independent of databases, parsing dicts physically.
    """

    def send_outreach(
        self,
        smtp_host: str,
        from_email: str,
        password: str,
        to_email: str,
        template_subject: str,
        template_body_html: str,
        variables: Dict[str, Any],
        from_name: Optional[str] = None,
        in_reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Render the email template and send via SMTP.
        """
        # ── Render templates ─────────────────────────────────────────
        subject = self._render(template_subject, variables)
        body_html = self._render(template_body_html, variables)
        body_text = self._html_to_text(body_html)

        # ── Generate unique Message-ID header ────────────────────────
        domain = from_email.split("@")[-1]
        message_id = f"<{uuid.uuid4()}@{domain}>"

        # ── Send via SMTP ────────────────────────────────────────────
        smtp = SMTPClient(
            host=smtp_host,
            port=587,
            username=from_email,
            password=password,
            use_tls=True,
        )
        result = smtp.send_email(
            to_email=to_email,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            from_email=from_email,
            from_name=from_name,
            message_id_header=message_id,
            in_reply_to=in_reply_to,
        )

        logger.info(
            "email_sent",
            from_email=from_email,
            to_email=to_email,
            subject=subject[:60],
        )

        return {
            "message_id": result.get("message_id", message_id),
            "from_email": from_email,
        }

    def _render(self, template_str: str, variables: Dict[str, Any]) -> str:
        """Render a Jinja2 template string."""
        try:
            return Template(template_str).render(**variables)
        except TemplateError as e:
            logger.warning(
                "template_render_warning",
                error=str(e),
                template_preview=template_str[:100],
            )
            return template_str

    def _html_to_text(self, html: str) -> str:
        """Convert HTML to plain text."""
        text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
