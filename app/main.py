"""app/main.py

FastAPI application entrypoint (run via ``uvicorn app.main:app``).

Implemented: signup/sign-in with server-side sessions, CSRF on every
state-changing request, login lockout, invite-based signups after the
single-owner bootstrap, admin controls (disable accounts / automation,
issue invites), schema-driven settings and setup UI, dashboard (mock
providers), what-if preview (right now / what-if / replay) and CLI helpers
via app.cli. The per-user worker loop and the real vendor adapters (Amber,
FoxESS) arrive in later phases.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import secrets
import sqlalchemy as sa
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import params, state
from app import backup
from app import mail
from app.auth import (
    SESSION_COOKIE,
    clear_failed_logins,
    consume_password_reset,
    count_reset_codes,
    create_invite,
    create_password_reset,
    create_session,
    destroy_all_sessions,
    destroy_session,
    get_session_user_id,
    hash_password,
    is_locked,
    record_failed_login,
    verify_password,
)
from app.config import get_settings
from app.db import Base, Session, get_session, init_engine
from app.models import (
    ActionLog,
    Invite,
    User,
)
from app.providers import registry
from app.rules import Capabilities, LastState
from app import preview as preview_module
from app.security import CSRFMiddleware, issue_csrf_token

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def render(request: Request, name: str, context: dict[str, Any], status_code: int = 200):
    """TemplateResponse + CSRF token injection for every rendered page."""
    context.setdefault("csrf_token", issue_csrf_token(request))
    return templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )


def invite_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _refuse_insecure_production(settings) -> None:
    if settings.DEBUG and os.environ.get("PRODUCTION"):
        raise RuntimeError(
            "Refusing to start: DEBUG is on in production. Set DEBUG=off for real deployments."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _refuse_insecure_production(settings)
    engine = init_engine(settings.DATABASE_URL)
    # Ephemeral-host deployments: pull the latest snapshot back into place
    # BEFORE anything opens the database (create_all first connects here).
    if backup.is_configured():
        restored = await asyncio.to_thread(backup.restore_if_empty, engine)
        if restored:
            # Rebind module-level engine pieces so the new file is used.
            from app.db import make_engine, make_session_factory

            engine = make_engine(settings.DATABASE_URL)
            SessionLocal_local = make_session_factory(engine)
            init_engine(settings.DATABASE_URL)
            import app.db as _db

            _db.SessionLocal = SessionLocal_local
    Base.metadata.create_all(bind=engine)
    app.state.engine = engine
    registry.import_all_adapters()
    params.register_provider_choices(
        _public_providers(registry.list_price_providers()),
        _public_providers(registry.list_inverter_providers()),
    )
    from app.db import SessionLocal

    with SessionLocal() as session:
        session.execute(sa.text("SELECT 1"))

    tasks: list[asyncio.Task] = []
    if backup.is_configured():
        tasks.append(asyncio.create_task(
            backup.backup_loop(
                engine,
                interval=max(60, int(settings.BACKUP_INTERVAL_SECONDS)),
                keep=max(1, int(settings.BACKUP_KEEP)),
            ),
            name="hf-backup",
        ))
    if (settings.WORKER_EMBED or "").strip().lower() in {"1", "true", "on", "yes"}:
        from app.worker import run_loop

        tasks.append(asyncio.create_task(
            run_loop(SessionLocal,
                     interval_seconds=max(30, int(settings.WORKER_INTERVAL_SECONDS))),
            name="embedded-worker",
        ))
    app.state.background_tasks = tasks
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def _public_providers(names: list[str]) -> list[str]:
    """Hide template adapters from the user-facing choice lists."""
    return [n for n in names if n != "template"]


class NotAuthenticated(Exception):
    """Raised by the current_user dependency to bounce to /login."""


async def current_user(
    request: Request, session: Session = Depends(get_session)
) -> User:
    """Resolve the signed-in user from the server-side session."""
    user_id = get_session_user_id(request.cookies.get(SESSION_COOKIE), session)
    if user_id is None:
        raise NotAuthenticated()
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise NotAuthenticated()
    return user


async def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user


app = FastAPI(title="Amber Home Automation", lifespan=lifespan)
app.add_middleware(CSRFMiddleware)

@app.exception_handler(NotAuthenticated)
async def _redirect_to_login_handler(request: Request, _exc: NotAuthenticated):
    next_path = request.url.path
    target = "/login" + (f"?next={next_path}" if next_path not in ("/", "") else "")
    return RedirectResponse(target, status_code=303)


# ---------------------------------------------------------------------------
# Sign up / sign in (single owner, then invite-only)
# ---------------------------------------------------------------------------


def _signup_open(session: Session) -> bool:
    if get_settings().SIGNUP_MODE == "open":
        return True
    return session.query(User).count() == 0


def _invite_valid(session: Session, token: str | None) -> bool:
    if not token:
        return False
    row = session.query(Invite).filter(Invite.token_hash == invite_hash(token)).first()
    if row is None or row.claimed_at is not None:
        return False
    if row.expires_at is not None and row.expires_at < dt.datetime.now(dt.timezone.utc):
        return False
    return True


def _sign_in(request: Request, session: Session, user_id: int, target: str = "/dashboard") -> RedirectResponse:
    token = create_session(user_id, session)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=86400 * int(get_settings().SESSION_LIFETIME_HOURS),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


def _can_accept_new_user(session: Session) -> bool:
    return session.query(User).count() < int(get_settings().MAX_USERS)


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, session: Session = Depends(get_session)):
    token = request.query_params.get("token")
    if _signup_open(session):
        return render(request, "auth.html", {"page": "auth", "mode": "signup", "invite_token": None})
    if _invite_valid(session, token):
        return render(request, "auth.html", {"page": "auth", "mode": "signup", "invite_token": token})
    return render(request, "auth.html", {"page": "auth", "mode": "closed", "invite_token": None})


@app.post("/signup", response_class=HTMLResponse)
async def signup_submit(request: Request, session: Session = Depends(get_session)):
    form = dict(await request.form())
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    invite_token = str(form.get("invite_token", "") or "")

    first_user = _signup_open(session)
    if not first_user and not _invite_valid(session, invite_token):
        return render(
            request, "auth.html",
            {"page": "auth", "mode": "closed", "invite_token": None,
             "error": "Sign-ups are closed for this instance."},
        )
    if not _can_accept_new_user(session):
        return render(
            request, "auth.html",
            {"page": "auth", "mode": "closed", "invite_token": None,
             "error": "This instance has reached its account limit."},
        )

    error = None
    if "@" not in email or len(email) < 5 or len(email) > 254:
        error = "Enter a valid email address."
    elif len(password) < 10:
        error = "Choose a password of at least 10 characters."
    elif session.query(User).filter(User.email == email).first() is not None:
        error = "An account with that email already exists."
    if error is not None:
        return render(
            request, "auth.html",
            {"page": "auth", "mode": "signup", "invite_token": invite_token or None,
             "error": error, "email": email},
        )

    user = User(email=email, password_hash=hash_password(password), is_admin=first_user)
    session.add(user)
    session.commit()
    state.save_user_settings(session, user.id, state.default_param_values())
    if invite_token:
        from app.auth import consume_invite

        consume_invite(invite_token, session)
    return _sign_in(request, session, user.id)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, session: Session = Depends(get_session)):
    return render(request, "auth.html", {"page": "auth", "mode": "login", "signup_open": _signup_open(session)})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, session: Session = Depends(get_session)):
    form = dict(await request.form())
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    next_path = str(form.get("next", "") or "")

    if is_locked(email, session):
        return render(
            request, "auth.html",
            {"page": "auth", "mode": "login", "signup_open": _signup_open(session),
             "error": "Too many failed attempts. Wait about 15 minutes and try again.",
             "email": email},
        )

    user = session.query(User).filter(User.email == email).first()
    if user is None or not verify_password(user.password_hash, password):
        record_failed_login(email, session)
        return render(
            request, "auth.html",
            {"page": "auth", "mode": "login", "signup_open": _signup_open(session),
             "error": "Email or password is incorrect.", "email": email},
        )
    if not user.is_active:
        return render(
            request, "auth.html",
            {"page": "auth", "mode": "login", "signup_open": _signup_open(session),
             "error": "This account has been disabled by the administrator.", "email": email},
        )
    clear_failed_logins(email, session)
    target = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/dashboard"
    return _sign_in(request, session, user.id, target)


# ---------------------------------------------------------------------------
# Password recovery (emailed passcode) and rescue unlock
# ---------------------------------------------------------------------------


def _reset_email_text(code: str, ttl: int) -> str:
    return (
        "Your Amber Home Automation reset code is:\n\n"
        f"    {code}\n\n"
        f"Enter it in the app within {ttl} minutes to choose a new password. "
        "If you did not request this, ignore this email — no one has changed anything."
    )


@app.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request, session: Session = Depends(get_session)):
    return render(request, "reset_email.html", {"page": "auth", "error": None, "email": ""})


@app.post("/reset", response_class=HTMLResponse)
async def reset_submit(request: Request, session: Session = Depends(get_session)):
    form = dict(await request.form())
    email = str(form.get("email", "")).strip().lower()
    user = session.query(User).filter(User.email == email).first()
    if user is not None and user.is_active:
        ttl = max(5, int(get_settings().RESET_CODE_TTL_MINUTES))
        window = ttl * 3
        if count_reset_codes(session, user.id, window) >= max(1, int(get_settings().RESET_MAX_CODES_PER_WINDOW)):
            return render(request, "reset_email.html",
                          {"page": "auth", "error": "Too many codes requested. Try again shortly.",
                           "email": email})
        raw_code = create_password_reset(session, user.id, ttl)
        try:
            mail.send_email(user.email, "Your Amber Home Automation reset code",
                            _reset_email_text(raw_code, ttl))
            sent = True
        except mail.MailNotConfigured:
            return render(request, "reset_email.html",
                          {"page": "auth", "error": (
                              "Email delivery is not configured on this instance yet. "
                              "The owner needs to set RESEND_API_KEY (or SMTP credentials) "
                              "in the service environment. Use the rescue link meanwhile."), })
        except mail.MailSendError:
            return render(request, "reset_email.html",
                          {"page": "auth", "error": "The email service rejected the message. Try again in a minute.",
                           "email": email})
        if sent:
            return render(request, "reset_code.html",
                          {"page": "auth", "email": email, "error": None,
                           "notice": "Check your inbox — we sent a 6-digit code. It expires in "
                                     f"{ttl} minutes."})
    # Unknown or disabled email: identical response so accounts can't be probed.
    return render(request, "reset_email.html",
                  {"page": "auth", "error": "If that email has an account, a code is on its way.",
                   "email": email})


@app.post("/reset/verify", response_class=HTMLResponse)
async def reset_verify(request: Request, session: Session = Depends(get_session)):
    form = dict(await request.form())
    email = str(form.get("email", "")).strip().lower()
    code = str(form.get("code", "")).strip()
    new_password = str(form.get("password", ""))

    def _fail(message: str) -> HTMLResponse:
        return render(request, "reset_code.html",
                      {"page": "auth", "email": email, "error": message, "notice": None})

    user = session.query(User).filter(User.email == email).first()
    if user is None or not user.is_active:
        return _fail("Start again from the reset form.")
    if len(code) != 6 or not code.isdigit():
        return _fail("The code is six digits — check the email and try again.")
    if len(new_password) < 10:
        return _fail("Choose a new password of at least 10 characters.")
    if not consume_password_reset(session, user.id, code):
        return _fail("That code is wrong or has expired. Request a new one from /reset.")

    user.password_hash = hash_password(new_password)
    session.commit()
    destroy_all_sessions(user.id, session)
    clear_failed_logins(user.email, session)
    return _sign_in(request, session, user.id)


@app.get("/rescue", response_class=HTMLResponse)
async def rescue_page(request: Request, session: Session = Depends(get_session)):
    token = str(request.query_params.get("token", "") or "")
    expected = get_settings().RESCUE_TOKEN
    if not expected or not token or not secrets.compare_digest(token, expected):
        return render(request, "rescue.html",
                      {"page": "auth", "error": "Rescue is disabled or the link is wrong.",
                       "armed": False, "email": ""})
    return render(request, "rescue.html",
                  {"page": "auth", "error": None, "armed": True, "email": "",
                   "rescue_token": token})


@app.post("/rescue", response_class=HTMLResponse)
async def rescue_submit(request: Request, session: Session = Depends(get_session)):
    form = dict(await request.form())
    token = str(form.get("rescue_token", "") or "")
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    expected = get_settings().RESCUE_TOKEN
    if not expected or not token or not secrets.compare_digest(token, expected):
        return render(request, "rescue.html",
                      {"page": "auth", "error": "Rescue is disabled or the link is wrong.",
                       "armed": False, "email": ""})
    if "@" not in email or len(email) < 5 or len(email) > 254:
        return render(request, "rescue.html",
                      {"page": "auth", "error": "Enter a valid email address.",
                       "armed": True, "email": email, "rescue_token": token})
    if len(password) < 10:
        return render(request, "rescue.html",
                      {"page": "auth", "error": "Choose a password of at least 10 characters.",
                       "armed": True, "email": email, "rescue_token": token})

    target = session.query(User).order_by(User.id.asc()).first()
    if target is None:
        target = User(email=email, password_hash=hash_password(password), is_admin=True)
        session.add(target)
    else:
        target.email = email
        target.password_hash = hash_password(password)
        target.is_active = True
        target.is_admin = True
    session.commit()
    destroy_all_sessions(target.id, session)
    clear_failed_logins(target.email, session)

    # Rescue link is strictly one-shot: burn the token from the environment is
    # not possible from inside the process, but the DB row below prevents reuse
    # after the operator clears the env var; the form check above fails closed.
    return _sign_in(request, session, target.id)


@app.post("/logout")
async def logout(request: Request, session: Session = Depends(get_session)):
    destroy_session(request.cookies.get(SESSION_COOKIE), session)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return RedirectResponse("/dashboard", status_code=302)


def _build_live_providers(session: Session, user: User) -> tuple[Any, Any]:
    """Instantiate the user's chosen providers (any registered adapter).

    Shared with the worker loop via app.worker.build_providers, so the UI and
    the automation see exactly the same provider configuration.
    """
    from app.providers.base import ProviderError
    from app.worker import build_providers

    try:
        price_provider, inverter_provider, _values = build_providers(session, user)
    except (registry.UnknownProviderError, ProviderError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return price_provider, inverter_provider


def _active_mode(soc: float | None, values: dict[str, Any], prices: Any) -> str:
    """Human-readable summary of what the rules would do right now."""
    if soc is None or prices is None:
        return "Unknown"
    parts = []
    if values.get("ENABLE_EXPORT_CONTROL") and soc <= values["TARGET_MRL"]:
        parts.append("Exports blocked")
    window_max = prices.current_buy_c
    forecast = list(prices.forecast_buy_c or [])
    if forecast:
        window_max = max([p for p in [prices.current_buy_c] + forecast if p is not None])
    if (
        values.get("ENABLE_GRID_CHARGE_CONTROL")
        and window_max is not None
        and window_max > values["PRICE_THRESHOLD_CENTS"]
    ):
        parts.append("Grid charging blocked")
    return " + ".join(parts) if parts else "Normal"


async def _dashboard_context(session: Session, user: User) -> dict[str, Any]:
    values = state.resolve_user_params(session, user.id)
    state_row = state.get_state_row(session, user.id)
    try:
        price_provider, inverter_provider = _build_live_providers(session, user)
        soc: float | None = await inverter_provider.get_soc()
        prices = await price_provider.get_prices(values["LOOKAHEAD_INTERVALS"])
        error: str | None = None
    except Exception as exc:
        soc, prices, error = None, None, str(exc)

    highest_forecast = max(prices.forecast_buy_c) if prices and prices.forecast_buy_c else None
    recent_actions = (
        session.query(ActionLog)
        .filter(ActionLog.user_id == user.id)
        .order_by(ActionLog.ts.desc())
        .limit(50)
        .all()
    )
    return {
        "user": user,
        "settings": values,
        "running": state_row.running,
        "dry_run": state_row.dry_run_effective,
        "soc": soc,
        "prices": prices,
        "highest_forecast": highest_forecast,
        "active_mode": _active_mode(soc, values, prices),
        "last_poll_at": state_row.last_poll_at,
        "next_poll_at": state_row.next_poll_at,
        "provider_error": error,
        "recent_actions": recent_actions,
        "price_provider_name": values.get("PRICE_PROVIDER") or "mock",
        "inverter_provider_name": values.get("INVERTER_PROVIDER") or "mock",
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    context = await _dashboard_context(session, user)
    return render(request, "dashboard.html", context | {"page": "dashboard"})


@app.get("/partials/status", response_class=HTMLResponse)
async def partial_status(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    context = await _dashboard_context(session, user)
    return render(request, "partials/status.html", context | {"page": "dashboard"})


@app.post("/automation/toggle")
async def automation_toggle(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    form = dict(await request.form())
    action = str(form.get("action", ""))
    if action not in ("start", "stop"):
        raise HTTPException(status_code=400, detail="Unknown action.")
    row = state.get_state_row(session, user.id)
    row.running = action == "start"
    if not row.running:
        row.next_poll_at = None
    session.commit()
    return RedirectResponse("/dashboard", status_code=303)


# ---------------------------------------------------------------------------
# Settings (fields rendered entirely from app/params.py)
# ---------------------------------------------------------------------------


def _param_form_context(values: dict[str, Any], errors: list[str] | None = None) -> dict[str, Any]:
    """Group user params into cards for the settings page."""
    groups: dict[str, list[dict[str, Any]]] = {
        "Providers": [],
        "Battery rules": [],
        "Loop": [],
        "Schedule": [],
        "Notifications": [],
        "Inverter command mapping": [],
    }
    assignment = {
        "PRICE_PROVIDER": "Providers",
        "INVERTER_PROVIDER": "Providers",
        **{k: "Inverter command mapping" for k in params.COMMAND_MAP_PARAMS},
        "TARGET_MRL": "Battery rules",
        "HARDWARE_MIN_SOC": "Battery rules",
        "PRICE_THRESHOLD_CENTS": "Battery rules",
        "LOOKAHEAD_INTERVALS": "Battery rules",
        "SOC_HYSTERESIS": "Battery rules",
        "ENABLE_EXPORT_CONTROL": "Battery rules",
        "ENABLE_GRID_CHARGE_CONTROL": "Battery rules",
        "PRICE_UNIT": "Battery rules",
        "POLL_MINUTES": "Loop",
        "RESUME_AFTER_RESTART": "Loop",
        "DRY_RUN": "Loop",
        "ACTIVE_DAYS": "Schedule",
        "ACTIVE_FROM": "Schedule",
        "ACTIVE_TO": "Schedule",
        "TIMEZONE": "Schedule",
        "NOTIFY_EMAIL": "Notifications",
        "NOTIFY_WEBHOOK_URL": "Notifications",
        "NOTIFY_EVENTS": "Notifications",
    }
    for key in params.user_param_keys():
        defn = params.get(key)
        raw = values.get(defn.key, "")
        display_value = "" if raw in (None, "") else str(raw)
        groups[assignment.get(key, "Loop")].append({
            "key": defn.key,
            "label": defn.label,
            "value": raw,
            "display_value": display_value,
            "help_text": defn.help_text,
            "type": defn.type,
            "choices": list(defn.choices or []),
            "attrs": defn.input_attrs(),
            "placeholder": defn.placeholder,
        })
    return {"param_groups": groups, "errors": errors or []}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    values = state.get_user_settings(session, user.id)
    return render(
        request, "settings.html",
        _param_form_context(values)
        | {
            "page": "settings",
            "user": user,
            "saved": request.query_params.get("saved") == "1",
        },
    )


@app.post("/settings")
async def settings_save(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    form = dict(await request.form())
    # Booleans are checkboxes: an absent key means unticked.
    for key, defn in params.USER_PARAMS.items():
        if defn.type == "bool":
            form.setdefault(key, "false")
    try:
        values = params.validate_section("user", form)
    except params.ParamValidationError as exc:
        values = state.get_user_settings(session, user.id)
        return render(
            request, "settings.html",
            _param_form_context(values, errors=[str(exc)])
            | {"page": "settings", "user": user, "saved": False},
        )
    # Instance-min clamp: POLL_MINUTES cannot be faster than the instance minimum.
    minimum = get_settings().MIN_POLL_MINUTES
    if values.get("POLL_MINUTES") is not None and minimum and values["POLL_MINUTES"] < minimum:
        raise HTTPException(
            status_code=422,
            detail=f"Poll interval must be at least {minimum} minutes (instance minimum).",
        )
    state.save_user_settings(session, user.id, values)
    return RedirectResponse("/settings?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# What-if preview (read-only; never sends anything)
# ---------------------------------------------------------------------------


def _last_state_for(session: Session, user_id: int) -> LastState:
    row = state.get_state_row(session, user_id)
    try:
        applied = json.loads(row.last_applied_json or "{}")
    except (TypeError, ValueError):
        applied = {}
    return LastState(
        export_blocked=applied.get("export_blocked"),
        grid_charge_disabled=applied.get("grid_charge_disabled"),
    )


@app.get("/preview", response_class=HTMLResponse)
async def preview_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    values = state.resolve_user_params(session, user.id)
    initial_soc = initial_buy = initial_feed = None
    initial_forecast = ""
    try:
        price_provider, inverter_provider = _build_live_providers(session, user)
        initial_soc = await inverter_provider.get_soc()
        snapshot = await price_provider.get_prices(int(values.get("LOOKAHEAD_INTERVALS") or 6))
        initial_buy = snapshot.current_buy_c
        initial_feed = snapshot.current_feed_in_c
        initial_forecast = ",".join(f"{x:g}" for x in snapshot.forecast_buy_c)
    except Exception:
        pass
    return render(
        request, "preview.html",
        {
            "page": "preview",
            "user": user,
            "settings": values,
            "initial_soc": initial_soc,
            "initial_buy": initial_buy,
            "initial_feed": initial_feed,
            "initial_forecast": initial_forecast,
        },
    )


@app.post("/preview/right-now", response_class=HTMLResponse)
async def preview_right_now(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    values = state.resolve_user_params(session, user.id)
    result, error = None, None
    try:
        price_provider, inverter_provider = _build_live_providers(session, user)
        result = await preview_module.right_now(
            values, price_provider, inverter_provider, _last_state_for(session, user.id)
        )
    except Exception as exc:
        error = str(exc)
    return templates.TemplateResponse(
        request=request,
        name="partials/preview_result.html",
        context={"result": result, "error": error, "title": "Right now"},
    )


@app.post("/preview/what-if", response_class=HTMLResponse)
async def preview_what_if(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    form = dict(await request.form())
    values = state.resolve_user_params(session, user.id)

    def _float(raw: Any) -> float | None:
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return None

    forecast: list[float] = []
    bad_forecast = False
    for chunk in str(form.get("forecast", "") or "").split(","):
        value = _float(chunk)
        if chunk.strip() and value is None:
            bad_forecast = True
        elif value is not None:
            forecast.append(value)
    error = None
    if bad_forecast:
        error = "The forecast list must be comma-separated numbers, e.g. 12,18,40."
    elif _float(form.get("soc")) is None:
        error = "Enter the battery SoC as a number (%)."
    elif _float(form.get("buy")) is None:
        error = "Enter the current buy price as a number."
    result = None
    if error is None:
        result = preview_module.what_if(
            values,
            soc=_float(form.get("soc")),
            current_buy_c=_float(form.get("buy")),
            forecast_buy_c=forecast,
            feed_in_c=_float(form.get("feed_in")),
            last_state=LastState(),
            caps=Capabilities.fully_supported(),
        )
    return templates.TemplateResponse(
        request=request,
        name="partials/preview_result.html",
        context={"result": result, "error": error, "title": "What if"},
    )


@app.post("/preview/replay", response_class=HTMLResponse)
async def preview_replay(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    from app.models import PollHistory

    form = dict(await request.form())
    values = state.resolve_user_params(session, user.id)
    overrides: dict[str, float] = {}
    for key in ("TARGET_MRL", "PRICE_THRESHOLD_CENTS"):
        raw = str(form.get(key) or "").strip()
        if raw != "":
            try:
                overrides[key] = float(raw)
            except ValueError:
                return templates.TemplateResponse(
                    request=request,
                    name="partials/preview_replay.html",
                    context={"summary": None, "error": f"The {key} override must be a number."},
                )
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    history = (
        session.query(PollHistory)
        .filter(PollHistory.user_id == user.id, PollHistory.ts >= since)
        .order_by(PollHistory.ts.asc())
        .all()
    )
    summary = preview_module.replay(values, history, overrides) if history else None
    return templates.TemplateResponse(
        request=request,
        name="partials/preview_replay.html",
        context={
            "summary": summary,
            "error": None if history else "No stored polls in the last 24 hours yet. "
            "Run the automation loop for a while, then replay.",
        },
    )


# ---------------------------------------------------------------------------
# Setup wizard (schema-driven, zero per-adapter UI code)
# ---------------------------------------------------------------------------


def _adapter_for(provider_name: str, kind: str | None = None) -> Any:
    if kind == "price":
        return registry.get_price_adapter(provider_name)
    return registry.get_inverter_adapter(provider_name)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    values = state.get_user_settings(session, user.id)
    return render(
        request, "setup.html",
        {
            "page": "setup",
            "user": user,
            "adapters": [a for a in registry.all_adapters() if a["provider_name"] != "template"],
            "saved_values": values,
        },
    )


@app.post("/setup/creds/{provider_name}/{kind}", response_class=HTMLResponse)
async def setup_creds(
    request: Request,
    provider_name: str,
    kind: str,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    """Save provider credentials (encrypted at rest) and set the choice param."""
    form = dict(await request.form())
    try:
        adapter = _adapter_for(provider_name, kind)
        cleaned = adapter.validate_config(form)
    except (ValueError, registry.UnknownProviderError) as exc:
        return templates.TemplateResponse(
            request=request,
            name="partials/setup_error.html",
            context={"message": str(exc)},
        )
    state.save_credentials(session, user.id, provider_name, cleaned)
    key = "PRICE_PROVIDER" if kind == "price" else "INVERTER_PROVIDER"
    values = state.get_user_settings(session, user.id)
    values[key] = provider_name
    state.save_user_settings(session, user.id, values)
    return templates.TemplateResponse(
        request=request,
        name="partials/setup_saved.html",
        context={"provider": provider_name, "kind": kind},
    )


@app.post("/setup/test/{provider_name}/{kind}", response_class=HTMLResponse)
async def setup_test(
    request: Request,
    provider_name: str,
    kind: str,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    """'Test connection': read-only calls with plain-language outcomes."""
    try:
        adapter = _adapter_for(provider_name, kind)
        values = state.get_credentials(session, user.id, provider_name)
        instance = adapter(values or {})
        message = await instance.test_connection()
        ok = True
    except Exception as exc:
        message, ok = str(exc), False
    hint = _hint_for(message) if not ok else ""
    return templates.TemplateResponse(
        request=request,
        name="partials/test_result.html",
        context={"ok": ok, "message": message, "hint": hint, "provider": provider_name},
    )


def _hint_for(message: str) -> str:
    text = message.lower()
    if "401" in message or "unauthorized" in text:
        return "Check that the key/token is current and pasted without extra spaces."
    if "not found" in text or "404" in message:
        return "Check the site/device identifier - it must match the exact value in your provider app."
    return "Check your inputs and network, then try again."


# ---------------------------------------------------------------------------
# Admin (invite management, account controls)
# ---------------------------------------------------------------------------


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    users = session.query(User).order_by(User.id.asc()).all()
    invites = session.query(Invite).order_by(Invite.id.desc()).limit(20).all()
    raw_invite = request.query_params.get("invite")
    settings = get_settings()
    invite_link = (
        f"{(settings.BASE_URL or '').rstrip('/')}/signup?token={raw_invite}" if raw_invite else None
    )
    return render(
        request, "admin.html",
        {
            "page": "admin",
            "user": user,
            "users": users,
            "invites": invites,
            "invite_link": invite_link,
            "invite_mode": settings.SIGNUP_MODE or "invite_only",
            "invite_mode_url": f"{(settings.BASE_URL or '').rstrip('/')}/signup",
        },
    )


@app.post("/admin/invites")
async def admin_create_invite(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    raw = create_invite(session, user.id)
    return RedirectResponse(f"/admin?invite={raw}", status_code=303)


@app.post("/admin/users/{user_id}/disable")
async def admin_disable_user(
    request: Request,
    user_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such account.")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot disable your own account.")
    target.is_active = False
    target.automation_enabled = False
    destroy_all_sessions(target.id, session)  # instant sign-out + automation off
    from app.models import AutomationState

    state_row = (
        session.query(AutomationState).filter(AutomationState.user_id == user_id).first()
    )
    if state_row is None:
        state_row = AutomationState(user_id=user_id)
        session.add(state_row)
    state_row.running = False
    session.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/enable")
async def admin_enable_user(
    request: Request,
    user_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such account.")
    target.is_active = True
    session.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/stop-automation")
async def admin_stop_automation(
    request: Request,
    user_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    from app.models import AutomationState

    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such account.")
    state_row = (
        session.query(AutomationState).filter(AutomationState.user_id == user_id).first()
    )
    if state_row is None:
        state_row = AutomationState(user_id=user_id)
        session.add(state_row)
    state_row.running = False
    target.automation_enabled = False
    session.commit()
    return RedirectResponse("/admin", status_code=303)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; connect-src 'self'",
    )
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


__all__ = ["app", "templates", "current_user", "require_admin"]
