"""app/worker.py

The per-user automation loop. One cycle = read prices + SoC, run the shared
rules (app/rules.py - the same implementation the preview uses), apply the
decided writes to the inverter, and record everything.

Guarantees:
* Strict per-user isolation: every query and write is user-scoped.
* Dry-run (DRY_RUN parameter, default ON) never touches the inverter: the
  decisions are logged as "would ..." rows instead. The simulated state is
  still tracked so a dry run behaves exactly like a live run would (the
  AutomationState.dry_run_effective flag records which mode was used).
* Every cycle is recorded as a PollHistory row (feeds replay), and every
  decision/write/error/notification as an ActionLog row (audit trail).
* Provider failures never crash the loop: they are logged, counted per user,
  and escalated via notifications (repeated_provider_failure after 3 failed
  cycles, stopped_unexpectedly + automation stopped after 5).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import notify, params, state
from app.models import ActionLog, AutomationState, PollHistory, User
from app.providers.base import ProviderError
from app.providers import registry
from app.rules import Decision, LastState, RuleInputs, decide
from app.rules import Capabilities as RuleCapabilities

log = logging.getLogger(__name__)

FAILURE_WARN_AT = 3   # consecutive failed cycles -> repeated_provider_failure
FAILURE_STOP_AT = 5   # consecutive failed cycles -> stop automation + notify

# Process-local failure bookkeeping (per user). Deliberately in-memory: a
# worker restart starting from a clean streak is safe (failures just count up
# again), and it keeps the DB free of bookkeeping rows.
_failures: dict[int, int] = {}
_soc_unreadable_notified: set[int] = set()


def reset_failures(user_id: int | None = None) -> None:
    """Test/ops helper: clear the failure streak bookkeeping."""
    if user_id is None:
        _failures.clear()
        _soc_unreadable_notified.clear()
    else:
        _failures.pop(user_id, None)
        _soc_unreadable_notified.discard(user_id)


@dataclass
class CycleOutcome:
    user_id: int
    ran: bool
    skipped_reason: str | None = None
    dry_run: bool = True
    decisions: list[Decision] = field(default_factory=list)
    actions_taken: list[str] = field(default_factory=list)
    error: str | None = None
    notified: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Provider construction
# ---------------------------------------------------------------------------

def build_providers(session: Session, user: User) -> tuple[Any, Any, dict[str, Any]]:
    """Instantiate the user's chosen providers from (encrypted) credentials.

    Command-mapping overrides (FOXESS_SETTING_* parameters) live in the user's
    settings and are merged over the stored provider credentials here, so one
    adapter supports different inverter models/firmware without code changes.
    """
    registry.import_all_adapters()  # CLI and tests may not have the web startup
    values = state.resolve_user_params(session, user.id)
    out = []
    for kind, name in (
        ("price", values.get("PRICE_PROVIDER") or "mock"),
        ("inverter", values.get("INVERTER_PROVIDER") or "mock"),
    ):
        try:
            cls = (
                registry.get_price_adapter(name)
                if kind == "price"
                else registry.get_inverter_adapter(name)
            )
        except registry.UnknownProviderError as exc:
            raise ProviderError(str(exc)) from exc
        config = dict(state.get_credentials(session, user.id, name) or {})
        for f in cls.fields():  # type: ignore[attr-defined]
            if f.key in params.COMMAND_MAP_PARAMS:
                override = values.get(f.key)
                if override not in (None, ""):
                    config[f.key] = override
        out.append(cls(config))
    return out[0], out[1], values


# ---------------------------------------------------------------------------
# One automation cycle
# ---------------------------------------------------------------------------

def _log(session: Session, user_id: int, kind: str, message: str,
         rule: str | None = None, dry_run: bool = False,
         detail: dict[str, Any] | None = None) -> None:
    session.add(
        ActionLog(
            user_id=user_id, kind=kind, rule=rule, dry_run=dry_run,
            message=message,
            detail_json=json.dumps(detail) if detail else None,
        )
    )


def _last_state(state_row: AutomationState) -> LastState:
    try:
        applied = json.loads(state_row.last_applied_json or "{}")
    except (TypeError, ValueError):
        applied = {}
    return LastState(
        export_blocked=applied.get("export_blocked"),
        grid_charge_disabled=applied.get("grid_charge_disabled"),
    )


def _save_last_state(session: Session, state_row: AutomationState, last: LastState) -> None:
    state_row.last_applied_json = json.dumps({
        "export_blocked": last.export_blocked,
        "grid_charge_disabled": last.grid_charge_disabled,
    })


def _after_action(last: LastState, action: str) -> LastState:
    if action == "block_export":
        return LastState(True, last.grid_charge_disabled)
    if action == "restore_export":
        return LastState(False, last.grid_charge_disabled)
    if action == "disable_grid_charge":
        return LastState(last.export_blocked, True)
    if action == "restore_grid_charge":
        return LastState(last.export_blocked, False)
    return last


async def _notify(session: Session, user: User, event: str, title: str, message: str,
                  notifier: Callable[..., Awaitable[dict[str, Any]]] | None,
                  outcome: CycleOutcome) -> None:
    try:
        if notifier is not None:
            await notifier(session, user, event, title, message)
        else:
            await notify.emit(session, user, event, title, message)
        outcome.notified.append(event)
    except Exception:  # noqa: BLE001 - notifications must never kill the loop
        log.exception("notification %s failed for user %s", event, user.id)


async def run_cycle(
    session: Session,
    user: User,
    *,
    price_provider: Any | None = None,
    inverter_provider: Any | None = None,
    notifier: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> CycleOutcome:
    """Run one automation cycle for one user. Never raises for provider data."""
    outcome = CycleOutcome(user_id=user.id, ran=False, dry_run=True)
    state_row = state.get_state_row(session, user.id)

    if not user.is_active:
        outcome.skipped_reason = "account is disabled"
        return outcome
    if not user.automation_enabled or not state_row.running:
        outcome.skipped_reason = "automation switch is off"
        return outcome

    if price_provider is None or inverter_provider is None:
        try:
            price_provider, inverter_provider, values = build_providers(session, user)
        except ProviderError as exc:
            outcome.ran = True
            outcome.error = str(exc)
            _failures[user.id] = _failures.get(user.id, 0) + 1
            await _escalate_failure(session, user, state_row, outcome, str(exc), notifier)
            return outcome
    else:
        values = state.resolve_user_params(session, user.id)

    dry_run = bool(values.get("DRY_RUN", True))
    outcome.dry_run = dry_run
    outcome.ran = True
    lookahead = max(1, int(values.get("LOOKAHEAD_INTERVALS") or 6))
    poll_minutes = int(values.get("POLL_MINUTES") or 5)
    try:
        from app.config import get_settings
        poll_minutes = max(poll_minutes, int(get_settings().MIN_POLL_MINUTES or 1))
    except Exception:  # noqa: BLE001 - config problems must not stop the cycle
        pass

    now = dt.datetime.now(dt.timezone.utc)
    prices = None
    soc: float | None = None
    read_errors: list[str] = []

    try:
        prices = await price_provider.get_prices(lookahead)
    except ProviderError as exc:
        read_errors.append(f"prices: {exc}")
        _log(session, user.id, "error", f"Could not read prices this cycle: {exc}")
    except Exception as exc:  # noqa: BLE001
        read_errors.append(f"prices: {exc}")
        _log(session, user.id, "error", f"Unexpected error reading prices: {exc}")

    try:
        soc = await inverter_provider.get_soc()
    except ProviderError as exc:
        read_errors.append(f"SoC: {exc}")
        _log(session, user.id, "error", f"Could not read battery SoC this cycle: {exc}")
        if user.id not in _soc_unreadable_notified:
            _soc_unreadable_notified.add(user.id)
            await _notify(
                session, user, notify.EVENT_SOC_UNREADABLE,
                "Battery SoC unreadable",
                f"Battery state of charge could not be read: {exc} "
                "Automation decisions that need SoC are skipped until it works again.",
                notifier, outcome,
            )
    except Exception as exc:  # noqa: BLE001
        read_errors.append(f"SoC: {exc}")
        _log(session, user.id, "error", f"Unexpected error reading SoC: {exc}")

    if read_errors:
        _failures[user.id] = _failures.get(user.id, 0) + 1
        outcome.error = "; ".join(read_errors)

    poll_row: PollHistory | None = None
    if prices is not None or soc is not None:
        # One row per cycle while the switch is ON; feeds the 24 h replay.
        poll_row = PollHistory(
            user_id=user.id, ts=now,
            soc=soc,
            buy_c=prices.current_buy_c if prices else None,
            feed_in_c=prices.current_feed_in_c if prices else None,
            forecast=list(prices.forecast_buy_c) if prices else [],
            interval_minutes=prices.interval_minutes if prices else 30,
            raw_json=json.dumps({"read_errors": read_errors}) if read_errors else None,
        )
        session.add(poll_row)

    if prices is None and soc is None:
        await _escalate_failure(
            session, user, state_row, outcome, outcome.error or "no data could be read", notifier
        )
        session.commit()
        return outcome

    # -- shared rules (identical to the preview) ----------------------------
    inputs = RuleInputs(
        soc=soc,
        current_buy_c=prices.current_buy_c if prices else None,
        forecast_buy_c=list(prices.forecast_buy_c) if prices else [],
        feed_in_c=prices.current_feed_in_c if prices else None,
    )
    last = _last_state(state_row)
    caps = RuleCapabilities.from_provider(inverter_provider)
    decisions = decide(values, inputs, last, caps)
    outcome.decisions = decisions

    new_last = LastState(last.export_blocked, last.grid_charge_disabled)
    for d in decisions:
        if d.unsupported:
            _log(session, user.id, "skipped",
                 f"[{d.rule}] skipped: {d.reason}", rule=d.rule, dry_run=dry_run,
                 detail={"compared": d.compared, "parameter": d.parameter})
            continue
        if not d.apply:
            _log(session, user.id, "decision",
                 f"[{d.rule}] keep: {d.reason}", rule=d.rule, dry_run=dry_run,
                 detail={"compared": d.compared, "parameter": d.parameter})
            continue

        if dry_run:
            outcome.actions_taken.append(f"dry-run:{d.action}")
            _log(session, user.id, "write",
                 f"[{d.rule}] Dry run: would {d.action.replace('_', ' ')} - {d.reason}",
                 rule=d.rule, dry_run=True,
                 detail={"compared": d.compared, "parameter": d.parameter})
            new_last = _after_action(new_last, d.action)
            continue

        # d.action names the inverter method exactly.
        method = getattr(inverter_provider, d.action, None)
        if method is None:
            _log(session, user.id, "error",
                 f"[{d.rule}] provider has no '{d.action}' method; skipped.",
                 rule=d.rule, dry_run=False)
            continue
        try:
            await method()
            outcome.actions_taken.append(d.action)
            _log(session, user.id, "write",
                 f"[{d.rule}] Applied {d.action.replace('_', ' ')} - {d.reason}",
                 rule=d.rule, dry_run=False,
                 detail={"compared": d.compared, "parameter": d.parameter})
            new_last = _after_action(new_last, d.action)
        except ProviderError as exc:
            outcome.error = str(exc)
            _log(session, user.id, "error",
                 f"[{d.rule}] could not {d.action.replace('_', ' ')}: {exc}",
                 rule=d.rule, dry_run=False)
            # Keep the state of the writes that did land before this failure.
            _save_last_state(session, state_row, new_last)
            if poll_row is not None:
                poll_row.export_blocked = new_last.export_blocked
                poll_row.grid_charge_disabled = new_last.grid_charge_disabled
            _failures[user.id] = _failures.get(user.id, 0) + 1
            await _escalate_failure(session, user, state_row, outcome, str(exc), notifier)
            session.commit()
            return outcome

    # -- record the cycle -----------------------------------------------------
    _save_last_state(session, state_row, new_last)
    if poll_row is not None:
        poll_row.export_blocked = new_last.export_blocked
        poll_row.grid_charge_disabled = new_last.grid_charge_disabled
    state_row.dry_run_effective = dry_run
    state_row.last_poll_at = now
    state_row.next_poll_at = now + dt.timedelta(minutes=poll_minutes)
    if read_errors:
        state_row.provider_error = outcome.error
        await _escalate_failure(
            session, user, state_row, outcome, outcome.error or "cycle had problems", notifier
        )
    else:
        state_row.provider_error = None
        _failures.pop(user.id, None)
        _soc_unreadable_notified.discard(user.id)
    session.commit()
    return outcome


async def _escalate_failure(
    session: Session,
    user: User,
    state_row: AutomationState,
    outcome: CycleOutcome,
    message: str,
    notifier: Callable[..., Awaitable[dict[str, Any]]] | None,
) -> None:
    """Escalate a failing cycle: error bookkeeping, notifications, eventual stop."""
    state_row.provider_error = message[:2000]
    state_row.last_poll_at = dt.datetime.now(dt.timezone.utc)
    streak = _failures.get(user.id, 0)

    if streak == FAILURE_WARN_AT:
        await _notify(
            session, user, notify.EVENT_REPEATED_FAILURE,
            "Provider failing repeatedly",
            f"Automation has failed {streak} cycles in a row: {message} "
            "It will stop itself if this keeps happening.",
            notifier, outcome,
        )
    if streak >= FAILURE_STOP_AT:
        state_row.running = False
        await _notify(
            session, user, notify.EVENT_STOPPED,
            "Automation stopped unexpectedly",
            f"Automation stopped itself after {streak} failed cycles in a row: "
            f"{message} Fix the provider setup, then start automation again "
            "from the dashboard.",
            notifier, outcome,
        )
    session.commit()


# ---------------------------------------------------------------------------
# Passes and the loop (used by app.cli run)
# ---------------------------------------------------------------------------

def eligible_users(session: Session, user_email: str | None = None) -> list[User]:
    stmt = select(User).where(User.is_active.is_(True), User.automation_enabled.is_(True))
    if user_email:
        stmt = stmt.where(User.email == user_email.strip().lower())
    return list(session.scalars(stmt))


async def run_pass(
    session: Session,
    *,
    user_email: str | None = None,
    notifier: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> list[CycleOutcome]:
    """One cycle for every eligible user (per-user isolation: one at a time)."""
    outcomes: list[CycleOutcome] = []
    for user in eligible_users(session, user_email):
        try:
            outcomes.append(await run_cycle(session, user, notifier=notifier))
        except Exception:  # noqa: BLE001 - one bad user must not stop the rest
            log.exception("cycle crashed for user %s", user.id)
            outcomes.append(CycleOutcome(
                user_id=user.id, ran=False,
                skipped_reason="cycle crashed unexpectedly (see server log)",
            ))
    return outcomes


async def run_loop(
    session_factory: Callable[[], Session],
    *,
    once: bool = False,
    user_email: str | None = None,
    interval_seconds: float = 300.0,
    max_cycles: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[CycleOutcome]:
    """Run cycles forever (or once/max_cycles); safe to cancel at any point."""
    collected: list[CycleOutcome] = []
    cycles = 0
    while True:
        session = session_factory()
        try:
            collected.extend(await run_pass(session, user_email=user_email))
        finally:
            session.close()
        cycles += 1
        if once or (max_cycles is not None and cycles >= max_cycles):
            return collected
        await sleep(interval_seconds)


__all__ = [
    "CycleOutcome", "build_providers", "run_cycle", "run_pass", "run_loop",
    "eligible_users", "reset_failures", "FAILURE_WARN_AT", "FAILURE_STOP_AT",
]
