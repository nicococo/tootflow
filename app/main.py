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
from app.models import AccountLink, Campaign, MastodonAccount, Metrics, Post, User, XAccount
from sqlalchemy import or_
from app.auth import optional_current_user, get_current_user
from app.scheduler import start_scheduler, stop_scheduler
from app.site_config import get_banner, load_from_db
from app.routers import auth, campaigns, posts, metrics, account, admin
from app.routers.metrics import post_router as metrics_post_router

logger = logging.getLogger(__name__)
settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup security checks
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


# Paths that are never meaningful visitor signals
_TRACKING_SKIP_PATHS = frozenset({
    "/health", "/favicon.ico", "/robots.txt", "/sitemap.xml",
})
_TRACKING_SKIP_PREFIXES = ("/static/", "/admin", "/webhooks", "/mastodon/callback")

# Internal IPs — Docker healthcheck, loopback, private ranges
_TRACKING_SKIP_IPS = frozenset({"127.0.0.1", "::1", "localhost"})

# Bot/crawler keyword fragments matched against lowercased User-Agent
_BOT_UA_KEYWORDS = frozenset({
    "bot", "crawler", "spider", "scraper", "slurp",
    "googlebot", "bingbot", "duckduckbot", "baiduspider", "yandexbot",
    "facebookexternalhit", "twitterbot", "linkedinbot", "whatsapp",
    "curl", "wget", "python-requests", "python-httpx", "python/",
    "go-http-client", "java/", "ruby/", "scrapy", "axios",
    "pingdom", "uptimerobot", "statuscake", "freshping",
    "semrushbot", "ahrefsbot", "mj12bot", "dotbot",
    "lighthouse", "headlesschrome", "phantomjs", "selenium",
})


@app.middleware("http")
async def track_pageview(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path

    if request.method != "GET":
        return response
    if path in _TRACKING_SKIP_PATHS:
        return response
    if any(path.startswith(p) for p in _TRACKING_SKIP_PREFIXES):
        return response

    ip = (request.client.host if request.client else "") or ""
    if ip in _TRACKING_SKIP_IPS:
        return response

    ua = request.headers.get("user-agent", "").lower()
    if not ua or any(kw in ua for kw in _BOT_UA_KEYWORDS):
        return response

    try:
        from app.database import engine
        from app.models import PageView
        from sqlmodel import Session as SyncSession
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
app.include_router(metrics.router)
app.include_router(metrics_post_router)
app.include_router(account.router)
app.include_router(admin.router)

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["now"] = datetime.utcnow
templates.env.globals["settings"] = settings


@app.get("/health")
def health():
    return {"ok": True}


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
    return templates.TemplateResponse("privacy.html", {"request": request, "user": user, "settings": get_settings()})


@app.get("/impressum", response_class=HTMLResponse)
def impressum(request: Request, session: Session = Depends(get_session)):
    user = optional_current_user(request, session)
    return templates.TemplateResponse("impressum.html", {"request": request, "user": user, "settings": get_settings()})


@app.get("/tos", response_class=HTMLResponse)
def tos(request: Request, session: Session = Depends(get_session)):
    user = optional_current_user(request, session)
    return templates.TemplateResponse("tos.html", {"request": request, "user": user, "settings": get_settings()})


@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    user = optional_current_user(request, session)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})


def _get_linked_user_ids(session: Session, user_id: int) -> list[int]:
    links = session.exec(
        select(AccountLink).where(
            or_(AccountLink.user_id == user_id, AccountLink.linked_user_id == user_id)
        )
    ).all()
    return [
        link.linked_user_id if link.user_id == user_id else link.user_id
        for link in links
    ]


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    linked_user_ids = _get_linked_user_ids(session, user.id)
    all_user_ids = [user.id] + linked_user_ids

    campaigns_list = session.exec(
        select(Campaign).where(Campaign.user_id.in_(all_user_ids)).order_by(Campaign.created_at.desc())
    ).all()

    mastodon = session.exec(select(MastodonAccount).where(MastodonAccount.user_id == user.id)).first()

    # Build account label map for all campaigns
    all_mastodon = session.exec(
        select(MastodonAccount).where(MastodonAccount.user_id.in_(all_user_ids))
    ).all()
    mastodon_by_id = {acc.id: acc for acc in all_mastodon}
    mastodon_by_user = {acc.user_id: acc for acc in all_mastodon}
    all_x = session.exec(
        select(XAccount).where(XAccount.user_id.in_(all_user_ids))
    ).all()
    x_by_id = {acc.id: acc for acc in all_x}

    def _account_label(acc: MastodonAccount | None) -> str | None:
        if not acc:
            return None
        return f"@{acc.username}@{acc.instance_url.replace('https://','').replace('http://','')}"

    account_label_by_campaign: dict[int, str | None] = {}
    for c in campaigns_list:
        if c.x_account_id and c.x_account_id in x_by_id:
            account_label_by_campaign[c.id] = f"𝕏 @{x_by_id[c.x_account_id].username}"
            continue
        acc = mastodon_by_id.get(c.mastodon_account_id) if c.mastodon_account_id else mastodon_by_user.get(c.user_id)
        account_label_by_campaign[c.id] = _account_label(acc)

    linked_accounts = [mastodon_by_user[uid] for uid in linked_user_ids if uid in mastodon_by_user]

    # Assign a distinct color to each account — current user always gets teal (index 0)
    _ACCOUNT_PALETTE = ["#47b8b1", "#d95f2a", "#7b5ea7", "#2d7d5f", "#b8732a", "#3a6fa8"]
    account_color: dict[int, str] = {}
    if mastodon:
        account_color[user.id] = _ACCOUNT_PALETTE[0]
    for i, acc in enumerate(linked_accounts):
        account_color[acc.user_id] = _ACCOUNT_PALETTE[(i + 1) % len(_ACCOUNT_PALETTE)]

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
            "linked_accounts": linked_accounts,
            "account_label_by_campaign": account_label_by_campaign,
            "account_color": account_color,
            "own_user_id": user.id,
        },
    )
