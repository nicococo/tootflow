import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import get_current_user
from app.csrf import verify_csrf
from app.database import get_session
from app.models import Campaign, MastodonAccount, Metrics, Payment, Post, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/account")
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
def account_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    mastodon = session.exec(
        select(MastodonAccount).where(MastodonAccount.user_id == user.id)
    ).first()
    campaign_count = len(
        session.exec(select(Campaign).where(Campaign.user_id == user.id)).all()
    )
    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "user": user,
            "mastodon": mastodon,
            "campaign_count": campaign_count,
        },
    )


@router.get("/export")
def export_data(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Download all personal data as JSON (GDPR Art. 20)."""
    mastodon = session.exec(
        select(MastodonAccount).where(MastodonAccount.user_id == user.id)
    ).first()
    campaigns = session.exec(
        select(Campaign).where(Campaign.user_id == user.id)
    ).all()

    export = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "account": {
            "mastodon_username": mastodon.username if mastodon else None,
            "mastodon_instance": mastodon.instance_url if mastodon else None,
            "member_since": user.created_at.isoformat() + "Z",
        },
        "campaigns": [],
    }

    for c in campaigns:
        posts = session.exec(
            select(Post).where(Post.campaign_id == c.id).order_by(Post.scheduled_time)
        ).all()
        posts_data = []
        for p in posts:
            m = session.exec(select(Metrics).where(Metrics.post_id == p.id)).first()
            posts_data.append({
                "content": p.content,
                "scheduled_time": p.scheduled_time.isoformat(),
                "status": p.status,
                "published_at": p.published_at.isoformat() if p.published_at else None,
                "mastodon_status_id": p.mastodon_status_id,
                "metrics": {
                    "favourites": m.favourites,
                    "reblogs": m.reblogs,
                    "replies": m.replies,
                } if m else None,
            })

        export["campaigns"].append({
            "name": c.name,
            "description": c.description,
            "template_type": c.template_type,
            "tone": c.tone,
            "language": c.language,
            "status": c.status,
            "payment_status": c.payment_status,
            "created_at": c.created_at.isoformat(),
            "start_date": c.start_date.isoformat() if c.start_date else None,
            "end_date": c.end_date.isoformat() if c.end_date else None,
            "posts": posts_data,
        })

    payload = json.dumps(export, indent=2)
    filename = f"tootflow-export-{datetime.utcnow().strftime('%Y%m%d')}.json"
    return StreamingResponse(
        iter([payload]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/delete")
def delete_account(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    """Permanently delete the account and all associated data (GDPR Art. 17)."""
    # Delete in FK-safe order: Metrics → Posts → Payments → Campaigns → MastodonAccount → User
    campaigns = session.exec(
        select(Campaign).where(Campaign.user_id == user.id)
    ).all()
    for campaign in campaigns:
        posts = session.exec(select(Post).where(Post.campaign_id == campaign.id)).all()
        for post in posts:
            metrics = session.exec(
                select(Metrics).where(Metrics.post_id == post.id)
            ).first()
            if metrics:
                session.delete(metrics)
            session.delete(post)
        payment = session.exec(
            select(Payment).where(Payment.campaign_id == campaign.id)
        ).first()
        if payment:
            session.delete(payment)
        session.delete(campaign)

    mastodon = session.exec(
        select(MastodonAccount).where(MastodonAccount.user_id == user.id)
    ).first()
    if mastodon:
        session.delete(mastodon)

    session.delete(user)
    session.commit()

    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("access_token")
    return response
