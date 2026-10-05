"""Outbound email. Everything goes to the local Mailpit catcher; nothing leaves the machine."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from pathlib import Path

from app.config import settings


def send_mail(
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    attachments: list[tuple[str, Path]] | None = None,
) -> None:
    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, path in attachments or []:
        if path.is_file():
            msg.add_attachment(path.read_bytes(), maintype="application", subtype="octet-stream", filename=filename)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        smtp.send_message(msg)
