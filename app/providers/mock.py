"""app/providers/mock.py

Mock providers with fully controllable values, for demos and tests.

Nothing is sent anywhere. Values are set programmatically or via config
fields, so tests and previews can drive exact scenarios (SoC, prices,
applied writes) without any network or inverter involvement.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from app.providers.base import (
    Capabilities,
    PriceProvider,
    PriceSnapshot,
    ProviderAdapter,
    ProviderConfigField,
    ProviderError,
    describe_fields,
)
from app.providers.registry import register


@register
class MockPriceProvider(PriceProvider, ProviderAdapter):
    provider_name = "mock"
    kind = "price"
    label = "Mock price provider (demo/testing)"
    config_schema = [
        ProviderConfigField(
            key="current_buy_c",
            label="Simulated current buy price (c/kWh)",
            type="float",
            default=25.0,
            min=0,
            max=1000,
            help_text="Value returned as the current grid buy price.",
            placeholder="25.0",
        ),
        ProviderConfigField(
            key="current_feed_in_c",
            label="Simulated feed-in price (c/kWh)",
            type="float",
            default=5.0,
            min=0,
            max=1000,
            help_text="Value returned as the export (feed-in) price.",
            placeholder="5.0",
        ),
        ProviderConfigField(
            key="forecast_buy_c",
            label="Simulated forecast prices (c/kWh)",
            type="str",
            default="12,18,40",
            help_text="Comma-separated forecast buy prices for the lookahead window.",
            placeholder="12,18,40",
        ),
        ProviderConfigField(
            key="interval_minutes",
            label="Interval length (minutes)",
            type="int",
            default=30,
            min=1,
            max=240,
            help_text="Length of one price interval.",
        ),
    ]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # config values may also be mutated at runtime by tests/demos:
        self.config = self.validate_config(config or {})
        self.fetch_attempts: list[int] = []
        self.fail_next: int = 0  # >0: next N get_prices calls raise

    async def get_prices(self, lookahead: int) -> PriceSnapshot:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ProviderError("Mock price provider configured to fail.")
        self.fetch_attempts.append(lookahead)
        cfg = self.config
        forecast_raw = str(cfg.get("forecast_buy_c") or "").split(",")
        forecast = [float(x) for x in forecast_raw if x.strip() != ""]
        return PriceSnapshot(
            current_buy_c=float(cfg.get("current_buy_c", 25.0)),
            current_feed_in_c=float(cfg.get("current_feed_in_c", 5.0)),
            forecast_buy_c=forecast[: max(0, int(lookahead))],
            interval_minutes=int(cfg.get("interval_minutes", 30)),
            fetched_at=dt.datetime.now(dt.timezone.utc),
        )


@register
class MockInverterProvider(ProviderAdapter):
    provider_name = "mock"
    kind = "inverter"
    label = "Mock inverter (demo/testing)"
    capabilities = Capabilities(
        read_soc=True,
        block_export=True,
        restore_export=True,
        disable_grid_charge=True,
        restore_grid_charge=True,
        snapshot_settings=True,
        restore_snapshot=True,
        inspect=True,
    )
    config_schema = [
        ProviderConfigField(
            key="initial_soc",
            label="Simulated battery SoC %",
            type="float",
            default=50,
            min=0,
            max=100,
            help_text="SoC reported by the mock inverter.",
        ),
        ProviderConfigField(
            key="fail_soc_next",
            label="Fail next N SoC reads (test switch)",
            type="int",
            default=0,
            min=0,
            max=100,
            help_text="For tests: make the next SoC reads raise.",
        ),
    ]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = self.validate_config(config or {})
        self.soc: float = float(self.config.get("initial_soc", 50))
        self.fail_soc_next = int(self.config.get("fail_soc_next", 0))
        # simulated applied state
        self.export_blocked = False
        self.grid_charge_disabled = False
        self.write_calls: list[str] = []
        self.snapshot: dict[str, Any] | None = None
        self.fail_writes = False

    # -- reads --------------------------------------------------------------

    async def get_soc(self) -> float:
        if self.fail_soc_next > 0:
            self.fail_soc_next -= 1
            raise ProviderError("Mock inverter configured to fail SoC reads.")
        return self.soc

    async def inspect(self) -> dict[str, Any]:
        return {
            "soc": self.soc,
            "export_blocked": self.export_blocked,
            "grid_charge_disabled": self.grid_charge_disabled,
            "write_calls": list(self.write_calls),
        }

    async def snapshot_settings(self) -> dict[str, Any]:
        snap = {
            "soc": self.soc,
            "export_blocked": self.export_blocked,
            "grid_charge_disabled": self.grid_charge_disabled,
        }
        self.snapshot = snap
        return snap

    async def restore_snapshot(self) -> None:
        if self.snapshot is None:
            raise ProviderError("No snapshot saved for this inverter yet.")
        self.export_blocked = bool(self.snapshot["export_blocked"])
        self.grid_charge_disabled = bool(self.snapshot["grid_charge_disabled"])
        self.write_calls.append("restore_snapshot")

    # -- writes ---------------------------------------------------------------

    async def block_export(self) -> None:
        if self.fail_writes:
            raise ProviderError("Mock inverter configured to fail writes.")
        self.write_calls.append("block_export")
        self.export_blocked = True

    async def restore_export(self) -> None:
        if self.fail_writes:
            raise ProviderError("Mock inverter configured to fail writes.")
        self.write_calls.append("restore_export")
        self.export_blocked = False

    async def disable_grid_charge(self) -> None:
        if self.fail_writes:
            raise ProviderError("Mock inverter configured to fail writes.")
        self.write_calls.append("disable_grid_charge")
        self.grid_charge_disabled = True

    async def restore_grid_charge(self) -> None:
        if self.fail_writes:
            raise ProviderError("Mock inverter configured to fail writes.")
        self.write_calls.append("restore_grid_charge")
        self.grid_charge_disabled = False


__all__ = ["MockPriceProvider", "MockInverterProvider", "describe_fields"]
