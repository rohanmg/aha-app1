"""app/auth.py

Password hashing, server-side sessions, login lockout and invite helpers.

Security properties (tested):
* Passwords hashed with argon2; verification is constant-error, generic.
* Sessions are random tokens; only a SHA-256 hash of the token is stored,
  so a leaked database cannot be replayed as sessions.
* Login lockout: after MAX_FAILED_LOGINS failures within the window the
  account/email is locked for LOCKOUT_MINUTES, enforced on every attempt.
* Invites: single-use, hashed token lookup, expiry-aware.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DBSession

from app.config import get_settings
from app.models import LoginAttempt, LoginSession, User

SESSION_COOKIE = "aha_session"
_ph = PasswordHasher()

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
ATTEMPT_WINDOW_MINUTES = 15


def _email_hash(email: str) -> str:
    """Attempt tracking keyed by a hash: plaintext emails are never stored."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: Optional[str], password: str) -> bool:
    if not stored_hash:
        return False
    try:
        return _ph.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------


def _lockout_counts() -> tuple[int, int]:
    settings = get_settings()
    failed = int(getattr(settings, "MAX_FAILED_LOGINS", MAX_FAILED_LOGINS) or MAX_FAILED_LOGINS)
    window = int(getattr(settings, "LOCKOUT_MINUTES", LOCKOUT_MINUTES) or LOCKOUT_MINUTES)
    return failed, window


def is_locked(email: str, session: DBSession) -> bool:
    """True when the last N attempts in the window are all failures."""
    limit, window = _lockout_counts()
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=window)
    rows = session.scalars(
        select(LoginAttempt)
        .where(LoginAttempt.email_hash == _email_hash(email), LoginAttempt.ts >= since)
        .order_by(LoginAttempt.ts.desc())
        .limit(limit)
    ).all()
    return len(rows or []) >= limit


def record_failed_login(email: str, session: DBSession) -> None:
    session.add(LoginAttempt(email_hash=_email_hash(email)))
    session.commit()


def clear_failed_logins(email: str, session: DBSession) -> None:
    session.execute(
        delete(LoginAttempt).where(LoginAttempt.email_hash == _email_hash(email))
    )
    session.commit()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int, session: DBSession) -> str:
    """Create a server-side session; return the cookie token (show once)."""
    token = secrets.token_urlsafe(32)
    lifetime = dt.timedelta(hours=int(get_settings().SESSION_LIFETIME_HOURS or 72))
    session.add(
        LoginSession(
            token_hash=_token_hash(token),
            user_id=user_id,
            expires_at=dt.datetime.now(dt.timezone.utc) + lifetime,
        )
    )
    session.commit()
    return token


def get_session_user_id(token: Optional[str], session: DBSession) -> Optional[int]:
    """Resolve a cookie token to a live user id, or None."""
    if not token:
        return None
    row = session.scalar(
        select(LoginSession).where(LoginSession.token_hash == _token_hash(token))
    )
    if row is None:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:  # SQLite returns naive UTC datetimes
        expires = expires.replace(tzinfo=dt.timezone.utc)
    if expires < dt.datetime.now(dt.timezone.utc):
        session.delete(row)
        session.commit()
        return None
    return row.user_id


def destroy_session(token: Optional[str], session: DBSession) -> None:
    if not token:
        return
    session.execute(delete(LoginSession).where(LoginSession.token_hash == _token_hash(token)))
    session.commit()


def destroy_all_sessions(user_id: int, session: DBSession) -> None:
    """Revoke every session for a user (admin disable / password change)."""
    session.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
    session.commit()


# ---------------------------------------------------------------------------
# Password reset (one-use emailed passcode)
# ---------------------------------------------------------------------------


def create_password_reset(session: DBSession, user_id: int, ttl_minutes: int) -> str:
    """Issue a new 6-digit passcode, invalidating older ones for the user."""
    from app.models import PasswordReset

    session.execute(
        delete(PasswordReset).
        where(PasswordReset.user_id == user_id, PasswordReset.used_at.is_(None))
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = dt.datetime.now(dt.timezone.utc)
    session.add(
        PasswordReset(
            user_id=user_id,
            code_hash=_token_hash(code),
            created_at=now,
            expires_at=now + dt.timedelta(minutes=ttl_minutes),
        )
    )
    session.commit()
    return code


def count_reset_codes(session: DBSession, user_id: int, minutes: int) -> int:
    """Codes issued for this user inside the last ``minutes`` minutes."""
    from app.models import PasswordReset

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    return int(session.scalar(
        select(func.count(PasswordReset.id)).
        where(PasswordReset.user_id == user_id, PasswordReset.created_at >= since)
    ) or 0)


def consume_password_reset(session: DBSession, user_id: int, code: str) -> bool:
    """Mark the matching live passcode as used; False when no match."""
    from app.models import PasswordReset

    row = session.scalar(
        select(PasswordReset).
        where(
            PasswordReset.user_id == user_id,
            PasswordReset.code_hash == _token_hash(code),
            PasswordReset.used_at.is_(None),
            PasswordReset.expires_at >= dt.datetime.now(dt.timezone.utc),
        )
    )
    if row is None:
        return False
    row.used_at = dt.datetime.now(dt.timezone.utc)
    session.commit()
    return True


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


def create_invite(session: DBSession, created_by_user_id: int | None) -> str:
    """Create a single-use invite; return the raw token to send to the invitee."""
    from app.models import Invite

    raw = secrets.token_urlsafe(24)
    session.add(Invite(token_hash=_token_hash(raw), created_by_user_id=created_by_user_id))
    session.commit()
    return raw


def consume_invite(raw_token: str, session: DBSession) -> Optional["Invite"]:  # type: ignore[name-defined]
    """Mark a valid, unclaimed invite as used; None if invalid/expired/used."""
    from app.models import Invite

    row = session.scalar(select(Invite).where(Invite.token_hash == _token_hash(raw_token)))
    if row is None or row.claimed_at is not None:
        return None
    if row.expires_at is not None and row.expires_at < dt.datetime.now(dt.timezone.utc):
        return None
    row.claimed_at = dt.datetime.now(dt.timezone.utc)
    session.commit()
    return row


def signup_mode() -> str:
    return get_settings().SIGNUP_MODE or "invite_only"
