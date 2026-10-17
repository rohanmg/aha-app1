"""app/models.py

SQLAlchemy models for the eight application tables:

users, invites, credentials, settings, automation_state,
inverter_snapshots, action_log, poll_history.

Per-user isolation: every query filters by user_id at the query sites; the
models carry the FK/unique constraints that make ownership explicit.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    credentials: Mapped[list["Credential"]] = relationship(back_populates="user")
    settings: Mapped["Settings"] = relationship(back_populates="user", uselist=False)
    automation_state: Mapped["AutomationState"] = relationship(
        back_populates="user", uselist=False
    )


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    claimed_by_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LoginSession(Base):
    """Server-side session: the cookie carries only a random token; the DB
    holds its hash so a leaked database cannot mint sessions, and logout or
    account deactivation revokes instantly."""

    __tablename__ = "login_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)


class LoginAttempt(Base):
    """Failed sign-in attempts, for the login rate limit + lockout."""

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_email_time", "email_hash", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_hash: Mapped[str] = mapped_column(String(128), index=True)  # sha256 of the email
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Credential(Base):
    """Provider credentials, encrypted at rest with the instance Fernet key."""

    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_cred_user_provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64))  # e.g. "amber", "foxess", "mock"
    encrypted_json: Mapped[str] = mapped_column(Text)  # Fernet blob with all fields
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="credentials")


class Settings(Base):
    """Per-user parameter values; absent keys fall back per params.py precedence."""

    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("user_id", name="uq_settings_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    values_json: Mapped[str] = mapped_column(Text, default="{}")  # plaintext settings
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="settings")


class AutomationState(Base):
    """Single row per user: the running/stopped switch and last applied state."""

    __tablename__ = "automation_state"
    __table_args__ = (UniqueConstraint("user_id", name="uq_automation_state_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    dry_run_effective: Mapped[bool] = mapped_column(Boolean, default=True)
    last_poll_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_poll_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_applied_json: Mapped[str] = mapped_column(Text, default="{}")  # e.g. export_blocked: true
    provider_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="automation_state")


class InverterSnapshot(Base):
    """Saved original inverter configuration, restored on user request."""

    __tablename__ = "inverter_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    settings_json: Mapped[str] = mapped_column(Text)  # provider-native JSON
    taken_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    restored_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ActionLog(Base):
    """Per-user audit trail of every decision/write/notify/skip."""

    __tablename__ = "action_log"
    __table_args__ = (Index("ix_action_log_user_time", "user_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))  # decision|write|skipped|error|notify|info
    rule: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    message: Mapped[str] = mapped_column(Text)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class PollHistory(Base):
    """One row per automation cycle while the switch is ON; used for replay."""

    __tablename__ = "poll_history"
    __table_args__ = (Index("ix_poll_history_user_time", "user_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    soc: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    feed_in_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    forecast: Mapped[list] = mapped_column(JSON, default=list)  # normalised c/kWh list
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    export_blocked: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    grid_charge_disabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "User",
    "Invite",
    "LoginSession",
    "LoginAttempt",
    "Credential",
    "Settings",
    "AutomationState",
    "InverterSnapshot",
    "ActionLog",
    "PollHistory",
]
