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
from app.models import Campaign, MastodonAccount, PageView, Payment, Post, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()

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
        "campaigns_total":  count(Campaign),
        "campaigns_paid":   count(Campaign, Campaign.payment_status == "paid"),
        "campaigns_active": count(Campaign, Campaign.status.in_(["active", "running"])),
        # Revenue
        "revenue_total": round(revenue, 2),
        # Posts
        "posts_published": count(Post, Post.status == "published"),
        "posts_pending":   count(Post, Post.status == "pending"),
        "posts_failed":    count(Post, Post.status == "failed"),
        # Chart
        "chart": chart,
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
            **stats,
        }
    )


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
