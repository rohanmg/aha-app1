"""app/security.py

CSRF protection for every state-changing request.

Design: a cryptographically random token per session, kept in a separate
cookie (readable by the app, not by JS on foreign origins) and required to
match on every POST/submitted request via hidden form field or X-CSRF-Token
header (needed for HTMX). SameSite=Lax on the session cookie plus the
strict origin check means a foreign site cannot force a valid token pair.
"""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

CSRF_COOKIE = "aha_csrf"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def issue_csrf_token(request: Request) -> str:
    """Return the token for this browser, creating one if needed.

    Stored on request.state so templates can embed it in forms; the visible
    copy lives in a cookie set by the middleware below.
    """
    token = getattr(request.state, "csrf_token", None)
    if not token:
        token = request.cookies.get(CSRF_COOKIE) or ""
        if not _valid_format(token):
            token = secrets.token_urlsafe(32)
        request.state.csrf_token = token
    return token


def _valid_format(token: str) -> bool:
    return isinstance(token, str) and 32 <= len(token) <= 128


async def verify_csrf(request: Request) -> None:
    """Raise 403 unless the submitted token matches the browser's token."""
    if request.method.upper() in _SAFE_METHODS:
        return
    expected = request.cookies.get(CSRF_COOKIE) or ""
    if not _valid_format(expected):
        raise HTTPException(status_code=403, detail="Form session expired. Reload the page and try again.")
    supplied = request.headers.get("x-csrf-token") or ""
    if not supplied:
        form = await request.form()
        supplied = str(form.get("csrf_token", ""))
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Form session expired. Reload the page and try again.")


class CSRFMiddleware:
    """Enforces CSRF on every unsafe method, before the routes run.

    Accepts the token from the X-CSRF-Token header (HTMX) or from the
    ``csrf_token`` hidden form field (plain HTML forms). The request body is
    buffered and replayed, so handlers can still parse the form normally.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        if request.method.upper() not in _SAFE_METHODS:
            expected = request.cookies.get(CSRF_COOKIE) or ""
            browser_token_ok = _valid_format(expected)
            supplied = request.headers.get("x-csrf-token") or ""
            buffered: list[dict] = []
            if not supplied and browser_token_ok:
                content_type = request.headers.get("content-type", "")
                if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
                    buffered, body_bytes = await _buffer_receive(receive)
                    if content_type.startswith("application/x-www-form-urlencoded"):
                        supplied = _form_token(body_bytes)
            if not browser_token_ok or not supplied or not secrets.compare_digest(supplied, expected):
                response = _plain_response(
                    403, "Form session expired or missing. Reload the page and try again."
                )
                await response(scope, receive, send)
                return

            downstream_receive = _replay_receive(buffered) if buffered else receive
            await self.app(scope, downstream_receive, _cookie_refreshing_send(request, send))
            return

        await self.app(scope, receive, _cookie_refreshing_send(request, send))


async def _buffer_receive(receive) -> tuple[list[dict], bytes]:
    messages: list[dict] = []
    body = b""
    while True:
        message = await receive()
        messages.append(message)
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    return messages, body


def _replay_receive(messages: list[dict]):
    iterator = iter(messages)

    async def replay_receive():
        try:
            return next(iterator)
        except StopIteration:
            return {"type": "http.disconnect"}

    return replay_receive


def _form_token(body: bytes) -> str:
    """Extract csrf_token=... from an urlencoded form body."""
    import urllib.parse

    for part in body.decode("utf-8", "replace").split("&"):
        name, _sep, value = part.partition("=")
        if name == "csrf_token":
            return urllib.parse.unquote_plus(value)
    return ""


def _cookie_refreshing_send(request: Request, send):
    async def send_wrapper(message):
        if message["type"] == "http.response.start":
            token = request.cookies.get(CSRF_COOKIE) or ""
            if not _valid_format(token):
                token = secrets.token_urlsafe(32)
                headers = message.setdefault("headers", [])
                headers.append(
                    (b"set-cookie", _cookie_header(token, request.url.scheme == "https"))
                )
        await send(message)

    return send_wrapper


def _cookie_header(token: str, secure: bool) -> bytes:
    parts = [
        f"{CSRF_COOKIE}={token}",
        "Path=/",
        "SameSite=Lax",
        "HttpOnly",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts).encode("ascii")


def _plain_response(status: int, text: str):
    from starlette.responses import PlainTextResponse

    return PlainTextResponse(text, status_code=status)
