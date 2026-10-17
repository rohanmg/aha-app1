"""app/providers/registry.py

Adapter registry with decorator registration.

Adapters register themselves by subclassing PriceProvider/InverterProvider
and applying :func:`register`. Unknown provider names fail with a clear
message listing the available ones.
"""
from __future__ import annotations

from typing import Type, TypeVar

from app.providers.base import PriceProvider, ProviderAdapter

_TAdapter = TypeVar("_TAdapter", bound=ProviderAdapter)

_price_adapters: dict[str, Type[PriceProvider]] = {}
_inverter_adapters: dict[str, Type] = {}


def register(cls: Type[_TAdapter]) -> Type[_TAdapter]:
    """Class decorator: register a PriceProvider or InverterProvider adapter."""
    kind = getattr(cls, "kind", None)
    name = getattr(cls, "provider_name", None)
    if not name or not kind:
        raise TypeError("Adapter subclasses must define provider_name and kind.")
    if kind == "price":
        _price_adapters[name] = cls  # type: ignore[assignment]
    elif kind == "inverter":
        _inverter_adapters[name] = cls  # type: ignore[assignment]
    else:
        raise TypeError(f"Unknown adapter kind '{kind}' (expected 'price' or 'inverter').")
    return cls


class UnknownProviderError(ValueError):
    def __init__(self, provider: str, available: list[str]) -> None:
        super().__init__(
            f"Provider '{provider}' is not available. "
            f"Available providers: {', '.join(sorted(available))}."
        )


def get_price_adapter(name: str) -> Type[PriceProvider]:
    if name not in _price_adapters:
        raise UnknownProviderError(name, list_price_providers())
    return _price_adapters[name]


def get_inverter_adapter(name: str) -> Type:
    if name not in _inverter_adapters:
        raise UnknownProviderError(name, list_inverter_providers())
    return _inverter_adapters[name]


def list_price_providers() -> list[str]:
    return sorted(_price_adapters)


def list_inverter_providers() -> list[str]:
    return sorted(_inverter_adapters)


def all_adapters() -> list[dict[str, object]]:
    """Schema descriptions for the /setup wizard and Settings page."""
    out: list[dict[str, object]] = []
    for cls in list(_price_adapters.values()) + list(_inverter_adapters.values()):  # type: ignore[arg-type]
        out.append(
            {
                "provider_name": cls.provider_name,
                "kind": cls.kind,
                "label": cls.label,
                "config_schema": [
                    f.__dict__ | {"choices": f.choices, "attrs": f.attrs()}
                    for f in cls.fields()  # type: ignore[attr-defined]
                ],
            }
        )
    return out


def import_all_adapters() -> None:
    """Import every adapter module so their @register decorators run.

    Called at app startup (and by tests) so the registry is populated
    regardless of import order. Adapters that are not implemented yet (e.g.
    a real vendor adapter pending its phase) are skipped with a note rather
    than guessed at or silently swallowed.
    """
    import importlib
    import logging

    log = logging.getLogger(__name__)
    for module in ("app.providers.mock", "app.providers.amber", "app.providers.foxess", "app.providers._template"):
        try:
            importlib.import_module(module)
        except ModuleNotFoundError:
            log.info("adapter %s not implemented yet; skipping", module)
