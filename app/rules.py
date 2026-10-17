"""app/rules.py

Pure decision functions for the two automation rules. No I/O of any kind:
no network, no database, no inverter calls. The worker (phase 5) and the
preview (app/preview.py) both call these, so "what the rules would do" is
one shared implementation.

Inputs and params are plain dicts/dataclasses; outputs are dataclasses that
carry a plain-language reason, the parameter that caused each decision, and
the values compared - for the UI, logs and tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field

RULE_EXPORT = "export_control"
RULE_GRID_CHARGE = "grid_charge_control"


@dataclass(frozen=True)
class RuleInputs:
    """Normalised, provider-agnostic measurements for one moment."""

    soc: float | None                # battery SoC %
    current_buy_c: float | None      # grid buy price, c/kWh
    forecast_buy_c: list = field(default_factory=list)  # forward prices, c/kWh
    feed_in_c: float | None = None   # what the user earns on export, c/kWh

    def max_buy_in_window(self) -> float | None:
        prices = [p for p in ([self.current_buy_c] + list(self.forecast_buy_c)) if p is not None]
        return max(prices) if prices else None


@dataclass(frozen=True)
class LastState:
    """Last confirmed applied state (from automation_state.last_applied_json).

    ``None`` means "never applied / unknown" - treated as not-applied so the
    first differing state is written.
    """

    export_blocked: bool | None = None
    grid_charge_disabled: bool | None = None


@dataclass(frozen=True)
class Capabilities:
    """Which inverter operations this user's provider supports.

    Re-modeled here as a plain dataclass so rules stay I/O-free; mirrors
    app.providers.base.Capabilities.
    """

    block_export: bool = False
    restore_export: bool = False
    disable_grid_charge: bool = False
    restore_grid_charge: bool = False

    @classmethod
    def from_provider(cls, provider) -> "Capabilities":
        caps = getattr(provider, "capabilities", None)
        return cls(
            block_export=bool(getattr(caps, "block_export", False)),
            restore_export=bool(getattr(caps, "restore_export", False)),
            disable_grid_charge=bool(getattr(caps, "disable_grid_charge", False)),
            restore_grid_charge=bool(getattr(caps, "restore_grid_charge", False)),
        )

    @classmethod
    def fully_supported(cls) -> "Capabilities":
        """Assume every capability exists.

        Used by hypothetical paths (what-if, replay) where the real inverter's
        capability set is unknown or irrelevant; the UI shows unsupported
        only for the user's actual provider, not for imaginative scenarios.
        """
        return cls(
            block_export=True, restore_export=True,
            disable_grid_charge=True, restore_grid_charge=True,
        )


@dataclass(frozen=True)
class Decision:
    """One rule's decision for one moment."""

    rule: str
    action: str  # block_export | restore_export | disable_grid_charge | restore_grid_charge | none
    action_verb: str  # "block", "restore", "disable", "keep", "none" for prose
    apply: bool  # true when an inverter method should be called
    reason: str  # plain-language explanation
    parameter: str | None  # the parameter key that triggered the comparison
    compared: dict  # values compared, for UI/logs/tests
    unsupported: bool = False  # capability missing -> shown as unsupported, skipped


def _export_decision(params: dict, inputs: RuleInputs, last: LastState, caps: Capabilities) -> Decision:
    enabled = params.get("ENABLE_EXPORT_CONTROL", True)
    if not enabled:
        return Decision(
            rule=RULE_EXPORT, action="none", action_verb="none", apply=False,
            reason="Export control is switched off in Settings.", parameter=None,
            compared={"rule_enabled": False},
        )
    mrl = params.get("TARGET_MRL")
    if mrl is None:
        return Decision(
            rule=RULE_EXPORT, action="none", action_verb="none", apply=False,
            reason="TARGET_MRL is not set, so export control cannot be evaluated "
                   "and automation will not start.", parameter="TARGET_MRL",
            compared={"target_mrl": None},
        )
    if inputs.soc is None:
        return Decision(
            rule=RULE_EXPORT, action="none", action_verb="none", apply=False,
            reason="Battery SoC cannot be read this cycle, so no export decision is made.",
            parameter=None, compared={"soc": None},
        )

    hysteresis = params.get("SOC_HYSTERESIS", 0)
    if inputs.soc <= mrl:
        desired = True
        reason = (
            f"SoC {inputs.soc:g}% is at or below your {mrl:g}% floor, so exports would be blocked "
            f"(household loads can still run the battery down to "
            f"{params.get('HARDWARE_MIN_SOC', 0):g}%)."
        )
        parameter, compared = "TARGET_MRL", {"soc": inputs.soc, "target_mrl": mrl}
    elif inputs.soc >= mrl + hysteresis:
        desired = False
        reason = (
            f"SoC {inputs.soc:g}% is at or above your {mrl:g}% floor plus "
            f"{hysteresis:g}% hysteresis, so exports would return to normal."
        )
        parameter, compared = "SOC_HYSTERESIS", {
            "soc": inputs.soc, "target_mrl": mrl, "hysteresis": hysteresis,
        }
    else:
        return Decision(
            rule=RULE_EXPORT, action="none", action_verb="keep", apply=False,
            reason=(
                f"SoC {inputs.soc:g}% is inside the hysteresis band "
                f"({mrl:g}% to {mrl + hysteresis:g}%), so the last applied state is kept."
            ),
            parameter=None, compared={"soc": inputs.soc, "target_mrl": mrl, "hysteresis": hysteresis},
        )

    already = last.export_blocked == desired
    action = "none" if already else ("block_export" if desired else "restore_export")
    verb = "none" if already else ("block" if desired else "restore")
    if not already:
        supported = caps.block_export if desired else caps.restore_export
        if not supported:
            return Decision(
                rule=RULE_EXPORT,
                action="none" if desired == (last.export_blocked is True) else action,
                action_verb="none",
                apply=False,
                reason=reason + " This inverter does not support that capability, "
                       "so the rule is shown as unsupported and skipped.",
                parameter=parameter,
                compared=compared | {"capability_supported": False},
                unsupported=True,
            )
    return Decision(
        rule=RULE_EXPORT, action=action, action_verb=verb, apply=not already,
        reason=reason, parameter=parameter, compared=compared,
    )


def _grid_charge_decision(params: dict, inputs: RuleInputs, last: LastState, caps: Capabilities) -> Decision:
    enabled = params.get("ENABLE_GRID_CHARGE_CONTROL", True)
    if not enabled:
        return Decision(
            rule=RULE_GRID_CHARGE, action="none", action_verb="none", apply=False,
            reason="Grid-charge control is switched off in Settings.", parameter=None,
            compared={"rule_enabled": False},
        )
    threshold = params.get("PRICE_THRESHOLD_CENTS")
    if threshold is None:
        return Decision(
            rule=RULE_GRID_CHARGE, action="none", action_verb="none", apply=False,
            reason="PRICE_THRESHOLD_CENTS is not set, so grid-charge control cannot be "
                   "evaluated and automation will not start.",
            parameter="PRICE_THRESHOLD_CENTS", compared={"threshold": None},
        )
    window_max = inputs.max_buy_in_window()
    if window_max is None:
        return Decision(
            rule=RULE_GRID_CHARGE, action="none", action_verb="none", apply=False,
            reason="No price data this cycle, so no grid-charge decision is made.",
            parameter=None, compared={"threshold": threshold},
        )

    spike = window_max > threshold  # strictly above
    if spike:
        worst_source = "current price" if (
            inputs.current_buy_c is not None and window_max == inputs.current_buy_c
        ) else "highest forecast price"
        reason = (
            f"The {worst_source} {window_max:g}c is strictly above your "
            f"{threshold:g}c limit, so grid charging would be blocked."
        )
        parameter, compared = "PRICE_THRESHOLD_CENTS", {
            "max_price_in_window": window_max, "threshold": threshold,
        }
        desired = True
    else:
        reason = (
            f"The current and forecast prices are all at or below your "
            f"{threshold:g}c limit, so grid charging would return to normal."
        )
        parameter, compared = "PRICE_THRESHOLD_CENTS", {
            "max_price_in_window": window_max, "threshold": threshold,
        }
        desired = False

    already = last.grid_charge_disabled == desired
    action = "none" if already else ("disable_grid_charge" if desired else "restore_grid_charge")
    verb = "none" if already else ("disable" if desired else "restore")
    if not already:
        supported = caps.disable_grid_charge if desired else caps.restore_grid_charge
        if not supported:
            return Decision(
                rule=RULE_GRID_CHARGE,
                action="none" if desired == (last.grid_charge_disabled is True) else action,
                action_verb="none",
                apply=False,
                reason=reason + " This inverter does not support that capability, "
                       "so the rule is shown as unsupported and skipped.",
                parameter=parameter,
                compared=compared | {"capability_supported": False},
                unsupported=True,
            )
    return Decision(
        rule=RULE_GRID_CHARGE, action=action, action_verb=verb, apply=not already,
        reason=reason, parameter=parameter, compared=compared,
    )


def decide(
    params: dict,
    inputs: RuleInputs,
    last_state: LastState | None = None,
    caps: Capabilities | None = None,
) -> list[Decision]:
    """Run both rules independently; each outputs at most one write decision.

    Rules never raise for data problems: missing SoC/prices produce "no
    action" decisions with a reason. Failures belong to the worker.
    """
    last = last_state or LastState()
    caps = caps or Capabilities()
    return [
        _export_decision(params, inputs, last, caps),
        _grid_charge_decision(params, inputs, last, caps),
    ]


__all__ = [
    "RULE_EXPORT", "RULE_GRID_CHARGE",
    "RuleInputs", "LastState", "Capabilities", "Decision", "decide",
]
