"""app/providers/_template.py

Template for writing a new adapter. Copy this file, rename it, adjust the
schema and implement the methods. See docs/WRITING_AN_ADAPTER.md (added in a
later phase) for the full walkthrough.
"""
from __future__ import annotations

from typing import Any

from app.providers.base import (
    Capabilities,
    PriceProvider,
    PriceSnapshot,
    ProviderAdapter,
    ProviderConfigField,
    ProviderError,
)
from app.providers.registry import register


@register
class TemplatePriceProvider(PriceProvider, ProviderAdapter):
    provider_name = "template"
    kind = "price"
    label = "Template price provider (copy me)"
    config_schema = [
        ProviderConfigField(
            key="api_token",
            label="API token",
            secret=True,
            required=True,
            help_text="Token used to authenticate against the vendor API.",
            hint="Create it in the vendor's developer portal.",
            placeholder="YOUR_API_TOKEN",
        ),
        ProviderConfigField(
            key="site_id",
            label="Site ID",
            required=True,
            help_text="Identifier of the site whose prices are fetched.",
            hint="Shown on the vendor dashboard next to your address.",
            placeholder="YOUR_SITE_ID",
        ),
    ]

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = self.validate_config(config)

    async def get_prices(self, lookahead: int) -> PriceSnapshot:
        # TODO(your-adapter): call the vendor API and normalise to cents/kWh.
        # Feed-in must be flipped if the vendor reports it sign-inverted:
        # a positive current_feed_in_c always means the user EARNS.
        raise ProviderError(
            "Template adapter is a starting point; implement get_prices() "
            "against the vendor's official docs before use."
        )


@register
class TemplateInverterProvider(ProviderAdapter):
    provider_name = "template"
    kind = "inverter"
    label = "Template inverter (copy me)"
    capabilities = Capabilities()  # declare only what you truly implement
    config_schema = [
        ProviderConfigField(
            key="api_key",
            label="API key",
            secret=True,
            required=True,
            hint="In the vendor app under Account > API.",
            placeholder="YOUR_API_KEY",
        ),
        ProviderConfigField(
            key="device_serial",
            label="Inverter serial number",
            required=True,
            hint="Printed on the inverter and shown in the vendor app.",
            placeholder="YOUR_INVERTER_SERIAL",
        ),
    ]

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = self.validate_config(config)

    async def get_soc(self) -> float:
        raise ProviderError("Template adapter: implement get_soc() against official docs.")

    async def block_export(self) -> None:
        raise ProviderError("Template adapter: implement block_export() against official docs.")

    async def restore_export(self) -> None:
        raise ProviderError("Template adapter: implement restore_export() against official docs.")

    async def disable_grid_charge(self) -> None:
        raise ProviderError(
            "Template adapter: implement disable_grid_charge() against official docs."
        )

    async def restore_grid_charge(self) -> None:
        raise ProviderError(
            "Template adapter: implement restore_grid_charge() against official docs."
        )
