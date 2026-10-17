"""app/state.py

Runtime helpers that glue the DB to the core: resolving a user's effective
parameters (user > instance default > code default), reading/writing
settings and credentials, and building the UI context.

All helpers take an explicit ``user_id`` and never touch another user's
rows: strict per-user isolation is enforced here.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import params
from app.crypto import decrypt_obj, encrypt_obj, fernet_from_secret, mask_secret
from app.models import AutomationState, Credential, Settings, User


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def get_user_settings(session: Session, user_id: int) -> dict[str, Any]:
    """Return the user's stored raw parameter values as a dict."""
    row = session.scalar(select(Settings).where(Settings.user_id == user_id))
    if row is None:
        return {}
    try:
        return json.loads(row.values_json)
    except (TypeError, ValueError):
        return {}


def save_user_settings(session: Session, user_id: int, values: dict[str, Any]) -> None:
    """Persist validated raw user values (JSON, plaintext; never credentials)."""
    row = session.scalar(select(Settings).where(Settings.user_id == user_id))
    stored = {k: v for k, v in values.items() if v not in (None, "")}
    if row is None:
        row = Settings(user_id=user_id, values_json=json.dumps(stored))
        session.add(row)
    else:
        row.values_json = json.dumps(stored)
        row.updated_at = dt.datetime.now(dt.timezone.utc)
    session.commit()


def instance_defaults(session: Session) -> dict[str, Any]:
    """Admin-configurable instance defaults (empty in phase 1; env first later)."""
    stored = session.scalar(
        select(Settings).join(User, User.id == Settings.user_id).where(User.is_admin.is_(True))
    )
    if isinstance(stored, Settings):
        return get_user_settings_internal_defaults(stored)
    return {}


def get_user_settings_internal_defaults(_row: Settings) -> dict[str, Any]:
    # Placeholder indirection kept for phase 3 (admin-profile defaults).
    return {}


def resolve_user_params(
    session: Session, user_id: int, instance: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Effective parameters for one user: user > instance default > code default."""
    from app.config import get_settings

    instance_values = instance if instance is not None else get_settings().instance_defaults_for_user_params()
    user_values = get_user_settings(session, user_id)
    return params.resolve_all_user(user_values, instance_values)


# ---------------------------------------------------------------------------
# Credentials (encrypted)
# ---------------------------------------------------------------------------


def fernet_for(session: Session) -> Any:
    """Fernet instance from the app secret; raises if unconfigured."""
    from app.config import get_settings

    secret = get_settings().APP_SECRET_KEY
    if not secret:
        raise RuntimeError(
            "APP_SECRET_KEY instance parameter is missing; cannot encrypt or decrypt credentials."
        )
    return fernet_from_secret(secret)


def get_credentials(session: Session, user_id: int, provider: str) -> dict[str, Any]:
    row = session.scalar(
        select(Credential).where(Credential.user_id == user_id, Credential.provider == provider)
    )
    if row is None:
        return {}
    return decrypt_obj(fernet_for(session), row.encrypted_json)


def save_credentials(session: Session, user_id: int, provider: str, values: dict[str, Any]) -> None:
    row = session.scalar(
        select(Credential).where(Credential.user_id == user_id, Credential.provider == provider)
    )
    encrypted = encrypt_obj(fernet_for(session), values)
    if row is None:
        session.add(Credential(user_id=user_id, provider=provider, encrypted_json=encrypted))
    else:
        row.encrypted_json = encrypted
        row.updated_at = dt.datetime.now(dt.timezone.utc)
    session.commit()


def delete_credentials(session: Session, user_id: int, provider: str) -> bool:
    row = session.scalar(
        select(Credential).where(Credential.user_id == user_id, Credential.provider == provider)
    )
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def masked_credentials(
    session: Session, user_id: int, provider: str
) -> dict[str, str]:
    """Field-name -> masked display value, for saved-credential UIs."""
    values = get_credentials(session, user_id, provider)
    return {k: mask_secret(v) for k, v in values.items()}


# ---------------------------------------------------------------------------
# Automation state
# ---------------------------------------------------------------------------


def get_state_row(session: Session, user_id: int) -> AutomationState:
    row = session.scalar(select(AutomationState).where(AutomationState.user_id == user_id))
    if row is None:
        row = AutomationState(user_id=user_id)
        session.add(row)
        session.commit()
    return row


def is_running(session: Session, user_id: int) -> bool:
    return get_state_row(session, user_id).running


def default_param_values() -> dict[str, Any]:
    """Code defaults for every user parameter (for new-account profiles)."""
    return {key: defn.default for key, defn in params.USER_PARAMS.items()}
