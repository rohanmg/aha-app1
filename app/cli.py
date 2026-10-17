"""app/cli.py

Command line entrypoint: ``python -m app.cli <command>``.

Commands:
* simulate  - run the rules on hypothetical values with mock providers
              (sends nothing to any device)
* params    - print every parameter (default, type, bounds, description);
              ``--generate-docs`` writes docs/PARAMETERS.md
* providers - list registered providers and their declared config fields
* create-admin - create the first admin account
* run       - run the per-user automation worker loop (``--once`` for one pass)
* inspect   - read-only dump of one user's inverter state via their adapter
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from typing import Any

from app.params import (
    COMMAND_MAP_PARAMS,
    INSTANCE_PARAMS,
    USER_PARAMS,
    ParamDef,
    to_docs_markdown,
)
from app.db import init_engine
from app.rules import Capabilities, LastState, RuleInputs, decide
from app.providers.registry import all_adapters


def _fmt_def(defn: ParamDef) -> str:
    bounds = f" [{defn.min}..{defn.max}]" if defn.min is not None or defn.max is not None else ""
    secret = " (secret)" if defn.secret else ""
    required = " (required)" if defn.required else ""
    return (
        f"{defn.key}"
        f"  type={defn.type}  default={defn.default}{bounds}{secret}{required}\n"
        f"      {defn.help_text}"
    )


def cmd_params(args: argparse.Namespace) -> int:
    sections = [("User parameters", USER_PARAMS), ("Instance parameters", INSTANCE_PARAMS)]
    print(f"Command-mapping overrides: {len(COMMAND_MAP_PARAMS)} (VERIFY AGAINST YOUR INVERTER)")
    for name, section in sections:
        print(f"\n== {name} ==")
        for defn in section.values():
            print(_fmt_def(defn))
    if args.generate_docs:
        docs_dir = "docs"
        import os

        os.makedirs(docs_dir, exist_ok=True)
        with open(os.path.join(docs_dir, "PARAMETERS.md"), "w") as fh:
            fh.write(to_docs_markdown())
        print(f"\nWrote docs/PARAMETERS.md")
    return 0


def cmd_providers(_args: argparse.Namespace) -> int:
    from app.providers import registry

    registry.import_all_adapters()
    if not all_adapters():
        print("No providers registered (has the registry been populated?).")
        return 0
    for adapter in all_adapters():
        print(f"\n== {adapter['label']} ({adapter['kind']}, name='{adapter['provider_name']}') ==")
        schema = adapter.get("config_schema") or []
        for f in schema:
            flags = ("secret" if f["secret"] else "") + (" required" if f["required"] else "")
            print(f"  {f['key']}  {f['type']}{flags}\n    {f['help_text']}"
                  + (f"\n    Where to find it: {f['hint']}" if f.get("hint") else ""))
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from app.providers.mock import MockPriceProvider, MockInverterProvider

    soc = args.soc
    if soc is None:
        print("--soc is required (percent, e.g. --soc 28)")
        return 2
    forecast = [float(x) for x in args.forecast.split(",") if x.strip()] if args.forecast else []
    if args.price is None:
        print("--price is required (current buy price in c/kWh)")
        return 2

    params: dict[str, Any] = {
        "ENABLE_EXPORT_CONTROL": args.no_export_rule is False,
        "ENABLE_GRID_CHARGE_CONTROL": args.no_charge_rule is False,
        "TARGET_MRL": args.mrl,
        "SOC_HYSTERESIS": args.hysteresis,
        "PRICE_THRESHOLD_CENTS": args.threshold,
        "HARDWARE_MIN_SOC": 10,
    }
    inputs = RuleInputs(soc=float(soc), current_buy_c=float(args.price), forecast_buy_c=forecast)
    last = LastState()
    # The mock inverter supports every write, so simulation exercises the real paths
    # without touching anything: it only records what *would* be sent.
    decisions = decide(
        params, inputs, last,
        Capabilities(
            block_export=True, restore_export=True,
            disable_grid_charge=True, restore_grid_charge=True,
        ),
    )
    print("Simulated cycle - nothing is sent anywhere (mock providers only):")
    print(f"  SoC: {soc:g}%   buy: {args.price:g}c   forecast: {forecast or '(-)'}")
    for decision in decisions:
        tag = decision.action if decision.action != "none" else "keep"
        print(f"  [{decision.rule}] -> {tag}: {decision.reason}")
        print(f"      parameter={decision.parameter} compared={json.dumps(decision.compared)}")

    # Also exercise the mock providers end-to-end for a realistic read pipeline.
    price_provider = MockPriceProvider({
        "current_buy_c": args.price,
        "current_feed_in_c": args.feed_in if args.feed_in is not None else 5,
        "forecast_buy_c": args.forecast or "",
        "interval_minutes": args.interval,
    })
    inverter_provider = MockInverterProvider({"initial_soc": soc})

    async def _pipeline() -> None:
        snap = await price_provider.get_prices(lookahead=len(forecast) or 1)
        live_soc = await inverter_provider.get_soc()
        live_inputs = RuleInputs(soc=live_soc, current_buy_c=snap.current_buy_c,
                                 forecast_buy_c=snap.forecast_buy_c, feed_in_c=snap.current_feed_in_c)
        live_decisions = decide(
            params, live_inputs, last,
            Capabilities(block_export=True, restore_export=True,
                         disable_grid_charge=True, restore_grid_charge=True),
        )
        print("  -- via mock providers (get_prices -> get_soc -> decide):")
        for decision in live_decisions:
            print(f"     [{decision.rule}] -> {decision.action}: {decision.reason}")

    asyncio.run(_pipeline())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run the per-user automation loop (one pass with --once)."""
    from app.db import make_session_factory
    from app.worker import run_loop

    engine = init_engine()
    from app.db import Base

    Base.metadata.create_all(bind=engine)
    factory = make_session_factory(engine)
    outcomes = asyncio.run(
        run_loop(
            factory,
            once=args.once,
            user_email=args.user,
            interval_seconds=args.interval,
        )
    )
    if not outcomes:
        print("No users with automation switched on; nothing to do.")
        return 0
    for outcome in outcomes:
        if not outcome.ran:
            print(f"user {outcome.user_id}: skipped ({outcome.skipped_reason})")
            continue
        mode = "dry run" if outcome.dry_run else "LIVE"
        actions = ", ".join(outcome.actions_taken) or "no changes"
        line = f"user {outcome.user_id}: {mode} cycle ok - {actions}"
        if outcome.error:
            line += f" - problem: {outcome.error}"
        print(line)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Read-only dump of one user's inverter state (never writes)."""
    from app.db import make_session_factory
    from app.models import User
    from app.providers.base import ProviderError
    from app.worker import build_providers

    engine = init_engine()
    factory = make_session_factory(engine)
    with factory() as session:
        stmt = session.query(User)
        if args.user:
            stmt = stmt.filter(User.email == args.user.strip().lower())
        user = stmt.first()
        if user is None:
            print("No such user. Omit --user for the only account on a single-owner instance.")
            return 2
        try:
            _price, inverter, values = build_providers(session, user)
        except ProviderError as exc:
            print(f"Cannot build providers: {exc}")
            return 1
        print(f"User: {user.email}   inverter provider: {values.get('INVERTER_PROVIDER') or 'mock'}")
        print("Mode: " + ("DRY RUN" if values.get("DRY_RUN", True) else "LIVE"))

        async def _inspect() -> dict:
            inspect_fn = getattr(inverter, "inspect", None)
            if inspect_fn is None:
                return {"error": "this provider does not support inspect"}
            return await inspect_fn()

        try:
            result = asyncio.run(_inspect())
        except ProviderError as exc:
            print(f"Inspect failed: {exc}")
            return 1
        print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    from app.auth import hash_password
    from app.models import User

    email = (args.email or input("Admin email: ")).strip().lower()
    password = args.password or getpass.getpass("Admin password (min 10 chars): ")
    if "@" not in email or len(email) < 5:
        print("Enter a valid email address.")
        return 2
    if len(password) < 10:
        print("Choose a password of at least 10 characters.")
        return 2
    engine = init_engine()
    from app.db import Base, make_session_factory

    Base.metadata.create_all(bind=engine)
    with make_session_factory(engine)() as session:
        if session.query(User).count() > 0:
            print("This instance already has accounts. Aborting to protect the owner signup policy.")
            return 1
        user = User(email=email, password_hash=hash_password(password), is_admin=True)
        session.add(user)
        session.commit()
        print(f"Admin created: {email}")
    print("Sign up is now closed for this instance (single-owner policy).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Utility commands for the solar automation app.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_params = sub.add_parser("params", help="print every parameter, default and description")
    p_params.add_argument("--generate-docs", action="store_true", help="also write docs/PARAMETERS.md")
    p_params.set_defaults(func=cmd_params)

    p_providers = sub.add_parser("providers", help="list registered providers and their config fields")
    p_providers.set_defaults(func=cmd_providers)

    p_sim = sub.add_parser("simulate", help="dry-run the rules on hypothetical inputs (sends nothing)")
    p_sim.add_argument("--soc", type=float, help="battery SoC to simulate, percent")
    p_sim.add_argument("--price", type=float, help="current buy price to simulate, c/kWh")
    p_sim.add_argument("--forecast", help="comma-separated forecast buy prices, c/kWh (e.g. 12,18,40)")
    p_sim.add_argument("--mrl", type=float, default=30, help="TARGET_MRL override for this run (default 30)")
    p_sim.add_argument("--threshold", type=float, default=30.0,
                       help="PRICE_THRESHOLD_CENTS override for this run (default 30)")
    p_sim.add_argument("--hysteresis", type=float, default=1, help="SOC_HYSTERESIS for this run (default 1)")
    p_sim.add_argument("--feed-in", type=float, default=None, help="feed-in price for realism (c/kWh)")
    p_sim.add_argument("--interval", type=int, default=30, help="price interval minutes for mock (default 30)")
    p_sim.add_argument("--no-export-rule", action="store_true", help="disable Rule 1 for this run")
    p_sim.add_argument("--no-charge-rule", action="store_true", help="disable Rule 2 for this run")
    p_sim.set_defaults(func=cmd_simulate)

    p_run = sub.add_parser("run", help="run the per-user automation worker loop")
    p_run.add_argument("--once", action="store_true", help="run one pass over all users, then exit")
    p_run.add_argument("--user", help="only this account's email")
    p_run.add_argument("--interval", type=float, default=300.0,
                       help="seconds between passes in loop mode (default 300)")
    p_run.set_defaults(func=cmd_run)

    p_inspect = sub.add_parser("inspect", help="read-only inverter state dump for one user")
    p_inspect.add_argument("--user", help="account email (omit on a single-owner instance)")
    p_inspect.set_defaults(func=cmd_inspect)

    p_admin = sub.add_parser("create-admin", help="create the first admin account for this instance")
    p_admin.add_argument("--email", help="admin email address")
    p_admin.add_argument("--password", help="admin password (omit to prompt securely)")
    p_admin.set_defaults(func=cmd_create_admin)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
