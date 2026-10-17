"""app/config.py

Instance-level configuration via pydantic-settings (environment variables).

Every value here is an instance parameter from app/params.py with a
``DEFAULT_``-free, direct env binding. Nothing user-specific belongs here:
users configure providers per account in the UI.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.params import INSTANCE_PARAMS


class Settings(BaseSettings):
    """Instance settings. Every field maps 1:1 to an INSTANCE_PARAMS entry."""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    APP_SECRET_KEY: Optional[str] = None
    DATABASE_URL: Optional[str] = None
    BASE_URL: Optional[str] = None
    ALLOWED_HOSTS: str = ""
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM: Optional[str] = None
    RESEND_API_KEY: Optional[str] = None
    MAIL_FROM: str = "onboarding@resend.dev"
    RESET_CODE_TTL_MINUTES: int = 10
    RESET_MAX_CODES_PER_WINDOW: int = 3
    RESCUE_TOKEN: Optional[str] = None
    SESSION_LIFETIME_HOURS: int = 72
    SIGNUP_MODE: str = "invite_only"
    MAX_USERS: int = 50
    MIN_POLL_MINUTES: int = 1
    LOG_RETENTION_DAYS: int = 30
    DEBUG: bool = False

    # Deploy-hosting features (ephemeral-disk hosts, single-process deploys).
    HF_TOKEN: Optional[str] = None
    BACKUP_REPO_ID: Optional[str] = None
    BACKUP_INTERVAL_SECONDS: int = 600
    BACKUP_KEEP: int = 24
    WORKER_EMBED: str = "off"
    WORKER_INTERVAL_SECONDS: int = 300

    # --- helpers over the registry ----------------------------------------

    def validate_against_params(self) -> list[str]:
        """Cross-check settings against app/params.py definitions (single source)."""
        issues: list[str] = []
        for key in INSTANCE_PARAMS:
            defn = INSTANCE_PARAMS[key]
            value = getattr(self, key, None)
            if defn.required and (value is None or value == ""):
                issues.append(f"{defn.label} ({key}) is required.")
                continue
            try:
                defn.validate(value)
            except ValueError as exc:
                issues.append(str(exc))
        return issues

    def instance_defaults_for_user_params(self) -> dict[str, object]:
        """Admin-configurable instance defaults feed DEFAULT_<PARAM> env vars.

        Any env var named DEFAULT_TARGET_MRL, DEFAULT_POLL_MINUTES etc.
        overrides the code default for that user parameter; read/validated
        straight from app/params.py so there is one source of truth.
        """
        import os

        from app.params import USER_PARAMS

        defaults: dict[str, object] = {}
        for key, defn in USER_PARAMS.items():
            env_value = os.environ.get(f"DEFAULT_{key}")
            if env_value not in (None, ""):
                try:
                    defaults[key] = defn.coerce(env_value)
                except ValueError as exc:
                    raise ValueError(
                        f"Instance default DEFAULT_{key} is invalid: {exc}"
                    ) from exc
        return defaults


@lru_cache
def get_settings() -> Settings:
    return Settings()
