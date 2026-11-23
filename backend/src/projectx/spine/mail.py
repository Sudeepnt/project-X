"""Outgoing email over SMTP (works with Mailpit locally and with SES, Postmark, Resend… in production).

Product email (verification, resets, founder briefings) is rendered from templates and sent
from a background job so a slow mail server never slows a request down.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path
from typing import Any

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from projectx.settings import Settings

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape(["html"]), enable_async=False)


def render(template: str, **context: Any) -> str:
    return _env.get_template(template).render(**context)


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    async def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        from_addr: str | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        message = EmailMessage()
        sender_name, sender_email = parseaddr(from_addr or self._s.email_from)
        message["From"] = formataddr((sender_name, sender_email))
        message["To"] = to
        message["Subject"] = subject
        domain = sender_email.split("@")[-1] or "projectx.local"
        message_id = make_msgid(domain=domain)
        message["Message-ID"] = message_id
        if reply_to:
            message["Reply-To"] = reply_to
        for key, value in (headers or {}).items():
            message[key] = value
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        await aiosmtplib.send(
            message,
            hostname=self._s.smtp_host,
            port=self._s.smtp_port,
            username=self._s.smtp_username,
            password=self._s.smtp_password.get_secret_value() if self._s.smtp_password else None,
            start_tls=self._s.smtp_starttls,
            use_tls=self._s.smtp_use_tls,
            timeout=30,
        )
        return message_id
