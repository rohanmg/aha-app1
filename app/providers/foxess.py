"""app/providers/foxess.py

FoxESS inverter adapter (verified against the official FoxESS Open API doc,
https://www.foxesscloud.com/public/i18n/en/OpenApiDocument.html, and the
community client at github.com/TonyM1958/FoxESS-Cloud on 2026-10-16).

Endpoints and conventions:
* Base URL: https://www.foxesscloud.com, API paths under ``/op/``.
* Auth headers on every request (personal API key, created in FoxESS Cloud
  under User Profile -> API Management)::

      token: <API key>
      timestamp: <unix time in milliseconds>
      signature: lowercase MD5 of  path + "\\r\\n" + token + "\\r\\n" + timestamp

* ``POST /op/v1/device/real/query`` body ``{"sns": ["<SN>"]}`` -> ``result``
  is a list; ``result[0]["datas"]`` holds ``{"variable", "value", "unit"}``
  entries and the battery state of charge is the ``SoC`` variable.
* ``POST /op/v0/device/setting/get`` body ``{"sn", "key"}`` -> ``result.value``.
* ``POST /op/v0/device/setting/set`` body ``{"sn", "key", "value"}`` (value
  stringified). Documented export-limit key: ``ExportLimit``.
* Errors arrive as ``{"errno": <n>, "msg": ...}`` even on HTTP 200:
  ``40400`` = request frequency too high, ``44096`` = setting not updatable
  while a charge schedule is active.

Rate limits (official doc): 1,440 calls per inverter per account per day,
query interfaces at most once per second, update interfaces at most once
every two seconds. This adapter reads once per automation cycle and writes
only when the rules change state; it never retries writes.

Command mapping: the setting keys/values used for the two writes are
configurable per user (``FOXESS_SETTING_*``), so different models/firmware
can be supported without code changes. VERIFY THE DEFAULTS AGAINST YOUR
INVERTER before leaving dry-run mode.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import time
from typing import Any

import httpx

from app.providers.base import (
    Capabilities,
    InverterProvider,
    ProviderAdapter,
    ProviderConfigField,
    ProviderError,
)
from app.providers.registry import register

BASE_URL = "https://www.foxesscloud.com"
_TIMEOUT_SECONDS = 30.0
_USER_AGENT = "solar-automation-app/1.0 (self-hosted automation; contact your instance admin)"

# Documented errno values (the API returns them in a 200 response).
_ERRNO_RATE_LIMIT = 40400
_ERRNO_SCHEDULE_ACTIVE = 44096


@register
class FoxESSInverterProvider(InverterProvider, ProviderAdapter):
    provider_name = "foxess"
    kind = "inverter"
    label = "FoxESS inverter (cloud API)"
    config_schema = [
        ProviderConfigField(
            key="api_key",
            label="FoxESS API key",
            secret=True,
            required=True,
            help_text="Personal API key used to authenticate device reads and writes.",
            hint="FoxESS Cloud → User Profile → API Management → generate an API key.",
            placeholder="YOUR_FOXESS_API_KEY",
        ),
        ProviderConfigField(
            key="device_sn",
            label="Inverter serial number (SN)",
            required=True,
            help_text="Serial number of the inverter this account controls.",
            hint="FoxESS Cloud → Device → Inverter (deviceSN). Copy it exactly.",
            placeholder="YOUR_DEVICE_SN",
        ),
        ProviderConfigField(
            key="FOXESS_SETTING_KEY_BLOCK_EXPORT",
            label="Setting key used to block export",
            default="ExportLimit",
            max=64,
            placeholder="ExportLimit",
            help_text="Inverter setting key written to block exports. "
            "Default 'ExportLimit' is the key documented by FoxESS. "
            "VERIFY AGAINST YOUR INVERTER.",
        ),
        ProviderConfigField(
            key="FOXESS_SETTING_VALUE_BLOCK_EXPORT",
            label="Value that blocks export",
            default="0",
            max=32,
            placeholder="0",
            help_text="Value written to the export key to block exports "
            "(0 = no export). VERIFY AGAINST YOUR INVERTER.",
        ),
        ProviderConfigField(
            key="FOXESS_SETTING_KEY_DISABLE_GRID_CHARGE",
            label="Setting key used to disable grid charging",
            default=None,
            max=64,
            placeholder="YOUR_SETTING_KEY",
            help_text="Setting key that disables grid charging / timed charge. "
            "Leave empty if your model has no such setting - the rule is then "
            "shown as unsupported and skipped. VERIFY AGAINST YOUR INVERTER.",
        ),
        ProviderConfigField(
            key="FOXESS_SETTING_VALUE_DISABLE_GRID_CHARGE",
            label="Value that disables grid charging",
            default=None,
            max=32,
            placeholder="YOUR_VALUE",
            help_text="Value written to the grid-charge key to disable charging. "
            "VERIFY AGAINST YOUR INVERTER.",
        ),
    ]

    def __init__(self, config: dict[str, Any]) -> None:
        validated = self.validate_config(config or {})
        # Schema defaults first, validated values on top (validate_config omits
        # empty optional fields, so defaults must be applied here).
        self.config: dict[str, Any] = {f.key: f.default for f in self.config_schema}
        self.config.update(validated)
        self.base_url = BASE_URL  # overridable in tests
        self._originals: dict[str, str] = {}  # key -> value before our write
        self._snapshot: dict[str, str] | None = None
        self._write_calls: list[str] = []  # audit trail for tests/diagnostics

        block_key = self._map_key("FOXESS_SETTING_KEY_BLOCK_EXPORT")
        block_val = self._map_key("FOXESS_SETTING_VALUE_BLOCK_EXPORT")
        charge_key = self._map_key("FOXESS_SETTING_KEY_DISABLE_GRID_CHARGE")
        charge_val = self._map_key("FOXESS_SETTING_VALUE_DISABLE_GRID_CHARGE")
        self.capabilities = Capabilities(
            read_soc=True,
            block_export=bool(block_key and block_val),
            restore_export=bool(block_key and block_val),
            disable_grid_charge=bool(charge_key and charge_val),
            restore_grid_charge=bool(charge_key and charge_val),
            snapshot_settings=True,
            restore_snapshot=True,
            inspect=True,
        )

    # -- configuration helpers ---------------------------------------------

    def _map_key(self, key: str) -> str:
        value = self.config.get(key)
        return str(value).strip() if value not in (None, "") else ""

    def _sn(self) -> str:
        return str(self.config["device_sn"])

    # -- HTTP plumbing -------------------------------------------------------

    def _headers(self, path: str) -> dict[str, str]:
        token = str(self.config["api_key"])
        timestamp = str(round(time.time() * 1000))
        signature = hashlib.md5(
            f"{path}\r\n{token}\r\n{timestamp}".encode("utf-8")
        ).hexdigest()
        return {
            "Token": token,
            "Timestamp": timestamp,
            "Signature": signature,
            "Lang": "en",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }

    def _error(self, status: int, body: str) -> ProviderError:
        if status in (401, 403):
            return ProviderError(
                "FoxESS rejected the API key (401 unauthorized). Check that the "
                "key is current and pasted without extra spaces."
            )
        if status == 404:
            return ProviderError(
                "FoxESS could not find that endpoint or device (404). Check the "
                "inverter serial number, exactly as shown in FoxESS Cloud."
            )
        if status == 429:
            return ProviderError(
                "FoxESS rate limit reached (429). This instance reads once per "
                "cycle; try a longer poll interval."
            )
        snippet = (body or "").strip()[:200]
        return ProviderError(f"FoxESS API returned HTTP {status}. {snippet}")

    def _errno_error(self, errno: int, msg: str) -> ProviderError:
        if errno == _ERRNO_RATE_LIMIT:
            return ProviderError(
                "FoxESS rate limit reached (error 40400). This instance reads "
                "once per cycle; try a longer poll interval."
            )
        if errno == _ERRNO_SCHEDULE_ACTIVE:
            return ProviderError(
                "FoxESS will not change this setting while a charge schedule is "
                "active (error 44096). Pause or remove the schedule in FoxESS "
                "Cloud and try again."
            )
        detail = (msg or "").strip()[:200]
        return ProviderError(f"FoxESS API returned error {errno}. {detail}")

    async def _call(self, path: str, *, body: dict[str, Any] | None = None) -> Any:
        """POST (body given) or GET one API path; return the parsed ``result``."""
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                if body is None:
                    response = await client.get(url, headers=self._headers(path))
                else:
                    response = await client.post(url, json=body, headers=self._headers(path))
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach the FoxESS API: {exc}") from exc

        if response.status_code != 200:
            raise self._error(response.status_code, response.text)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("FoxESS returned a response that is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ProviderError("FoxESS returned an unexpected response shape.")
        errno = payload.get("errno")
        if isinstance(errno, int) and errno != 0:
            raise self._errno_error(errno, str(payload.get("msg") or payload.get("exception") or ""))
        return payload.get("result")

    # -- reads ---------------------------------------------------------------

    def _extract_soc(self, result: Any) -> float:
        """Find the SoC in the real-time query result (shapes vary by version)."""
        candidates: list[Any] = []
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, dict):
                candidates.append(first.get("soc"))
                datas = first.get("datas")
            else:
                datas = None
        elif isinstance(result, dict):
            candidates.append(result.get("soc"))
            datas = result.get("datas")
        else:
            datas = None
        if isinstance(datas, dict):
            candidates.append(datas.get("soc"))
            candidates.append(datas.get("SoC"))
        elif isinstance(datas, list):
            for entry in datas:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("variable", "")).lower() == "soc":
                    candidates.append(entry.get("value"))
        for candidate in candidates:
            try:
                return float(candidate)
            except (TypeError, ValueError):
                continue
        raise ProviderError(
            "FoxESS returned no battery state of charge (SoC) reading this "
            "cycle, so automation decisions cannot be made."
        )

    async def get_soc(self) -> float:
        result = await self._call("/op/v1/device/real/query", body={"sns": [self._sn()]})
        return self._extract_soc(result)

    async def _get_setting(self, key: str) -> str:
        result = await self._call(
            "/op/v0/device/setting/get", body={"sn": self._sn(), "key": key}
        )
        if isinstance(result, dict) and "value" in result:
            return str(result["value"])
        if isinstance(result, (str, int, float, bool)):
            return str(result)
        raise ProviderError(
            f"FoxESS returned no value for setting '{key}', so its previous "
            "state cannot be confirmed."
        )

    async def _set_setting(self, key: str, value: Any) -> None:
        await self._call(
            "/op/v0/device/setting/set",
            body={"sn": self._sn(), "key": key, "value": str(value)},
        )
        self._write_calls.append(f"{key}={value}")

    # -- writes --------------------------------------------------------------

    async def _block(self, key_param: str, value_param: str, what: str) -> None:
        key = self._map_key(key_param)
        block_value = self._map_key(value_param)
        if not key or not block_value:
            raise ProviderError(
                f"No setting key/value is configured to {what}; set it in "
                "Settings (Inverter command mapping) after verifying it "
                "against your inverter."
            )
        original = await self._get_setting(key)
        self._originals.setdefault(key, original)
        if original == block_value:
            return  # already blocked; nothing to write
        await self._set_setting(key, block_value)

    async def _restore(self, key_param: str, value_param: str, what: str) -> None:
        key = self._map_key(key_param)
        block_value = self._map_key(value_param)
        if not key or not block_value:
            raise ProviderError(
                f"No setting key/value is configured to {what}; set it in "
                "Settings (Inverter command mapping) after verifying it "
                "against your inverter."
            )
        original = self._originals.get(key)
        if original is None:
            current = await self._get_setting(key)
            if current != block_value:
                return  # not blocked right now; nothing to restore
            raise ProviderError(
                f"The value of setting '{key}' before automation blocked it is "
                "unknown (e.g. after a restart). Use 'Restore original "
                "settings' from a saved snapshot, or set it manually in "
                "FoxESS Cloud."
            )
        await self._set_setting(key, original)

    async def block_export(self) -> None:
        await self._block(
            "FOXESS_SETTING_KEY_BLOCK_EXPORT",
            "FOXESS_SETTING_VALUE_BLOCK_EXPORT",
            "block exports",
        )

    async def restore_export(self) -> None:
        await self._restore(
            "FOXESS_SETTING_KEY_BLOCK_EXPORT",
            "FOXESS_SETTING_VALUE_BLOCK_EXPORT",
            "restore exports",
        )

    async def disable_grid_charge(self) -> None:
        await self._block(
            "FOXESS_SETTING_KEY_DISABLE_GRID_CHARGE",
            "FOXESS_SETTING_VALUE_DISABLE_GRID_CHARGE",
            "disable grid charging",
        )

    async def restore_grid_charge(self) -> None:
        await self._restore(
            "FOXESS_SETTING_KEY_DISABLE_GRID_CHARGE",
            "FOXESS_SETTING_VALUE_DISABLE_GRID_CHARGE",
            "restore grid charging",
        )

    # -- snapshot / restore ---------------------------------------------------

    async def snapshot_settings(self) -> dict[str, Any]:
        snap: dict[str, str] = {}
        for key in {self._map_key("FOXESS_SETTING_KEY_BLOCK_EXPORT"),
                    self._map_key("FOXESS_SETTING_KEY_DISABLE_GRID_CHARGE")} - {""}:
            snap[key] = await self._get_setting(key)
        self._snapshot = snap
        self._originals.update(snap)
        return dict(snap)

    async def restore_snapshot(self) -> None:
        if not self._snapshot:
            raise ProviderError("No settings snapshot has been taken yet.")
        for key, value in self._snapshot.items():
            await self._set_setting(key, value)

    def load_snapshot(self, values: dict[str, Any]) -> None:
        """Reload a snapshot captured earlier (e.g. after a worker restart)."""
        self._snapshot = {str(k): str(v) for k, v in values.items()}
        self._originals.update(self._snapshot)

    # -- inspect (read-only) ---------------------------------------------------

    async def inspect(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            out["device"] = await self._call("/op/v1/device/detail", body=None)
        except ProviderError as exc:
            out["device"] = {"error": str(exc)}
        except Exception:  # noqa: BLE001 - inspect must never crash the CLI
            out["device"] = {"error": "unexpected error while reading device detail"}
        try:
            result = await self._call("/op/v1/device/real/query", body={"sns": [self._sn()]})
            out["soc"] = self._extract_soc(result)
            out["realtime"] = self._extract_all_vars(result)
        except ProviderError as exc:
            out["realtime"] = {"error": str(exc)}
        settings: dict[str, Any] = {}
        for key in {self._map_key("FOXESS_SETTING_KEY_BLOCK_EXPORT"),
                    self._map_key("FOXESS_SETTING_KEY_DISABLE_GRID_CHARGE")} - {""}:
            try:
                settings[key] = await self._get_setting(key)
            except ProviderError as exc:
                settings[key] = {"error": str(exc)}
        out["settings"] = settings
        out["capabilities"] = {
            "block_export": self.capabilities.block_export,
            "disable_grid_charge": self.capabilities.disable_grid_charge,
        }
        out["read_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        return out

    def _extract_all_vars(self, result: Any) -> dict[str, Any]:
        datas: Any = None
        if isinstance(result, list) and result and isinstance(result[0], dict):
            datas = result[0].get("datas")
        elif isinstance(result, dict):
            datas = result.get("datas")
        out: dict[str, Any] = {}
        if isinstance(datas, dict):
            out.update({str(k): v for k, v in datas.items()})
        elif isinstance(datas, list):
            for entry in datas:
                if isinstance(entry, dict) and "variable" in entry:
                    out[str(entry["variable"])] = entry.get("value")
        return out


__all__ = ["FoxESSInverterProvider"]
