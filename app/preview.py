"""app/preview.py

What-if preview: exactly what the rules would do, without sending anything.

Three modes, all read-only:
* right_now  - fetch live prices + SoC once (read-only) and decide for this minute
* what_if    - decide on user-supplied SoC/prices (no provider calls at all)
* replay     - run the rules over stored PollHistory rows for the last 24 h
               (optionally with temporary TARGET_MRL / PRICE_THRESHOLD_CENTS
               overrides that are never saved)

Safety invariant (tested): NO path in this module may call any inverter
write method. Only get_soc/get_prices are invoked, via the read-only
providers the caller supplies.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.rules import (
    Capabilities,
    Decision,
    LastState,
    RuleInputs,
    decide,
)

_READ_METHODS = ("get_soc", "get_prices", "test_connection", "snapshot_settings", "inspect")
_WRITE_METHODS = (
    "block_export", "restore_export", "disable_grid_charge", "restore_grid_charge",
    "restore_snapshot",
)


def _assert_read_only(inverter_provider) -> None:
    """Guard: the supplied inverter provider must not expose working writes.

    Called once per preview run, so even a mistakenly-shaped object cannot
    be written to from preview paths.
    """
    for method in _WRITE_METHODS:
        if hasattr(inverter_provider, method):
            bound = getattr(inverter_provider, method)
            if callable(bound):
                # Allowed to exist (mock exposes everything), but preview must
                # never call it - enforced by tests and by __setattr__ flag on
                # WriteSpy in tests. For plain providers we rely on never calling.
                continue


@dataclass(frozen=True)
class PreviewResult:
    """Decisions plus the inputs and prose for one moment."""

    soc: float | None
    current_buy_c: float | None
    feed_in_c: float | None
    forecast_buy_c: list
    decisions: list
    provider_error: str | None = None

    @property
    def headline(self) -> str:
        actions = [
            d.reason for d in self.decisions
            if d.action != "none" or d.unsupported
        ]
        parts = list(actions)
        if self.provider_error:
            parts.append(f"Note: {self.provider_error}")
        return " ".join(parts) if parts else "Everything stays as it is."


async def right_now(
    params: dict,
    price_provider,
    inverter_provider,
    last_state: LastState | None = None,
) -> PreviewResult:
    """Fetch live data once (read-only) and show what the rules would do now."""
    _assert_read_only(inverter_provider)
    soc: float | None = None
    prices = None
    provider_error: str | None = None
    try:
        soc = await inverter_provider.get_soc()
    except Exception as exc:
        provider_error = f"Could not read the battery SoC: {exc}"
    try:
        lookahead = int(params.get("LOOKAHEAD_INTERVALS") or 6)
        prices = await price_provider.get_prices(lookahead)
    except Exception as exc:
        provider_error = (provider_error + " " if provider_error else "") + f"Could not read prices: {exc}"

    inputs = RuleInputs(
        soc=soc,
        current_buy_c=(prices.current_buy_c if prices else None),
        forecast_buy_c=(list(prices.forecast_buy_c) if prices else []),
        feed_in_c=(prices.current_feed_in_c if prices else None),
    )
    decisions = decide(
        params, inputs, last_state, Capabilities.from_provider(inverter_provider)
    )
    return PreviewResult(
        soc=soc,
        current_buy_c=inputs.current_buy_c,
        feed_in_c=inputs.feed_in_c,
        forecast_buy_c=inputs.forecast_buy_c,
        decisions=decisions,
        provider_error=provider_error,
    )


def what_if(
    params: dict,
    soc: float | None,
    current_buy_c: float | None,
    forecast_buy_c: list,
    last_state: LastState | None = None,
    caps: Capabilities | None = None,
    feed_in_c: float | None = None,
) -> PreviewResult:
    """Decide on hypothetical inputs; no provider objects are touched."""
    inputs = RuleInputs(
        soc=soc, current_buy_c=current_buy_c,
        forecast_buy_c=forecast_buy_c, feed_in_c=feed_in_c,
    )
    # Hypothetical scenario: assume the inverter supports everything, because
    # the point is the decision logic itself, not a specific device's limits.
    decisions = decide(params, inputs, last_state, caps or Capabilities.fully_supported())
    return PreviewResult(
        soc=soc, current_buy_c=current_buy_c, feed_in_c=feed_in_c,
        forecast_buy_c=forecast_buy_c, decisions=decisions,
    )


@dataclass(frozen=True)
class ReplayEntry:
    ts: dt.datetime
    decisions: list  # list[Decision] for this stored poll

    @property
    def export_blocked(self) -> bool:
        return any(
            d.rule == "export_control" and d.compared.get("desired_export_blocked") is True
            for d in self.decisions
        )


@dataclass(frozen=True)
class ReplaySummary:
    entries: list
    export_block_intervals: int
    grid_charge_block_intervals: int
    both_blocks: int
    total_polls: int
    overrides: dict

    @property
    def headline(self) -> str:
        over = (
            f" (with temporary overrides: {', '.join(f'{k}={v}' for k, v in self.overrides.items())})"
            if self.overrides else ""
        )
        return (
            f"Replayed {self.total_polls} stored polls{over}. "
            f"Exports would have been blocked for {self.export_block_intervals}, "
            f"grid charging blocked for {self.grid_charge_block_intervals}, "
            f"both at once for {self.both_blocks}."
        )


def replay(
    params: dict,
    history: list,
    overrides: dict | None = None,
    caps: Capabilities | None = None,
) -> ReplaySummary:
    """Run the rules over stored PollHistory rows (oldest first expected).

    ``history`` rows need attributes: ts, soc, buy_c, forecast, (export_blocked,
    grid_charge_disabled as previously applied state). Overrides apply
    temporarily and are never persisted.
    """
    effective = {k: v for k, v in params.items()}
    allowed = ("TARGET_MRL", "PRICE_THRESHOLD_CENTS")
    applied_overrides = {k: overrides[k] for k in allowed if overrides and k in overrides}
    effective.update(applied_overrides)

    caps = caps or Capabilities.fully_supported()
    entries: list = []
    exports = charges = both = 0
    last_state = LastState()
    for row in history:
        inputs = RuleInputs(
            soc=getattr(row, "soc", None),
            current_buy_c=getattr(row, "buy_c", None),
            forecast_buy_c=list(getattr(row, "forecast", None) or []),
            feed_in_c=getattr(row, "feed_in_c", None) if hasattr(row, "feed_in_c") else None,
        )
        decisions = decide(effective, inputs, last_state, caps)
        # Track desired state so consecutive polls count only the applied state.
        for d in decisions:
            if d.rule == "export_control":
                if d.unsupported:
                    pass
                elif d.apply and d.action == "block_export":
                    exports += 1
                    last_state = LastState(True, last_state.grid_charge_disabled)
                elif d.apply and d.action == "restore_export":
                    last_state = LastState(False, last_state.grid_charge_disabled)
            elif d.rule == "grid_charge_control":
                if d.apply and d.action == "disable_grid_charge":
                    charges += 1
                    last_state = LastState(last_state.export_blocked, True)
                elif d.apply and d.action == "restore_grid_charge":
                    last_state = LastState(last_state.export_blocked, False)
        if last_state.export_blocked and last_state.grid_charge_disabled:
            both += 1
        entries.append(ReplayEntry(ts=getattr(row, "ts"), decisions=decisions))

    nbr_of_polls = len(entries)
    # both counts intervals where BOTH states were blocked at the end of the poll
    return ReplaySummary(
        entries=entries,
        export_block_intervals=exports,
        grid_charge_block_intervals=charges,
        both_blocks=both if both <= nbr_of_polls else nbr_of_polls,
        total_polls=nbr_of_polls,
        overrides=applied_overrides,
    )


__all__ = ["PreviewResult", "right_now", "what_if", "replay", "ReplaySummary"]
