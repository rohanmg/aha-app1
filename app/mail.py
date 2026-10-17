"""app/mail.py

Outbound email for account recovery (password-reset passcodes).

Priority:
1. Resend REST API when ``RESEND_API_KEY`` is set (simplest hosted option;
   without a verified domain Resend delivers to the account owner's own
   address, which is fine for self-delivered passcodes).
2. SMTP via ``SMTP_HOST``/``SMTP_USER``/``SMTP_PASSWORD`` when configured.
3. Otherwise MailNotConfigured — callers render a friendly setup hint instead
   of failing silently.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

import httpx

from app.config import get_settings


class MailNotConfigured(RuntimeError):
    """No email transport is configured on this instance."""


class MailSendError(RuntimeError):
    """The configured transport rejected the message."""


def _send_via_resend(to: str, subject: str, text: str) -> None:
    settings = get_settings()
    api_key = settings.RESEND_API_KEY
    assert api_key, "RESEND_API_KEY not set"
    from_email = settings.MAIL_FROM or "onboarding@resend.dev"
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_email,
                "to": [to],
                "subject": subject,
                "text": text,
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise MailSendError(f"Resend request failed: {exc}") from exc
    if resp.status_code not in (200, 201, 202):
        raise MailSendError(f"Resend rejected the message: HTTP {resp.status_code}")


def _send_via_smtp(to: str, subject: str, text: str) -> None:
    settings = get_settings()
    host = settings.SMTP_HOST
    user = settings.SMTP_USER
    password = settings.SMTP_PASSWORD
    assert host and user and password, "SMTP not fully configured"
    from_email = settings.SMTP_FROM or user
    msg = EmailMessage()
    msg["From"] = from_email or user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    try:
        with smtplib.SMTP(host, int(settings.SMTP_PORT or 587), timeout=20) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailSendError(f"SMTP send failed: {exc}") from exc


def send_email(to: str, subject: str, text: str) -> None:
    """Deliver one email via the first configured transport."""
    settings = get_settings()
    if settings.RESEND_API_KEY:
        _send_via_resend(to, subject, text)
        return
    if settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD:
        _send_via_smtp(to, subject, text)
        return
    raise MailNotConfigured(
        "Set RESEND_API_KEY (or SMTP_HOST / SMTP_USER / SMTP_PASSWORD) on this "
        "instance so recovery emails can be delivered."
    )
