import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import _rate_limit_exceeded_handler
from slowapi.middleware import SlowAPIMiddleware
from slowapi.errors import RateLimitExceeded
from sqlmodel import Session, select

from app.config import get_settings
from app.csrf import CSRFMiddleware
from app.limiter import limiter
from app.database import create_db_and_tables, get_session, engine
from sqlalchemy import func
from app.models import Campaign, MastodonAccount, Metrics, Post, User
from app.auth import optional_current_user, get_current_user
from app.scheduler import start_scheduler, stop_scheduler
from app.site_config import get_banner, load_from_db
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
    with Session(engine) as s:
        load_from_db(s)
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="TootFlow", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Middleware (applied in reverse order — CSRF runs first, then security headers)
app.add_middleware(CSRFMiddleware)
app.add_middleware(SlowAPIMiddleware)


@app.middleware("http")
async def inject_banner(request: Request, call_next):
    request.state.banner_text = get_banner()
    return await call_next(request)


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


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt(request: Request):
    base = str(request.base_url).rstrip("/")
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /webhooks/\n"
        "Disallow: /mastodon/callback\n"
        "Disallow: /dashboard\n"
        "Disallow: /campaigns/\n"
        "Disallow: /account\n"
        f"\nSitemap: {base}/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
def sitemap_xml(request: Request):
    base = str(request.base_url).rstrip("/")
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{base}/</loc><changefreq>monthly</changefreq><priority>1.0</priority></url>\n"
        f"  <url><loc>{base}/login</loc><changefreq>yearly</changefreq><priority>0.3</priority></url>\n"
        f"  <url><loc>{base}/privacy</loc><changefreq>yearly</changefreq><priority>0.2</priority></url>\n"
        "</urlset>\n"
    )
    return Response(content=content, media_type="application/xml")


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, session: Session = Depends(get_session)):
    user = optional_current_user(request, session)
    return templates.TemplateResponse("privacy.html", {"request": request, "user": user})


@app.get("/impressum", response_class=HTMLResponse)
def impressum(request: Request, session: Session = Depends(get_session)):
    user = optional_current_user(request, session)
    return templates.TemplateResponse("impressum.html", {"request": request, "user": user})


@app.get("/tos", response_class=HTMLResponse)
def tos(request: Request, session: Session = Depends(get_session)):
    user = optional_current_user(request, session)
    return templates.TemplateResponse("tos.html", {"request": request, "user": user})


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

    campaign_ids = [c.id for c in campaigns_list]
    published_count = 0
    active_count = 0
    paused_count = 0
    next_post = None
    next_post_campaign = None

    if campaign_ids:
        published_count = session.exec(
            select(func.count()).select_from(Post)
            .where(Post.campaign_id.in_(campaign_ids), Post.status == "published")
        ).one() or 0
        active_count = sum(1 for c in campaigns_list if c.status in ("active", "running") and c.enabled)
        paused_count = sum(1 for c in campaigns_list if c.status in ("active", "running") and not c.enabled)
        active_campaign_ids = [c.id for c in campaigns_list if c.status in ("active", "running") and c.enabled]
        next_post = session.exec(
            select(Post)
            .where(Post.campaign_id.in_(active_campaign_ids), Post.status == "pending")
            .order_by(Post.scheduled_time)
        ).first() if active_campaign_ids else None
        if next_post:
            next_post_campaign = next(c for c in campaigns_list if c.id == next_post.campaign_id)

    # Engagement score per campaign: (favs + reblogs + replies) / published posts
    engagement_by_campaign: dict[int, float] = {}
    if campaign_ids:
        pub_posts = session.exec(
            select(Post).where(Post.campaign_id.in_(campaign_ids), Post.status == "published")
        ).all()
        if pub_posts:
            post_ids = [p.id for p in pub_posts]
            metrics_map = {
                m.post_id: m
                for m in session.exec(select(Metrics).where(Metrics.post_id.in_(post_ids))).all()
            }
            from collections import defaultdict
            posts_by_campaign: dict = defaultdict(list)
            for p in pub_posts:
                posts_by_campaign[p.campaign_id].append(p)
            for cid, posts in posts_by_campaign.items():
                total = sum(
                    (metrics_map[p.id].favourites + metrics_map[p.id].reblogs + metrics_map[p.id].replies)
                    for p in posts if p.id in metrics_map
                )
                engagement_by_campaign[cid] = round(total / len(posts), 1)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request, "user": user, "campaigns": campaigns_list, "mastodon": mastodon,
            "published_count": published_count, "active_count": active_count, "paused_count": paused_count,
            "next_post": next_post, "next_post_campaign": next_post_campaign,
            "engagement_by_campaign": engagement_by_campaign,
        },
    )
