"""app/notify.py

User notifications for automation events. Delivery channels are opt-in per
user (NOTIFY_WEBHOOK_URL / NOTIFY_EMAIL) and every notification is recorded
in the per-user ActionLog, sent or not.

Events (configured via NOTIFY_EVENTS, comma-separated):
* stopped_unexpectedly        - automation had to stop itself
* repeated_provider_failure   - provider failing cycle after cycle
* soc_unreadable              - battery SoC could not be read

Delivery failures never raise into the worker loop: they are logged as
ActionLog error rows instead.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy.orm import Session

from app import state
from app.models import ActionLog, User

EVENT_STOPPED = "stopped_unexpectedly"
EVENT_REPEATED_FAILURE = "repeated_provider_failure"
EVENT_SOC_UNREADABLE = "soc_unreadable"
KNOWN_EVENTS = (EVENT_STOPPED, EVENT_REPEATED_FAILURE, EVENT_SOC_UNREADABLE)

_TIMEOUT_SECONDS = 10.0


async def _deliver_webhook(url: str, title: str, message: str, event: str) -> None:
    """POST the notification to a webhook or ntfy-style topic URL.

    URLs containing 'ntfy' get ntfy formatting (plain body + Title header);
    anything else receives a JSON payload.
    """
    if "ntfy" in url.lower():
        response = await httpx.AsyncClient(timeout=_TIMEOUT_SECONDS).post(
            url,
            content=message.encode("utf-8"),
            headers={"Title": title, "Tags": "solar"},
        )
    else:
        response = await httpx.AsyncClient(timeout=_TIMEOUT_SECONDS).post(
            url,
            json={"title": title, "message": message, "event": event,
                  "ts": dt.datetime.now(dt.timezone.utc).isoformat()},
            headers={"Title": title},
        )
    response.raise_for_status()


async def _deliver_email(to_addr: str, title: str, message: str) -> None:
    """Send one notification email via the instance SMTP settings."""
    import asyncio
    import smtplib
    from email.message import EmailMessage

    from app.config import get_settings

    cfg = get_settings()
    if not cfg.SMTP_HOST:
        raise RuntimeError("SMTP is not configured on this instance (SMTP_HOST missing).")
    email = EmailMessage()
    email["Subject"] = title
    email["From"] = cfg.SMTP_FROM or cfg.SMTP_USER or "notifications@localhost"
    email["To"] = to_addr
    email.set_content(message)

    def _send() -> None:
        with smtplib.SMTP(cfg.SMTP_HOST, int(cfg.SMTP_PORT), timeout=_TIMEOUT_SECONDS) as smtp:
            smtp.starttls()
            if cfg.SMTP_USER and cfg.SMTP_PASSWORD:
                smtp.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
            smtp.send_message(email)

    await asyncio.to_thread(_send)


def _log(session: Session, user_id: int, kind: str, message: str,
         rule: str | None = None, detail: dict[str, Any] | None = None) -> None:
    session.add(
        ActionLog(
            user_id=user_id,
            kind=kind,
            rule=rule,
            dry_run=False,
            message=message,
            detail_json=json.dumps(detail) if detail else None,
        )
    )
    session.commit()


async def emit(
    session: Session,
    user: User,
    event: str,
    title: str,
    message: str,
    *,
    webhook: Callable[[str, str, str, str], Awaitable[None]] | None = None,
    email: Callable[[str, str, str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Send one notification to the user's configured channels.

    Returns a summary dict {event, delivered, skipped, errors}; always logs
    an ActionLog 'notify' (or 'error') row. Never raises.
    """
    params = state.resolve_user_params(session, user.id)
    wanted = [e.strip() for e in str(params.get("NOTIFY_EVENTS") or "").split(",") if e.strip()]
    summary: dict[str, Any] = {"event": event, "delivered": [], "skipped": [], "errors": []}

    if event not in wanted:
        summary["skipped"].append(f"event '{event}' is not in NOTIFY_EVENTS")
        _log(session, user.id, "info",
             f"Notification '{event}' not sent: event is not in NOTIFY_EVENTS.",
             detail={"event": event})
        return summary

    webhook_url = str(params.get("NOTIFY_WEBHOOK_URL") or "").strip()
    email_addr = str(params.get("NOTIFY_EMAIL") or "").strip()
    deliver_webhook = webhook or _deliver_webhook
    deliver_email = email or _deliver_email

    if webhook_url:
        try:
            await deliver_webhook(webhook_url, title, message, event)
            summary["delivered"].append("webhook")
        except Exception as exc:  # noqa: BLE001 - delivery must never raise
            summary["errors"].append(f"webhook: {exc}")
    else:
        summary["skipped"].append("no NOTIFY_WEBHOOK_URL configured")

    if email_addr:
        try:
            await deliver_email(email_addr, title, message)
            summary["delivered"].append("email")
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"email: {exc}")
    else:
        summary["skipped"].append("no NOTIFY_EMAIL configured")

    if summary["errors"]:
        _log(session, user.id, "error",
             f"Notification '{event}' failed to send: " + "; ".join(summary["errors"]),
             detail=summary)
    else:
        _log(session, user.id, "notify",
             f"Notification '{event}' sent ({', '.join(summary['delivered']) or 'no channels'}): {title}",
             detail=summary)
    return summary


__all__ = ["emit", "KNOWN_EVENTS", "EVENT_STOPPED", "EVENT_REPEATED_FAILURE",
           "EVENT_SOC_UNREADABLE"]
