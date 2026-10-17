"""app/params.py

Single source of truth for every configurable parameter.

Every parameter is defined once here as a ``ParamDef`` carrying its type,
default, min/max bounds, help text, category and validation. The UI, (later)
API, CLI and tests all render/validate from this registry - nothing is
duplicated elsewhere.

Placeholder policy: no user-specific value (site IDs, tokens, serials) is
hard-coded anywhere in this codebase. Users supply them in the UI or CLI;
docs and tests use placeholders such as YOUR_SITE_ID.

Precedence (highest to lowest):
    1. per-user setting
    2. instance default (admin, DEFAULT_<PARAM> env var or admin page)
    3. code default (the ``default`` in the ParamDef below)
"""

from __future__ import annotations

import json
import numbers
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamDef:
    """One configurable parameter, defined exactly once."""

    key: str
    label: str
    type: str  # "int" | "float" | "bool" | "str" | "choice"
    default: Any
    help_text: str
    category: str = "user"  # "user" | "instance"
    min: Any = None
    max: Any = None
    choices: tuple[str, ...] | list[str] | None = None
    secret: bool = False  # values must be encrypted at rest / never logged
    required: bool = False
    placeholder: str | None = None  # shown in empty form fields, never a real value

    def coerce(self, raw: Any) -> Any:
        """Convert raw input (str from a form/JSON) to the declared type.

        Raises ValueError with a plain-language message on bad input.
        """
        if raw is None or raw == "":
            if self.type == "bool":
                return self.default  # unchecked box == default
            return None
        try:
            if self.type == "int":
                value = int(str(raw).strip())
            elif self.type == "float":
                value = float(str(raw).strip())
            elif self.type == "bool":
                if isinstance(raw, bool):
                    return raw
                text = str(raw).strip().lower()
                if text in ("true", "1", "yes", "on"):
                    return True
                if text in ("false", "0", "no", "off"):
                    return False
                raise ValueError(f"{self.label}: please choose on or off.")
            elif self.type == "choice":
                text = str(raw).strip()
                if self.choices and text not in [str(c) for c in self.choices]:
                    allowed = ", ".join(str(c) for c in self.choices)
                    raise ValueError(
                        f"{self.label}: '{text}' is not one of the available options ({allowed})."
                    )
                return text
            else:  # str
                return str(raw)
        except (ValueError, TypeError) as exc:
            if str(exc).startswith(self.label):
                raise
            raise ValueError(
                f"{self.label}: expected a {self.type}, got '{raw}'."
            ) from exc

        # numeric bounds
        if self.min is not None and isinstance(self.min, numbers.Number) and value < self.min:
            raise ValueError(f"{self.label}: must be at least {self.min} (got {value}).")
        if self.max is not None and isinstance(self.max, numbers.Number) and value > self.max:
            raise ValueError(f"{self.label}: must be at most {self.max} (got {value}).")
        return value

    def validate(self, value: Any) -> None:
        """Raise ValueError if ``value`` is invalid for this parameter."""
        coerced = self.coerce(value)
        if self.type == "str" and coerced is not None:
            coerced = str(coerced)
            if self.min is not None and len(coerced) < int(self.min):
                raise ValueError(
                    f"{self.label}: must be at least {int(self.min)} characters."
                )
            if self.max is not None and len(coerced) > int(self.max):
                raise ValueError(
                    f"{self.label}: must be at most {int(self.max)} characters."
                )
        if self.type == "choice" and coerced is not None and self.choices:
            if coerced not in [str(c) for c in self.choices]:
                allowed = ", ".join(str(c) for c in self.choices)
                raise ValueError(
                    f"{self.label}: '{coerced}' is not one of the available options ({allowed})."
                )
        # numeric bound check is already done in coerce() for valid numbers

    def input_attrs(self) -> dict[str, Any]:
        """HTML input attributes derived from the definition (schema-driven UI)."""
        attrs: dict[str, Any] = {}
        if self.type in ("int", "float"):
            attrs["inputmode"] = "numeric" if self.type == "int" else "decimal"
            if self.min is not None:
                attrs["min"] = self.min
            if self.max is not None:
                attrs["max"] = self.max
        if self.type == "str" and self.min is not None:
            attrs["minlength"] = int(self.min)
        if self.type == "str" and self.max is not None:
            attrs["maxlength"] = int(self.max)
        return attrs


# ---------------------------------------------------------------------------
# Per-user parameters (editable in Settings UI, stored per user)
# ---------------------------------------------------------------------------


USER_PARAMS: dict[str, ParamDef] = {p.key: p for p in [
    ParamDef(
        key="PRICE_PROVIDER",
        label="Price provider",
        type="choice",
        choices=[],  # filled in by the provider registry at import time
        default=None,
        help_text="Where electricity price data is read from.",
        required=False,  # required only once a provider is chosen
    ),
    ParamDef(
        key="INVERTER_PROVIDER",
        label="Inverter provider",
        type="choice",
        choices=[],  # filled in by the provider registry at import time
        default=None,
        help_text="Which inverter integration sends the commands.",
        required=False,
    ),
    ParamDef(
        key="TARGET_MRL",
        label="Target minimum reserve level (MRL) %",
        type="int",
        default=30,
        min=0,
        max=100,
        help_text="Battery SoC floor: when SoC is at or below this, exports are blocked.",
    ),
    ParamDef(
        key="HARDWARE_MIN_SOC",
        label="Hardware minimum SoC %",
        type="int",
        default=10,
        min=0,
        max=99,
        help_text="Battery SoC below which the inverter hardware stops discharging. "
        "This app never sets anything below this.",
    ),
    ParamDef(
        key="PRICE_THRESHOLD_CENTS",
        label="Price threshold (c/kWh)",
        type="float",
        default=30.0,
        min=0,
        max=10000,
        help_text="If the current or any forecast buy price is above this, grid charging is blocked.",
    ),
    ParamDef(
        key="LOOKAHEAD_INTERVALS",
        label="Lookahead intervals",
        type="int",
        default=6,
        min=1,
        max=96,
        help_text="How many forecast intervals ahead to consider for price spikes.",
    ),
    ParamDef(
        key="POLL_MINUTES",
        label="Poll interval (minutes)",
        type="int",
        default=5,
        min=1,
        max=120,
        help_text="How often the automation loop runs while the switch is ON.",
    ),
    ParamDef(
        key="SOC_HYSTERESIS",
        label="SoC hysteresis %",
        type="int",
        default=1,
        min=0,
        max=50,
        help_text="Exports are restored only when SoC >= MRL + hysteresis, to avoid rapid toggling.",
    ),
    ParamDef(
        key="ENABLE_EXPORT_CONTROL",
        label="Enable export control",
        type="bool",
        default=True,
        help_text="Turn Rule 1 (block exports at low SoC) on or off independently.",
    ),
    ParamDef(
        key="ENABLE_GRID_CHARGE_CONTROL",
        label="Enable grid-charge control",
        type="bool",
        default=True,
        help_text="Turn Rule 2 (block charging at high prices) on or off independently.",
    ),
    ParamDef(
        key="RESUME_AFTER_RESTART",
        label="Resume automation after restart",
        type="bool",
        default=False,
        help_text="If off, automation stays STOPPED after the server restarts and a banner is shown.",
    ),
    ParamDef(
        key="DRY_RUN",
        label="Dry run",
        type="bool",
        default=True,
        help_text="Read everything but never send commands to the inverter. Default for every new user.",
    ),
    ParamDef(
        key="ACTIVE_DAYS",
        label="Active days",
        type="str",
        default="all",
        max=20,
        placeholder="all",
        help_text="Comma-separated weekday abbreviations (mon,tue,...,sun) or 'all'. Optional schedule.",
    ),
    ParamDef(
        key="ACTIVE_FROM",
        label="Active from (HH:MM)",
        type="str",
        default="00:00",
        placeholder="00:00",
        max=5,
        help_text="Optional time-of-day window start in TIMEZONE.",
    ),
    ParamDef(
        key="ACTIVE_TO",
        label="Active to (HH:MM)",
        type="str",
        default="23:59",
        placeholder="23:59",
        max=5,
        help_text="Optional time-of-day window end in TIMEZONE.",
    ),
    ParamDef(
        key="TIMEZONE",
        label="Timezone",
        type="str",
        default="UTC",
        max=64,
        placeholder="e.g. Australia/Sydney",
        help_text="IANA timezone used for schedules and the active window.",
    ),
    ParamDef(
        key="PRICE_UNIT",
        label="Price unit label",
        type="str",
        default="c/kWh",
        max=16,
        help_text="Label used everywhere prices are displayed.",
    ),
    ParamDef(
        key="NOTIFY_EMAIL",
        label="Notification email address",
        type="str",
        default=None,
        max=254,
        placeholder="you@example.com",
        help_text="Optional target for email notifications.",
    ),
    ParamDef(
        key="NOTIFY_WEBHOOK_URL",
        label="Notification webhook / ntfy URL",
        type="str",
        default=None,
        max=512,
        placeholder="https://ntfy.sh/your-topic",
        help_text="Optional HTTP target for notifications (webhook or ntfy).",
    ),
    ParamDef(
        key="NOTIFY_EVENTS",
        label="Notify on events",
        type="str",
        default="stopped_unexpectedly,repeated_provider_failure,soc_unreadable",
        max=200,
        help_text="Comma-separated event names that trigger notifications.",
    ),
]}

_YES = {"true", "1", "yes", "on"}


# ---------------------------------------------------------------------------
# Command-mapping overrides (per-user, so different inverter models/firmware
# can be supported without code changes). Each adapter declares the mapping
# in its config schema; overrides live with the user's settings.
# ---------------------------------------------------------------------------

COMMAND_MAP_PARAMS: dict[str, ParamDef] = {
    "FOXESS_SETTING_KEY_BLOCK_EXPORT": ParamDef(
        key="FOXESS_SETTING_KEY_BLOCK_EXPORT",
        label="FoxESS: setting key used to block export",
        type="str",
        default=None,
        max=64,
        placeholder="YOUR_SETTING_KEY",
        help_text="Setting key of your inverter that blocks export. VERIFY AGAINST YOUR INVERTER.",
    ),
    "FOXESS_SETTING_VALUE_BLOCK_EXPORT": ParamDef(
        key="FOXESS_SETTING_VALUE_BLOCK_EXPORT",
        label="FoxESS: value that blocks export",
        type="str",
        default=None,
        max=32,
        placeholder="YOUR_VALUE",
        help_text="Value to write to the export-block key. VERIFY AGAINST YOUR INVERTER.",
    ),
    "FOXESS_SETTING_KEY_DISABLE_GRID_CHARGE": ParamDef(
        key="FOXESS_SETTING_KEY_DISABLE_GRID_CHARGE",
        label="FoxESS: setting key used to disable grid charging",
        type="str",
        default=None,
        max=64,
        placeholder="YOUR_SETTING_KEY",
        help_text="Setting key of your inverter that disables grid charging. VERIFY AGAINST YOUR INVERTER.",
    ),
    "FOXESS_SETTING_VALUE_DISABLE_GRID_CHARGE": ParamDef(
        key="FOXESS_SETTING_VALUE_DISABLE_GRID_CHARGE",
        label="FoxESS: value that disables grid charging",
        type="str",
        default=None,
        max=32,
        placeholder="YOUR_VALUE",
        help_text="Value to write to the grid-charge-disabling key. VERIFY AGAINST YOUR INVERTER.",
    ),
}

def _replace(defn: ParamDef, **changes: Any) -> ParamDef:
    """Dataclass replace with a short name for module-level use."""
    from dataclasses import replace

    return replace(defn, **changes)


USER_PARAMS.update(COMMAND_MAP_PARAMS)
USER_PARAMS["PRICE_PROVIDER"] = _replace(USER_PARAMS["PRICE_PROVIDER"], choices=[])
USER_PARAMS["INVERTER_PROVIDER"] = _replace(USER_PARAMS["INVERTER_PROVIDER"], choices=[])


def register_provider_choices(price_providers: Iterable[str], inverter_providers: Iterable[str]) -> None:
    """Called at app startup: fill the provider choice lists from the registry."""
    USER_PARAMS["PRICE_PROVIDER"] = _replace(
        USER_PARAMS["PRICE_PROVIDER"], choices=list(price_providers)
    )
    USER_PARAMS["INVERTER_PROVIDER"] = _replace(
        USER_PARAMS["INVERTER_PROVIDER"], choices=list(inverter_providers)
    )


# ---------------------------------------------------------------------------
# Instance parameters (admin only; set via env with APP_/INSTANCE_ prefix,
# editable later from the admin page)
# ---------------------------------------------------------------------------


INSTANCE_PARAMS: dict[str, ParamDef] = {p.key: p for p in [
    ParamDef(
        key="APP_SECRET_KEY",
        label="App secret key",
        type="str",
        default=None,
        secret=True,
        required=True,
        min=32,
        help_text="Master key used to hash/encrypt secrets (Fernet) and sign sessions. "
        "Losing it makes stored credentials unreadable.",
        category="instance",
    ),
    ParamDef(
        key="DATABASE_URL",
        label="Database URL",
        type="str",
        default=None,
        required=True,
        help_text="SQLAlchemy URL, e.g. sqlite+aiosqlite:///./app.db (placeholder, not a real path).",
        category="instance",
    ),
    ParamDef(
        key="BASE_URL",
        label="Public base URL",
        type="str",
        default=None,
        required=True,
        max=254,
        placeholder="https://your-domain.example",
        help_text="Canonical externally reachable URL, used in links and notifications.",
        category="instance",
    ),
    ParamDef(
        key="ALLOWED_HOSTS",
        label="Allowed hosts",
        type="str",
        default="",
        max=512,
        help_text="Comma-separated host names the app answers for.",
        category="instance",
    ),
    ParamDef(
        key="SMTP_HOST",
        label="SMTP host",
        type="str",
        default=None,
        max=254,
        placeholder="smtp.example.com",
        help_text="SMTP server for notifications.",
        category="instance",
    ),
    ParamDef(
        key="SMTP_PORT",
        label="SMTP port",
        type="int",
        default=587,
        min=1,
        max=65535,
        help_text="SMTP port (587 submission by default).",
        category="instance",
    ),
    ParamDef(
        key="SMTP_USER",
        label="SMTP user",
        type="str",
        default=None,
        max=254,
        placeholder="YOUR_SMTP_USER",
        help_text="SMTP username.",
        category="instance",
    ),
    ParamDef(
        key="SMTP_PASSWORD",
        label="SMTP password",
        type="str",
        default=None,
        secret=True,
        max=254,
        placeholder="YOUR_SMTP_PASSWORD",
        help_text="SMTP password (stored encrypted, never displayed again).",
        category="instance",
    ),
    ParamDef(
        key="SMTP_FROM",
        label="SMTP from address",
        type="str",
        default=None,
        max=254,
        placeholder="notifications@your-domain.example",
        help_text="From address for notification emails.",
        category="instance",
    ),
    ParamDef(
        key="SESSION_LIFETIME_HOURS",
        label="Session lifetime (hours)",
        type="int",
        default=72,
        min=1,
        max=24 * 30,
        help_text="How long a signed-in session lives before re-authentication.",
        category="instance",
    ),
    ParamDef(
        key="SIGNUP_MODE",
        label="Signup mode",
        type="choice",
        choices=["invite_only", "closed", "admin_created"],
        default="invite_only",
        help_text="How new accounts can be created.",
        category="instance",
    ),
    ParamDef(
        key="MAX_USERS",
        label="Maximum users",
        type="int",
        default=50,
        min=1,
        max=100000,
        help_text="Instance-wide cap on the number of accounts.",
        category="instance",
    ),
    ParamDef(
        key="MIN_POLL_MINUTES",
        label="Minimum poll interval (minutes)",
        type="int",
        default=1,
        min=1,
        max=60,
        help_text="Users cannot set POLL_MINUTES below this instance-wide limit. "
        "Staggering starts are derived from it.",
        category="instance",
    ),
    ParamDef(
        key="LOG_RETENTION_DAYS",
        label="Log retention (days)",
        type="int",
        default=30,
        min=1,
        max=3650,
        help_text="poll_history and audit/log rows older than this are pruned.",
        category="instance",
    ),
    ParamDef(
        key="DEBUG",
        label="Debug mode",
        type="bool",
        default=False,
        help_text="Instance-side debug switch. The app refuses to start with DEBUG on in production.",
        category="instance",
    ),
]}

ALL_PARAMS: dict[str, ParamDef] = {**INSTANCE_PARAMS, **USER_PARAMS}


# ---------------------------------------------------------------------------
# Registry access helpers
# ---------------------------------------------------------------------------


def get(key: str) -> ParamDef:
    try:
        return ALL_PARAMS[key]
    except KeyError:
        raise KeyError(
            f"Unknown parameter '{key}'. Known parameters: {', '.join(sorted(ALL_PARAMS))}"
        ) from None


def user_param_keys() -> list[str]:
    return list(USER_PARAMS)


def instance_param_keys() -> list[str]:
    return list(INSTANCE_PARAMS)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ParamValidationError(ValueError):
    """Raised with a plain-language message naming the offending parameter."""


def validate_section(section: str, values: dict[str, Any]) -> dict[str, Any]:
    """Validate a dict of raw (form/JSON/CLI) values against the registry.

    Returns the coerced value dict; raises ParamValidationError with a
    plain-language, parameter-naming message on any invalid value.
    Unknown keys raise as well: parameters are defined once, here.
    """
    registry = USER_PARAMS if section == "user" else INSTANCE_PARAMS
    out: dict[str, Any] = {}
    errors: list[str] = []
    for key, raw in values.items():
        defn = registry.get(key)
        if defn is None:
            errors.append(f"Unknown parameter '{key}' for section '{section}'.")
            continue
        try:
            out[key] = defn.coerce(raw)
            if isinstance(raw, str) and raw.strip() != "":
                defn.validate(raw)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ParamValidationError("; ".join(errors))
    return out


# ---------------------------------------------------------------------------
# Precedence resolution
# ---------------------------------------------------------------------------


def resolve(
    key: str,
    user_values: dict[str, Any] | None = None,
    instance_values: dict[str, Any] | None = None,
) -> Any:
    """Resolve one parameter with precedence: user > instance > code default."""
    defn = get(key)
    if user_values and key in user_values and user_values[key] not in (None, ""):
        return defn.coerce(user_values[key]) if defn.type not in ("bool",) else bool(user_values[key])
    if instance_values and key in instance_values and instance_values[key] not in (None, ""):
        return defn.coerce(instance_values[key]) if defn.type not in ("bool",) else bool(instance_values[key])
    return defn.default


def resolve_all_user(
    user_values: dict[str, Any] | None = None,
    instance_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve every user parameter into a flat dict for the core loop."""
    return {key: resolve(key, user_values, instance_values) for key in user_param_keys()}


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------

_SECRET_MARKERS = ("KEY", "TOKEN", "PASSWORD", "SECRET")


def is_secret_param(key: str) -> bool:
    defn = ALL_PARAMS.get(key)
    if defn is None:
        raise KeyError(key)
    return defn.secret or any(marker in key for marker in _SECRET_MARKERS)


def export_user_config(
    values: dict[str, Any],
    credentials: dict[str, Any] | None = None,
) -> str:
    """Serialise a user's settings as JSON for import elsewhere, secrets excluded.

    Provider credentials (token/site-id/API-key values) are never exported.
    """
    keys = user_param_keys()
    exportable = {key: resolve(key, values) for key in keys}
    if credentials:
        # exported as schema *keys with empty values* so the target account is pre-filled and still prompted
        creds_section: dict[str, dict[str, None]] = {}
        for provider_name, fields_ in credentials.items():
            names = fields_.keys() if isinstance(fields_, dict) else fields_
            creds_section[provider_name] = {field: None for field in names}
        exportable |= {"credentials": creds_section}
    return json.dumps(
        {"app": "solar-automation", "version": 1, "params": exportable},
        indent=2,
        sort_keys=True,
    )


def import_user_config(payload: str | bytes | dict) -> dict[str, Any]:
    """Parse an exported profile into raw values ready for validate_section('user', ...)."""
    if isinstance(payload, bytes):
        payload = payload.decode()
    try:
        data = json.loads(payload) if isinstance(payload, str) else dict(payload)
    except json.JSONDecodeError as exc:
        raise ParamValidationError(
            "Import file is not valid JSON: " + str(exc)
        ) from exc
    if not isinstance(data, dict) or "params" not in data:
        raise ParamValidationError("Import file does not look like an app settings export.")
    params = data.get("params")
    if not isinstance(params, dict):
        raise ParamValidationError("Import file has no 'params' section.")
    # Drop anything unknown rather than silently importing junk...
    unknown = [k for k in params if k not in USER_PARAMS]
    if unknown:
        raise ParamValidationError(
            "Import file contains unknown parameters: " + ", ".join(sorted(unknown)) + "."
        )
    return {k: params[k] for k in params if params[k] not in (None, "")}


# ---------------------------------------------------------------------------
# Documentation generation
# ---------------------------------------------------------------------------


def describe_all() -> list[ParamDef]:
    return list(ALL_PARAMS.values())


def to_docs_markdown() -> str:
    """Render every parameter into a Markdown table for docs/PARAMETERS.md."""
    lines: list[str] = [
        "# Parameters",
        "",
        "Every parameter in this app is defined once in `app/params.py`. "
        "Precedence is always **user setting > instance default > code default**.",
        "",
    ]
    for section, items in (("User parameters", USER_PARAMS), ("Instance parameters", INSTANCE_PARAMS)):
        lines.append(f"## {section}")
        lines.append("")
        lines.append("| Key | Type | Default | Min | Max | Secret | Description |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for defn in items.values():
            shown_default = defn.default
            lines.append(
                f"| `{defn.key}` | {defn.type} | {shown_default} | "
                f"{defn.min if defn.min is not None else '—'} | "
                f"{defn.max if defn.max is not None else '—'} | "
                f"{'yes' if defn.secret else 'no'} | {defn.help_text} |"
            )
        lines.append("")
    return "\n".join(lines)
