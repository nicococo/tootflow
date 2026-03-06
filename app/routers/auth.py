import ipaddress
import logging
import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import create_access_token, get_token_payload, optional_current_user, auth_cookie_params
from app.config import get_settings
from app.crypto import encrypt_token
from app.csrf import verify_csrf
from app.database import get_session
from app.limiter import limiter
from app.models import MastodonAccount, User
from app.services import mastodon_service

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()
templates.env.globals["settings"] = settings

# OAuth state store: state_token → {instance_url, client_id, client_secret}
_oauth_apps: dict[str, dict] = {}


def _validate_instance_url(url: str) -> str:
    """Reject private/loopback URLs to prevent SSRF."""
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid instance URL")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Instance URL must use https")
    host = parsed.hostname or ""
    if not host:
        raise HTTPException(status_code=400, detail="Invalid instance URL")
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local"):
        raise HTTPException(status_code=400, detail="Invalid instance URL")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise HTTPException(status_code=400, detail="Invalid instance URL")
    except ValueError:
        pass  # hostname, not an IP — fine
    return url.rstrip("/")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: Session = Depends(get_session)):
    user = optional_current_user(request, session)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
@limiter.limit("10/minute")
async def login(
    request: Request,
    instance_url: str = Form(...),
    _csrf: None = Depends(verify_csrf),
):
    """Initiate Mastodon OAuth flow."""
    instance_url = _validate_instance_url(instance_url)
    redirect_uri = f"{settings.app_base_url}/mastodon/callback"
    try:
        app_data = await mastodon_service.register_app(instance_url, redirect_uri)
    except Exception as e:
        logger.error(f"Could not register OAuth app with {instance_url}: {e}")
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Could not connect to that Mastodon instance"},
            status_code=400,
        )

    state = secrets.token_urlsafe(16)
    _oauth_apps[state] = {
        "instance_url": instance_url,
        "client_id": app_data["client_id"],
        "client_secret": app_data["client_secret"],
    }

    auth_url = mastodon_service.get_oauth_url(instance_url, app_data["client_id"], redirect_uri, state)
    return RedirectResponse(auth_url, status_code=302)


@router.get("/mastodon/callback")
async def mastodon_callback(
    request: Request,
    code: str,
    state: str,
    session: Session = Depends(get_session),
):
    """OAuth callback: creates or retrieves the user, sets session cookie."""
    if state not in _oauth_apps:
        raise HTTPException(status_code=400, detail="OAuth session expired or invalid state")

    app = _oauth_apps.pop(state)
    instance_url = app["instance_url"]
    redirect_uri = f"{settings.app_base_url}/mastodon/callback"

    try:
        token = await mastodon_service.exchange_code_for_token(
            instance_url, app["client_id"], app["client_secret"], code, redirect_uri
        )
        info = await mastodon_service.get_account_info(instance_url, token)
    except Exception as e:
        logger.error(f"OAuth token exchange failed for {instance_url}: {e}")
        raise HTTPException(status_code=400, detail="Mastodon OAuth failed")

    username = info.get("username", "")
    encrypted_token = encrypt_token(token)

    # Get-or-create user by Mastodon identity
    existing_account = session.exec(
        select(MastodonAccount).where(
            MastodonAccount.username == username,
            MastodonAccount.instance_url == instance_url,
        )
    ).first()

    if existing_account:
        # Returning user — refresh their token
        existing_account.access_token = encrypted_token
        session.add(existing_account)
        session.commit()
        user_id = existing_account.user_id
    else:
        # New user — create User + MastodonAccount
        user = User()
        session.add(user)
        session.commit()
        session.refresh(user)

        account = MastodonAccount(
            user_id=user.id,
            instance_url=instance_url,
            access_token=encrypted_token,
            username=username,
        )
        session.add(account)
        session.commit()
        user_id = user.id

    jwt_token = create_access_token(user_id)
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie("access_token", jwt_token, **auth_cookie_params())
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("access_token")
    return response
