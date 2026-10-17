"""app/providers/amber.py

Amber Electricity price adapter (verified against Amber's OpenAPI spec,
https://app.amber.com.au/swagger.json, on 2026-10-16).

Endpoints and conventions:
* Base URL: https://api.amber.com.au/v1
* Auth: ``Authorization: Bearer <API token>`` (personal API keys are created
  in the Amber web app under Settings -> Developer Mode -> Generate API Key).
* ``GET /sites/{site_id}/prices/current?next=NN`` with optional
  ``resolution=5|30`` (default: the site's billing interval). The response is
  a JSON ARRAY of interval records across the site's channels - positions are
  not stable, so records are grouped by ``channelType``.
* Records carry ``perKwh`` (c/kWh incl. GST), ``duration`` (minutes, 5/15/30),
  ``startTime`` and a ``type`` of Actual/Current/ForecastInterval.
* Feed-in sign: Amber reports the ``feedIn`` channel sign-inverted - a raw
  negative perKWh means the user EARNS that amount, a raw positive value
  means the user PAYS (e.g. a negative feed-in tariff). We normalise so that
  a positive ``current_feed_in_c`` always means earning, per the provider
  contract in app/providers/base.py.

Rate limits are advertised in RateLimit-* response headers; this adapter
does one read per automation cycle, well inside the announced ~50 calls /
5 minutes, and never retries writes (it has none).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from app.providers.base import (
    PriceProvider,
    PriceSnapshot,
    ProviderAdapter,
    ProviderConfigField,
    ProviderError,
)
from app.providers.registry import register

BASE_URL = "https://api.amber.com.au/v1"
_TIMEOUT_SECONDS = 15.0


@register
class AmberPriceProvider(PriceProvider, ProviderAdapter):
    provider_name = "amber"
    kind = "price"
    label = "Amber Electricity (live prices)"
    config_schema = [
        ProviderConfigField(
            key="api_token",
            label="Amber API token",
            secret=True,
            required=True,
            help_text="Personal API token used to authenticate price reads.",
            hint="Amber web app → Settings → Developer Mode → Generate API Key.",
            placeholder="YOUR_AMBER_API_TOKEN",
        ),
        ProviderConfigField(
            key="site_id",
            label="Amber site ID",
            required=True,
            help_text="Identifier of your site - all prices are fetched for it.",
            hint="Returned by the /sites endpoint of the API for your account, "
            "or visible in your Amber account. Copy it exactly.",
            placeholder="YOUR_SITE_ID",
        ),
        ProviderConfigField(
            key="resolution",
            label="Interval resolution (minutes)",
            type="choice",
            choices=["30", "5"],
            default="30",
            help_text="Leave at the default unless you know your site bills in 5-minute intervals.",
        ),
    ]

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = self.validate_config(config)
        self.base_url = BASE_URL  # overridable in tests

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config['api_token']}"}

    def _error(self, status: int, body: str) -> ProviderError:
        if status in (401, 403):
            return ProviderError(
                "Amber rejected the API token (401 unauthorized). Check that the "
                "token is current and pasted without extra spaces."
            )
        if status == 404:
            return ProviderError(
                "Amber could not find that site ID (404 not found). The site ID "
                "must match the exact value from your Amber account."
            )
        if status == 429:
            return ProviderError(
                "Amber rate limit reached (429). The automation reads once per "
                "cycle; try a longer poll interval."
            )
        snippet = (body or "").strip()[:200]
        return ProviderError(f"Amber API returned HTTP {status}. {snippet}")

    async def get_prices(self, lookahead: int) -> PriceSnapshot:
        lookahead = max(1, int(lookahead))
        url = f"{self.base_url}/sites/{self.config['site_id']}/prices/current"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    url,
                    params={"next": lookahead, "resolution": str(self.config.get("resolution", "30"))},
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach the Amber API: {exc}") from exc

        if response.status_code != 200:
            raise self._error(response.status_code, response.text)
        try:
            records = response.json()
        except ValueError as exc:
            raise ProviderError("Amber returned a response that is not valid JSON.") from exc
        if not isinstance(records, list):
            raise ProviderError("Amber returned an unexpected response shape (not a list).")
        return self._parse(records, lookahead)

    def _parse(self, records: list[dict[str, Any]], lookahead: int) -> PriceSnapshot:
        """Group records by channel; pick the current interval and forecasts.

        Amber returns records across channels in an array whose order is not
        stable, and the current interval may also appear in the forecast list,
        so both are selected by ``type`` rather than by position:
        "CurrentInterval" is the present price, "ForecastInterval" records
        are future prices, "ActualInterval" records are the recent past.
        """
        current_buy: float | None = None
        raw_feed_in: float | None = None
        forecast: list[tuple[str, float]] = []
        interval_minutes = 30

        for record in records:
            channel = record.get("channelType")
            per_kwh = record.get("perKwh")
            if channel not in ("general", "feedIn") or per_kwh is None:
                continue
            try:
                per_kwh = float(per_kwh)
            except (TypeError, ValueError):
                continue
            duration = record.get("duration")
            if duration:
                try:
                    interval_minutes = int(duration)
                except (TypeError, ValueError):
                    pass

            record_type = str(record.get("type", ""))
            lowered = record_type.lower()
            start = str(record.get("startTime") or record.get("nemTime") or "")

            if channel == "general":
                if "forecast" in lowered:
                    forecast.append((start, per_kwh))
                elif "current" in lowered or current_buy is None:
                    # Prefer an explicit CurrentInterval; fall back to the
                    # first non-forecast general record available.
                    if current_buy is None or "current" in lowered:
                        current_buy = per_kwh
            else:  # feedIn - sign-inverted per the provider contract
                if "forecast" in lowered:
                    continue
                if raw_feed_in is None or "current" in lowered:
                    raw_feed_in = per_kwh

        if current_buy is None:
            raise ProviderError(
                "Amber returned no current general-channel price, so no decision "
                "inputs can be produced this cycle."
            )
        feed_in = None if raw_feed_in is None else -raw_feed_in  # normalise sign

        # Order forecasts by start time and cut to the requested lookahead.
        def _sort_key(pair: tuple[str, float]) -> float:
            try:
                return dt.datetime.fromisoformat(pair[0]).timestamp()
            except ValueError:
                return 0.0

        forecast.sort(key=_sort_key)
        forecast_buy_c = [price for _start, price in forecast[:lookahead]]

        return PriceSnapshot(
            current_buy_c=float(current_buy),
            current_feed_in_c=float(feed_in if feed_in is not None else 0.0),
            forecast_buy_c=forecast_buy_c,
            interval_minutes=interval_minutes,
            fetched_at=dt.datetime.now(dt.timezone.utc),
            raw={"amber_records": len(records)},
        )
