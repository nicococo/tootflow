import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from slowapi import _rate_limit_exceeded_handler
from slowapi.middleware import SlowAPIMiddleware
from slowapi.errors import RateLimitExceeded
from sqlmodel import Session, select

from app.config import get_settings
from app.csrf import CSRFMiddleware
from app.limiter import limiter
from app.database import create_db_and_tables, get_session
from app.models import Campaign, MastodonAccount, User
from app.auth import optional_current_user, get_current_user
from app.scheduler import start_scheduler, stop_scheduler
from app.routers import auth, campaigns, posts, payments, metrics, account, admin

logger = logging.getLogger(__name__)
settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup security checks
    if not settings.lemonsqueezy_webhook_secret:
        logger.critical("LEMONSQUEEZY_WEBHOOK_SECRET is not set — webhook endpoint is insecure")
    if settings.secret_key in ("changeme", "changeme-generate-a-long-random-string"):
        logger.critical("SECRET_KEY is set to the default placeholder — JWTs are forgeable")

    create_db_and_tables()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="TootFlow", lifespan=lifespan)

# Rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Middleware (applied in reverse order — CSRF runs first, then security headers)
app.add_middleware(CSRFMiddleware)
app.add_middleware(SlowAPIMiddleware)


@app.middleware("http")
async def track_pageview(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    # Only track GET requests; skip admin, webhooks, and OAuth callbacks
    if (request.method == "GET"
            and not path.startswith("/admin")
            and not path.startswith("/webhooks")
            and path != "/mastodon/callback"):
        try:
            from app.database import engine
            from app.models import PageView
            from sqlmodel import Session as SyncSession
            ip = (request.client.host if request.client else "") or ""
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
            visitor_hash = hashlib.sha256(f"{ip}:{date_str}".encode()).hexdigest()
            with SyncSession(engine) as db:
                db.add(PageView(visitor_hash=visitor_hash, path=path))
                db.commit()
        except Exception:
            pass  # never let analytics break the response
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Inline styles/scripts are used in templates; unsafe-inline is required
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline';"
    )
    return response


# Routers
app.include_router(auth.router)
app.include_router(campaigns.router)
app.include_router(posts.router)
app.include_router(payments.router)
app.include_router(metrics.router)
app.include_router(account.router)
app.include_router(admin.router)

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["now"] = datetime.utcnow


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, session: Session = Depends(get_session)):
    user = optional_current_user(request, session)
    return templates.TemplateResponse("privacy.html", {"request": request, "user": user})


@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    user = optional_current_user(request, session)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    campaigns_list = session.exec(
        select(Campaign).where(Campaign.user_id == user.id).order_by(Campaign.created_at.desc())
    ).all()
    mastodon = session.exec(select(MastodonAccount).where(MastodonAccount.user_id == user.id)).first()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "campaigns": campaigns_list, "mastodon": mastodon},
    )
