"""app/providers/base.py

Abstract provider interfaces.

The whole application core (rules, worker, preview) depends ONLY on these
interfaces plus the data types below - never on a concrete adapter. Adapters
convert their own native sign/data conventions into the normalised types
defined here.

Normalisation contract
----------------------
* All prices are cents per kWh.
* ``current_buy_c`` / ``forecast_buy_c``: what the user pays to import.
* ``current_feed_in_c``: what the user EARNS on export; adapters flip their
  own sign conventions (e.g. Amber reports feed-in negative) so that a
  positive value here always means earning.
"""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class PriceSnapshot:
    current_buy_c: float
    current_feed_in_c: float
    forecast_buy_c: list[float]
    interval_minutes: int
    fetched_at: dt.datetime
    raw: dict[str, Any] | None = None


class ProviderError(RuntimeError):
    """Raised by adapters on any failure; carries a plain-language message."""


class PriceProvider(ABC):
    """Reads electricity prices for one site."""

    provider_name: str = "abstract-price"

    @abstractmethod
    async def get_prices(self, lookahead: int) -> PriceSnapshot:
        """Return a normalised PriceSnapshot covering ``lookahead`` intervals ahead."""

    async def test_connection(self) -> str:
        """Read-only check used by the 'Test connection' button."""
        await self.get_prices(lookahead=1)
        return "Connection ok: price data readable."


@dataclass
class Capabilities:
    """Which inverter operations the adapter can actually perform.

    A missing capability never crashes the core: the relevant rule is shown
    as 'unsupported' in the UI and skipped.
    """

    read_soc: bool = False
    block_export: bool = False
    restore_export: bool = False
    disable_grid_charge: bool = False
    restore_grid_charge: bool = False
    snapshot_settings: bool = False
    restore_snapshot: bool = False
    inspect: bool = False


NO_CAPABILITIES = Capabilities()


class InverterProvider(ABC):
    """Controls one inverter for one user."""

    provider_name: str = "abstract-inverter"
    capabilities: Capabilities = NO_CAPABILITIES

    @abstractmethod
    async def get_soc(self) -> float:
        """Battery state of charge in percent (0-100)."""

    @abstractmethod
    async def block_export(self) -> None:
        """Block grid exports entirely. Household use may still discharge the battery."""

    @abstractmethod
    async def restore_export(self) -> None:
        """Return exports to normal operation."""

    @abstractmethod
    async def disable_grid_charge(self) -> None:
        """Disable/pause/restrict grid charging and timed-charge windows."""

    @abstractmethod
    async def restore_grid_charge(self) -> None:
        """Return grid charging to normal operation."""

    async def snapshot_settings(self) -> dict[str, Any]:
        """Capture the current inverter configuration exactly (JSON-able)."""
        raise NotImplementedError("snapshot_settings unsupported by this provider")

    async def restore_snapshot(self) -> None:
        """Restore a previously captured configuration."""
        raise NotImplementedError("restore_snapshot unsupported by this provider")

    async def inspect(self) -> dict[str, Any]:
        """Return current device/settings state for the CLI/UI (read-only)."""
        raise NotImplementedError("inspect unsupported by this provider")

    async def test_connection(self) -> str:
        """Read-only check used by the 'Test connection' button."""
        soc = await self.get_soc()
        return f"Connection ok: SoC readable ({soc:g}%)."


@dataclass
class ProviderConfigField:
    """One adapter-declared configuration field.

    The /setup wizard and Settings page render their forms from these
    declarations; adding an adapter requires zero UI code.
    """

    key: str
    label: str
    type: str = "str"  # str|int|float|bool|choice
    secret: bool = False
    required: bool = False
    default: Any = None
    min: Any = None  # numeric bounds or min length for str
    max: Any = None
    choices: tuple[str, ...] | list[str] | None = None
    help_text: str = ""
    hint: str = ""  # 'where to find this' hint, plain language
    placeholder: str | None = None  # NEVER a real value

    def attrs(self) -> dict[str, Any]:
        a: dict[str, Any] = {}
        if self.type in ("int", "float") and self.min is not None:
            a["min"] = self.min
        if self.type in ("int", "float") and self.max is not None:
            a["max"] = self.max
        return a


class ProviderAdapter(ABC):
    """Common declaration surface for every adapter (schema + registration)."""

    provider_name: str
    kind: str  # "price" | "inverter"
    label: str  # human-readable name for the UI
    config_schema: list[ProviderConfigField] = []

    @classmethod
    def fields(cls) -> list[ProviderConfigField]:
        return list(cls.config_schema)

    @classmethod
    def validate_config(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Coerce + validate raw form data against the declared schema.

        Secret fields are not coerced further (they're stored encrypted); all
        values returned are either valid or a plain-language ValueError
        naming the field is raised.
        """
        import numbers

        schema = {f.key: f for f in cls.config_schema}
        unknown = set(values) - set(schema)
        if unknown:
            raise ValueError(
                f"{cls.label}: unknown setting(s): {', '.join(sorted(unknown))}."
            )
        out: dict[str, Any] = {}
        for defn in cls.config_schema:
            raw = values.get(defn.key)
            if raw in (None, ""):
                if defn.required:
                    # hint inside the message: which value is missing and why
                    raise ValueError(
                        f"{defn.label} is required for {cls.label}. "
                        + (f"Where to find it: {defn.hint}" if defn.hint else "")
                    )
                if raw is None or raw == "":
                    if defn.type == "bool":
                        out[defn.key] = bool(defn.default)
                    continue
            if defn.type in ("int", "float"):
                try:
                    num = float(raw) if defn.type == "float" else int(str(raw).strip())
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{defn.label}: expected a {defn.type}, got '{raw}'."
                    ) from None
                if isinstance(defn.min, numbers.Number) and num < defn.min:
                    raise ValueError(f"{defn.label}: must be at least {defn.min}.")
                if isinstance(defn.max, numbers.Number) and num > defn.max:
                    raise ValueError(f"{defn.label}: must be at most {defn.max}.")
                out[defn.key] = num
            elif defn.type == "bool":
                if isinstance(raw, bool):
                    out[defn.key] = raw
                else:
                    text = str(raw).strip().lower()
                    if text in ("true", "1", "yes", "on"):
                        out[defn.key] = True
                    elif text in ("false", "0", "no", "off"):
                        out[defn.key] = False
                    else:
                        raise ValueError(f"{defn.label}: please choose on or off.")
            elif defn.type == "choice":
                text = str(raw).strip()
                allowed = [str(c) for c in (defn.choices or [])]
                if text not in allowed:
                    raise ValueError(
                        f"{defn.label}: '{text}' is not one of the available options "
                        f"({', '.join(allowed)})."
                    )
                out[defn.key] = text
            else:
                text = str(raw)
                if defn.max is not None and len(text) > int(defn.max):
                    raise ValueError(
                        f"{defn.label}: must be at most {defn.max} characters."
                    )
                out[defn.key] = text
        return out


def describe_fields(fields: Iterable[ProviderConfigField]) -> list[dict[str, Any]]:
    return [
        {
            "key": f.key,
            "label": f.label,
            "type": f.type,
            "secret": f.secret,
            "required": f.required,
            "help_text": f.help_text,
            "hint": f.hint,
            "placeholder": f.placeholder,
            "attrs": f.attrs(),
        }
        for f in fields
    ]
