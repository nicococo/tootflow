import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import get_current_user
from app.config import get_settings
from app.csrf import verify_csrf
from app.database import get_session
from app.models import AccountLink, Campaign, FollowerSnapshot, MastodonAccount, Metrics, Payment, Post, User
from sqlalchemy import or_

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/account")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["settings"] = get_settings()


def _get_linked_accounts(session: Session, user_id: int) -> list[MastodonAccount]:
    links = session.exec(
        select(AccountLink).where(
            or_(AccountLink.user_id == user_id, AccountLink.linked_user_id == user_id)
        )
    ).all()
    linked_ids = [
        link.linked_user_id if link.user_id == user_id else link.user_id
        for link in links
    ]
    if not linked_ids:
        return []
    return session.exec(
        select(MastodonAccount).where(MastodonAccount.user_id.in_(linked_ids))
    ).all()


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
    linked_accounts = _get_linked_accounts(session, user.id)
    linked = request.query_params.get("linked") == "1"
    link_error = request.query_params.get("link_error") == "1"
    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "user": user,
            "mastodon": mastodon,
            "campaign_count": campaign_count,
            "linked_accounts": linked_accounts,
            "linked": linked,
            "link_error": link_error,
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
    payments = session.exec(
        select(Payment).where(Payment.user_id == user.id).order_by(Payment.created_at)
    ).all()
    follower_snapshots = session.exec(
        select(FollowerSnapshot).where(FollowerSnapshot.user_id == user.id).order_by(FollowerSnapshot.recorded_at)
    ).all()

    export = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "account": {
            "mastodon_username": mastodon.username if mastodon else None,
            "mastodon_instance": mastodon.instance_url if mastodon else None,
            "member_since": user.created_at.isoformat() + "Z",
        },
        "payments": [
            {
                "lemonsqueezy_order_id": p.lemonsqueezy_order_id,
                "amount": p.amount,
                "currency": p.currency,
                "status": p.status,
                "created_at": p.created_at.isoformat() + "Z",
            }
            for p in payments
        ],
        "follower_snapshots": [
            {
                "count": s.count,
                "recorded_at": s.recorded_at.isoformat() + "Z",
            }
            for s in follower_snapshots
        ],
        "campaigns": [],
    }

    for c in campaigns:
        posts = session.exec(
            select(Post).where(Post.campaign_id == c.id).order_by(Post.scheduled_time)
        ).all()
        posts_data = []
        for p in posts:
            m = session.exec(select(Metrics).where(Metrics.post_id == p.id)).first()
            poll = json.loads(p.poll_options) if p.poll_options else None
            posts_data.append({
                "content": p.content,
                "scheduled_time": p.scheduled_time.isoformat() + "Z",
                "status": p.status,
                "created_at": p.created_at.isoformat() + "Z",
                "published_at": p.published_at.isoformat() + "Z" if p.published_at else None,
                "mastodon_status_id": p.mastodon_status_id,
                "poll": {"options": poll, "expires_in_seconds": p.poll_expires_in} if poll else None,
                "metrics": {
                    "favourites": m.favourites,
                    "reblogs": m.reblogs,
                    "replies": m.replies,
                    "poll_voters": m.poll_voters,
                    "last_updated": m.last_updated.isoformat() + "Z",
                } if m else None,
            })

        try:
            description_parsed = json.loads(c.description) if c.description else None
        except (json.JSONDecodeError, ValueError):
            description_parsed = c.description or None

        try:
            export_drafts = json.loads(c.export_drafts) if c.export_drafts else {}
        except (json.JSONDecodeError, ValueError):
            export_drafts = {}

        export["campaigns"].append({
            "name": c.name,
            "description": description_parsed,
            "template_type": c.template_type,
            "tone": c.tone,
            "language": c.language,
            "duration_days": c.duration_days,
            "posts_per_week": c.posts_per_week,
            "status": c.status,
            "enabled": c.enabled,
            "payment_status": c.payment_status,
            "ai_posts_generated": c.ai_posts_generated,
            "export_generations": c.export_generations,
            "export_drafts": export_drafts,
            "created_at": c.created_at.isoformat() + "Z",
            "start_date": c.start_date.isoformat() + "Z" if c.start_date else None,
            "end_date": c.end_date.isoformat() + "Z" if c.end_date else None,
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

    for snapshot in session.exec(
        select(FollowerSnapshot).where(FollowerSnapshot.user_id == user.id)
    ).all():
        session.delete(snapshot)

    session.delete(user)
    session.commit()

    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("access_token")
    return response
