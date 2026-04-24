import hmac
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, distinct
from sqlmodel import Session, select

from app.config import get_settings
from app.csrf import verify_csrf
from app.database import get_session
from app.models import Campaign, LoginEvent, MastodonAccount, PageView, Payment, Post, SiteConfig, User
from app.scheduler import update_metrics
from app.site_config import get_banner, set_banner, get_registration_open, set_registration_open

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()
templates.env.globals["settings"] = settings

ADMIN_COOKIE = "admin_session"


def _is_authenticated(request: Request) -> bool:
    secret = settings.admin_secret
    if not secret:
        return False
    cookie = request.cookies.get(ADMIN_COOKIE, "")
    return hmac.compare_digest(cookie, secret)


def _build_stats(session: Session) -> dict:
    now = datetime.utcnow()
    day_ago   = now - timedelta(days=1)
    week_ago  = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    def count(model, where=None):
        q = select(func.count()).select_from(model)
        if where is not None:
            q = q.where(where)
        return session.exec(q).one() or 0

    def unique_visitors(since):
        return session.exec(
            select(func.count(distinct(PageView.visitor_hash)))
            .where(PageView.created_at >= since)
        ).one() or 0

    # 30-day daily visitor bar chart
    chart = []
    for i in range(29, -1, -1):
        day_start = (now - timedelta(days=i)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = day_start + timedelta(days=1)
        n = session.exec(
            select(func.count(distinct(PageView.visitor_hash)))
            .where(PageView.created_at >= day_start, PageView.created_at < day_end)
        ).one() or 0
        chart.append({"date": day_start.strftime("%b %d"), "count": n})

    max_count = max((d["count"] for d in chart), default=1) or 1
    for d in chart:
        d["pct"] = round(d["count"] / max_count * 100)

    # 30-day daily login chart
    login_chart = []
    for i in range(29, -1, -1):
        day_start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        n = session.exec(
            select(func.count()).select_from(LoginEvent)
            .where(LoginEvent.created_at >= day_start, LoginEvent.created_at < day_end)
        ).one() or 0
        login_chart.append({"date": day_start.strftime("%b %d"), "count": n})
    max_logins = max((d["count"] for d in login_chart), default=1) or 1
    for d in login_chart:
        d["pct"] = round(d["count"] / max_logins * 100)

    revenue = session.exec(
        select(func.sum(Payment.amount)).where(Payment.status == "paid")
    ).one() or 0.0

    return {
        # Visitors
        "visitors_1d":  unique_visitors(day_ago),
        "visitors_7d":  unique_visitors(week_ago),
        "visitors_30d": unique_visitors(month_ago),
        # Users
        "users_total":  count(User),
        "users_7d":     count(User, User.created_at >= week_ago),
        "users_30d":    count(User, User.created_at >= month_ago),
        # Campaigns
        "campaigns_total":     count(Campaign),
        "campaigns_paid":      count(Campaign, Campaign.payment_status == "paid"),
        "campaigns_draft":     count(Campaign, Campaign.status == "draft"),
        "campaigns_active":    count(Campaign, Campaign.status == "active"),
        "campaigns_running":   count(Campaign, Campaign.status == "running"),
        "campaigns_completed": count(Campaign, Campaign.status == "completed"),
        "campaigns_archived":  count(Campaign, Campaign.status == "archived"),
        "campaigns_paused":    count(Campaign, Campaign.status.in_(["active", "running", "completed"]) & (Campaign.enabled == False)),
        # Revenue
        "revenue_total": round(revenue, 2),
        # Posts
        "posts_total":     count(Post),
        "posts_published": count(Post, Post.status == "published"),
        "posts_failed":    count(Post, Post.status == "failed"),
        "posts_pending_running": session.exec(
            select(func.count()).select_from(Post)
            .join(Campaign, Post.campaign_id == Campaign.id)
            .where(Post.status == "pending",
                   Campaign.status.in_(["active", "running"]),
                   Campaign.enabled == True)
        ).one() or 0,
        "posts_pending_paused": session.exec(
            select(func.count()).select_from(Post)
            .join(Campaign, Post.campaign_id == Campaign.id)
            .where(Post.status == "pending",
                   Campaign.status.in_(["active", "running"]),
                   Campaign.enabled == False)
        ).one() or 0,
        "posts_pending_unpaid": session.exec(
            select(func.count()).select_from(Post)
            .join(Campaign, Post.campaign_id == Campaign.id)
            .where(Post.status == "pending",
                   Campaign.payment_status != "paid")
        ).one() or 0,
        # Charts
        "chart": chart,
        "login_chart": login_chart,
    }


@router.get("", response_class=HTMLResponse)
def admin_page(
    request: Request,
    token: str = "",
    session: Session = Depends(get_session),
):
    secret = settings.admin_secret

    # Token supplied in URL → validate, set cookie, redirect clean
    if token:
        if secret and hmac.compare_digest(token, secret):
            response = RedirectResponse("/admin", status_code=302)
            response.set_cookie(
                ADMIN_COOKIE, token, httponly=True, samesite="strict",
                secure=(settings.app_env == "production"),
            )
            return response
        return templates.TemplateResponse(
            "admin.html", {"request": request, "auth": False, "error": True}
        )

    if not _is_authenticated(request):
        return templates.TemplateResponse(
            "admin.html", {"request": request, "auth": False, "error": False}
        )

    stats = _build_stats(session)

    campaigns_list = session.exec(select(Campaign).order_by(Campaign.created_at.desc())).all()
    mastodon_accounts = session.exec(select(MastodonAccount)).all()
    account_by_user = {a.user_id: a for a in mastodon_accounts}

    return templates.TemplateResponse(
        "admin.html", {
            "request": request, "auth": True, "now": datetime.utcnow,
            "campaigns_list": campaigns_list, "account_by_user": account_by_user,
            "banner_text": get_banner(),
            "registration_open": get_registration_open(),
            **stats,
        }
    )


@router.post("/collect-metrics")
async def collect_metrics_now(
    request: Request,
    _csrf: None = Depends(verify_csrf),
):
    if not _is_authenticated(request):
        return RedirectResponse("/admin", status_code=302)
    await update_metrics()
    logger.info("Admin triggered manual metrics collection")
    return RedirectResponse("/admin", status_code=302)


@router.post("/banner")
def update_banner(
    request: Request,
    banner_text: str = Form(""),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    if not _is_authenticated(request):
        return RedirectResponse("/admin", status_code=302)
    text = banner_text.strip()
    cfg = session.exec(select(SiteConfig).where(SiteConfig.key == "banner_text")).first()
    if cfg:
        cfg.value = text
    else:
        cfg = SiteConfig(key="banner_text", value=text)
    session.add(cfg)
    session.commit()
    set_banner(text)
    logger.info(f"Admin updated banner: {text!r}")
    return RedirectResponse("/admin", status_code=302)


@router.post("/registration")
def update_registration(
    request: Request,
    registration_open: str = Form("false"),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    if not _is_authenticated(request):
        return RedirectResponse("/admin", status_code=302)
    value = registration_open == "true"
    cfg = session.exec(select(SiteConfig).where(SiteConfig.key == "registration_open")).first()
    if cfg:
        cfg.value = str(value).lower()
    else:
        cfg = SiteConfig(key="registration_open", value=str(value).lower())
    session.add(cfg)
    session.commit()
    set_registration_open(value)
    logger.info(f"Admin set registration_open={value}")
    return RedirectResponse("/admin", status_code=302)


@router.post("/campaigns/mark-paid")
def mark_campaign_paid(
    request: Request,
    campaign_id: int = Form(...),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    if not _is_authenticated(request):
        return RedirectResponse("/admin", status_code=302)
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        return RedirectResponse("/admin", status_code=302)
    campaign.payment_status = "paid"
    campaign.status = "active"
    if not campaign.start_date:
        campaign.start_date = datetime.utcnow()
    session.add(campaign)
    session.commit()
    logger.info(f"Admin manually marked campaign {campaign_id} as paid")
    return RedirectResponse("/admin", status_code=302)


@router.post("/settings")
def update_settings(
    request: Request,
    campaign_price: str = Form(...),
    campaign_price_suggested: str = Form(...),
    campaign_currency: str = Form(...),
    openai_model: str = Form(...),
    openai_max_posts_per_campaign: int = Form(...),
    openai_max_tokens: int = Form(...),
    max_posts_per_campaign: int = Form(...),
    export_max_per_campaign: int = Form(...),
    metrics_max_days: int = Form(...),
    metrics_max_posts_per_user: int = Form(...),
    _csrf: None = Depends(verify_csrf),
):
    if not _is_authenticated(request):
        return RedirectResponse("/admin", status_code=302)
    settings.campaign_price = campaign_price.strip()
    settings.campaign_price_suggested = campaign_price_suggested.strip()
    settings.campaign_currency = campaign_currency.strip().upper()
    settings.openai_model = openai_model.strip()
    settings.openai_max_posts_per_campaign = openai_max_posts_per_campaign
    settings.openai_max_tokens = openai_max_tokens
    settings.max_posts_per_campaign = max_posts_per_campaign
    settings.export_max_per_campaign = export_max_per_campaign
    settings.metrics_max_days = metrics_max_days
    settings.metrics_max_posts_per_user = metrics_max_posts_per_user
    logger.info(
        f"Admin updated in-memory settings: price={settings.campaign_price} "
        f"currency={settings.campaign_currency} model={settings.openai_model} "
        f"max_posts={settings.openai_max_posts_per_campaign}"
    )
    return RedirectResponse("/admin", status_code=302)


@router.post("/campaigns/reset-ai-counter")
def reset_ai_counter(
    request: Request,
    campaign_id: int = Form(...),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
):
    if not _is_authenticated(request):
        return RedirectResponse("/admin", status_code=302)
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        return RedirectResponse("/admin", status_code=302)
    campaign.ai_posts_generated = 0
    campaign.export_generations = 0
    session.add(campaign)
    session.commit()
    logger.info(f"Admin reset AI counter for campaign {campaign_id}")
    return RedirectResponse("/admin", status_code=302)
