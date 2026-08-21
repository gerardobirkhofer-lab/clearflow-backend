"""
Async email service using aiosmtplib.
"""
from __future__ import annotations
import os
from typing import Optional

from aiosmtplib import SMTP
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


class EmailService:
    """Async email sender using SMTP."""

    def __init__(
        self,
        hostname: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_tls: bool = True,
        sender: Optional[str] = None,
    ):
        self.hostname = hostname or os.getenv("SMTP_HOST", "")
        self.port = port or int(os.getenv("SMTP_PORT", "587"))
        self.username = username or os.getenv("SMTP_USER", "")
        self.password = password or os.getenv("SMTP_PASS", "")
        self.use_tls = use_tls
        self.sender = sender or os.getenv("SMTP_SENDER", "noreply@clearview.app")

    async def send_email(
        self,
        to: str,
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
    ) -> dict:
        """Send an email asynchronously."""
        if not self.hostname:
            return {"success": False, "error": "SMTP not configured"}

        message = MIMEMultipart("alternative")
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject

        message.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            message.attach(MIMEText(body_html, "html", "utf-8"))

        try:
            async with SMTP(
                hostname=self.hostname,
                port=self.port,
                use_tls=self.use_tls,
            ) as client:
                if self.username and self.password:
                    await client.login(self.username, self.password)
                await client.send_message(message)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}


# Global singleton
_email_service: Optional[EmailService] = None


def get_email_service() -> EmailService:
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service
