import logging
from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import get_session
from app.models import Campaign, Post, User
from app.auth import get_current_user
from app.csrf import verify_csrf
from app.limiter import limiter
from app.services.ai_service import generate_posts, regenerate_single_post, schedule_posts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaigns")
templates = Jinja2Templates(directory="app/templates")


@router.get("/{campaign_id}/posts", response_class=HTMLResponse)
def edit_posts_page(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    posts = session.exec(
        select(Post).where(Post.campaign_id == campaign_id).order_by(Post.scheduled_time)
    ).all()
    return templates.TemplateResponse(
        "posts_edit.html",
        {"request": request, "user": user, "campaign": campaign, "posts": posts},
    )


@router.post("/{campaign_id}/posts/add")
def add_post(
    campaign_id: int,
    content: str = Form(...),
    scheduled_time: str = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    sched = datetime.fromisoformat(scheduled_time)
    post = Post(campaign_id=campaign_id, content=content[:500], scheduled_time=sched)
    session.add(post)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/posts", status_code=302)


@router.post("/{campaign_id}/posts/{post_id}/edit")
def edit_post(
    campaign_id: int,
    post_id: int,
    content: str = Form(...),
    scheduled_time: str = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    post = session.get(Post, post_id)
    if not post or post.campaign_id != campaign_id:
        raise HTTPException(status_code=404)
    post.content = content[:500]
    post.scheduled_time = datetime.fromisoformat(scheduled_time)
    session.add(post)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/posts", status_code=302)


@router.post("/{campaign_id}/generate")
@limiter.limit("10/hour")
async def generate_campaign_posts(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    """Generate all posts for a paid campaign via AI, replacing existing pending posts."""
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    if campaign.payment_status != "paid":
        raise HTTPException(status_code=403, detail="Campaign must be activated before generating posts")

    try:
        post_contents = generate_posts(
            campaign_name=campaign.name,
            description=campaign.description,
            template_type=campaign.template_type,
            tone=campaign.tone,
            language=campaign.language,
            duration_days=campaign.duration_days,
            posts_per_week=campaign.posts_per_week,
        )
        # Delete only pending posts (keep published/failed)
        pending = session.exec(
            select(Post).where(Post.campaign_id == campaign_id, Post.status == "pending")
        ).all()
        for p in pending:
            session.delete(p)

        # Schedule from campaign start_date (set at payment time)
        start = campaign.start_date or datetime.utcnow()
        for content, sched_time in schedule_posts(post_contents, start, campaign.posts_per_week):
            session.add(Post(campaign_id=campaign_id, content=content, scheduled_time=sched_time))
        session.commit()
    except Exception as e:
        logger.error(f"AI campaign generation failed for campaign {campaign_id}: {e}")
        raise HTTPException(status_code=500, detail="AI generation failed — please try again")

    return RedirectResponse(f"/campaigns/{campaign_id}/posts", status_code=302)


@router.post("/{campaign_id}/posts/{post_id}/regenerate")
@limiter.limit("30/hour")
async def regenerate_post(
    campaign_id: int,
    post_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    """Regenerate the content of a single pending post via AI."""
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    if campaign.payment_status != "paid":
        raise HTTPException(status_code=403, detail="Campaign must be activated before regenerating posts")

    post = session.get(Post, post_id)
    if not post or post.campaign_id != campaign_id or post.status != "pending":
        raise HTTPException(status_code=404)

    try:
        post.content = regenerate_single_post(
            campaign_name=campaign.name,
            description=campaign.description,
            template_type=campaign.template_type,
            tone=campaign.tone,
            language=campaign.language,
        )
        session.add(post)
        session.commit()
    except Exception as e:
        logger.error(f"AI regeneration failed for post {post_id}: {e}")
        raise HTTPException(status_code=500, detail="AI regeneration failed — please try again")

    return RedirectResponse(f"/campaigns/{campaign_id}/posts", status_code=302)


@router.post("/{campaign_id}/posts/{post_id}/delete")
def delete_post(
    campaign_id: int,
    post_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    post = session.get(Post, post_id)
    if not post or post.campaign_id != campaign_id:
        raise HTTPException(status_code=404)
    session.delete(post)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/posts", status_code=302)
