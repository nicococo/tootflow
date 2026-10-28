import json
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import get_settings
from app.database import get_session
from app.models import AccountLink, Campaign, Post, User, UserAIConfig
from app.auth import get_current_user
from app.csrf import verify_csrf
from app.limiter import limiter
from sqlalchemy import or_
from app.services.ai_service import generate_posts, regenerate_single_post, refine_single_post, schedule_posts, build_ai_client
from app.routers.campaigns import format_description_for_ai

settings = get_settings()

logger = logging.getLogger(__name__)


def _parse_poll(poll_option: list[str], poll_expires_in: int):
    """Returns (options_json_or_none, expires_in, error_or_none)."""
    clean = [o.strip() for o in poll_option if o.strip()]
    if len(clean) == 0:
        return None, poll_expires_in, None
    if len(clean) == 1:
        return None, poll_expires_in, "A poll needs at least 2 options."
    return json.dumps(clean[:4]), poll_expires_in, None


def _user_ai(session: Session, user: User):
    """Resolve (client, model) for the current user's BYOK config or env fallback."""
    cfg = session.exec(select(UserAIConfig).where(UserAIConfig.user_id == user.id)).first()
    return build_ai_client(cfg)


def _ai_configured(session: Session, user: User) -> bool:
    client, _ = _user_ai(session, user)
    return client is not None

router = APIRouter(prefix="/campaigns")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["settings"] = settings


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


def _can_access(session: Session, campaign: Campaign, user: User) -> bool:
    if campaign.user_id == user.id:
        return True
    return campaign.user_id in _get_linked_user_ids(session, user.id)


@router.get("/{campaign_id}/posts", response_class=HTMLResponse)
def edit_posts_page(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or not _can_access(session, campaign, user):
        raise HTTPException(status_code=404)
    posts = session.exec(
        select(Post).where(Post.campaign_id == campaign_id).order_by(Post.scheduled_time)
    ).all()
    poll_data = {}
    for p in posts:
        if p.poll_options:
            try:
                poll_data[p.id] = json.loads(p.poll_options)
            except Exception:
                pass
    return templates.TemplateResponse(
        "posts_edit.html",
        {"request": request, "user": user, "campaign": campaign, "posts": posts,
         "max_posts": settings.max_posts_per_campaign,
         "ai_max": settings.openai_max_posts_per_campaign,
         "poll_data": poll_data,
         "ai_configured": _ai_configured(session, user)},
    )


@router.post("/{campaign_id}/posts/add")
def add_post(
    campaign_id: int,
    content: str = Form(...),
    scheduled_time: str = Form(...),
    poll_option: list[str] = Form(default=[]),
    poll_expires_in: int = Form(default=86400),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or not _can_access(session, campaign, user):
        raise HTTPException(status_code=404)
    if campaign.status == "archived":
        raise HTTPException(status_code=403, detail="Archived campaigns cannot be modified")
    current_count = session.exec(select(Post).where(Post.campaign_id == campaign_id)).all()
    if len(current_count) >= settings.max_posts_per_campaign:
        raise HTTPException(status_code=400, detail=f"Campaign has reached the maximum of {settings.max_posts_per_campaign} posts")
    opts_json, expires_in, err = _parse_poll(poll_option, poll_expires_in)
    if err:
        raise HTTPException(status_code=400, detail=err)
    sched = datetime.fromisoformat(scheduled_time)
    post = Post(campaign_id=campaign_id, content=content[:500], scheduled_time=sched,
                poll_options=opts_json, poll_expires_in=expires_in)
    session.add(post)
    # Advance campaign status on post add
    if campaign.status == "draft":
        campaign.status = "active"
        session.add(campaign)
    elif campaign.status == "completed":
        campaign.status = "running"
        session.add(campaign)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/posts", status_code=302)


@router.post("/{campaign_id}/posts/{post_id}/edit")
def edit_post(
    campaign_id: int,
    post_id: int,
    content: str = Form(...),
    scheduled_time: str = Form(...),
    poll_option: list[str] = Form(default=[]),
    poll_expires_in: int = Form(default=86400),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or not _can_access(session, campaign, user):
        raise HTTPException(status_code=404)
    if campaign.status == "archived":
        raise HTTPException(status_code=403, detail="Archived campaigns cannot be modified")
    post = session.get(Post, post_id)
    if not post or post.campaign_id != campaign_id:
        raise HTTPException(status_code=404)
    opts_json, expires_in, err = _parse_poll(poll_option, poll_expires_in)
    if err:
        raise HTTPException(status_code=400, detail=err)
    post.content = content[:500]
    post.scheduled_time = datetime.fromisoformat(scheduled_time)
    post.poll_options = opts_json
    post.poll_expires_in = expires_in
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
    if not campaign or not _can_access(session, campaign, user):
        raise HTTPException(status_code=404)
    if campaign.status == "archived":
        raise HTTPException(status_code=403, detail="Archived campaigns cannot be modified")
    if campaign.ai_posts_generated >= settings.openai_max_posts_per_campaign:
        raise HTTPException(status_code=403, detail="AI generation limit reached for this campaign")

    client, model_override = _user_ai(session, user)
    if client is None:
        raise HTTPException(status_code=400, detail="No AI provider configured. Add your API key on the Account page.")

    try:
        post_contents = await generate_posts(
            campaign_name=campaign.name,
            description=format_description_for_ai(campaign.description),
            template_type=campaign.template_type,
            tone=campaign.tone,
            language=campaign.language,
            duration_days=campaign.duration_days,
            posts_per_week=campaign.posts_per_week,
            client=client,
            model=model_override,
        )
        # Delete only pending posts (keep published/failed)
        pending = session.exec(
            select(Post).where(Post.campaign_id == campaign_id, Post.status == "pending")
        ).all()
        for p in pending:
            session.delete(p)
        session.flush()

        # Respect the hard post cap, accounting for already-published/failed posts
        non_pending = session.exec(
            select(Post).where(Post.campaign_id == campaign_id, Post.status != "pending")
        ).all()
        slots = max(0, settings.max_posts_per_campaign - len(non_pending))
        post_contents = post_contents[:slots]

        # Use now if start_date is in the past (e.g. campaign paid but unused for a while)
        start = max(campaign.start_date or datetime.utcnow(), datetime.utcnow())
        for content, sched_time in schedule_posts(post_contents, start, campaign.posts_per_week):
            session.add(Post(campaign_id=campaign_id, content=content, scheduled_time=sched_time))
        campaign.ai_posts_generated += len(post_contents)
        # Advance campaign status on post add
        if campaign.status == "draft":
            campaign.status = "active"
        elif campaign.status == "completed":
            campaign.status = "running"
        session.add(campaign)
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
    if not campaign or not _can_access(session, campaign, user):
        raise HTTPException(status_code=404)
    if campaign.status == "archived":
        raise HTTPException(status_code=403, detail="Archived campaigns cannot be modified")
    if campaign.ai_posts_generated >= settings.openai_max_posts_per_campaign:
        raise HTTPException(status_code=403, detail="AI generation limit reached for this campaign")

    post = session.get(Post, post_id)
    if not post or post.campaign_id != campaign_id or post.status != "pending":
        raise HTTPException(status_code=404)

    client, model_override = _user_ai(session, user)
    if client is None:
        raise HTTPException(status_code=400, detail="No AI provider configured. Add your API key on the Account page.")

    try:
        post.content = await regenerate_single_post(
            campaign_name=campaign.name,
            description=format_description_for_ai(campaign.description),
            template_type=campaign.template_type,
            tone=campaign.tone,
            language=campaign.language,
            client=client,
            model=model_override,
        )
        campaign.ai_posts_generated += 1
        session.add(post)
        session.add(campaign)
        session.commit()
    except Exception as e:
        logger.error(f"AI regeneration failed for post {post_id}: {e}")
        return RedirectResponse(
            f"/campaigns/{campaign_id}/posts?error=ai_failed",
            status_code=302,
        )

    return RedirectResponse(f"/campaigns/{campaign_id}/posts", status_code=302)


@router.post("/{campaign_id}/posts/{post_id}/refine")
@limiter.limit("30/hour")
async def refine_post(
    campaign_id: int,
    post_id: int,
    request: Request,
    instructions: str = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    """Refine a single pending post based on user instructions."""
    campaign = session.get(Campaign, campaign_id)
    if not campaign or not _can_access(session, campaign, user):
        raise HTTPException(status_code=404)
    if campaign.status == "archived":
        raise HTTPException(status_code=403, detail="Archived campaigns cannot be modified")
    if campaign.ai_posts_generated >= settings.openai_max_posts_per_campaign:
        raise HTTPException(status_code=403, detail="AI generation limit reached for this campaign")

    post = session.get(Post, post_id)
    if not post or post.campaign_id != campaign_id or post.status != "pending":
        raise HTTPException(status_code=404)

    instructions = instructions.strip()[:300]
    if not instructions:
        raise HTTPException(status_code=400, detail="Instructions cannot be empty")

    client, model_override = _user_ai(session, user)
    if client is None:
        raise HTTPException(status_code=400, detail="No AI provider configured. Add your API key on the Account page.")

    try:
        poll_opts = json.loads(post.poll_options) if post.poll_options else None
        post.content = await refine_single_post(
            current_content=post.content,
            instructions=instructions,
            campaign_name=campaign.name,
            description=format_description_for_ai(campaign.description),
            template_type=campaign.template_type,
            tone=campaign.tone,
            language=campaign.language,
            poll_options=poll_opts,
            client=client,
            model=model_override,
        )
        campaign.ai_posts_generated += 1
        session.add(post)
        session.add(campaign)
        session.commit()
    except Exception as e:
        logger.error(f"AI refinement failed for post {post_id}: {e}")
        return RedirectResponse(
            f"/campaigns/{campaign_id}/posts?error=ai_failed#post-{post_id}",
            status_code=302,
        )

    return RedirectResponse(f"/campaigns/{campaign_id}/posts#post-{post_id}", status_code=302)


@router.post("/{campaign_id}/posts/{post_id}/delete")
def delete_post(
    campaign_id: int,
    post_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or not _can_access(session, campaign, user):
        raise HTTPException(status_code=404)
    if campaign.status == "archived":
        raise HTTPException(status_code=403, detail="Archived campaigns cannot be modified")
    post = session.get(Post, post_id)
    if not post or post.campaign_id != campaign_id:
        raise HTTPException(status_code=404)
    session.delete(post)
    session.flush()

    # Re-check completion: running → completed if no pending posts remain and below max
    if campaign.status == "running":
        remaining = session.exec(
            select(Post).where(Post.campaign_id == campaign_id)
        ).all()
        has_pending = any(p.status == "pending" for p in remaining)
        at_limit = len(remaining) >= settings.max_posts_per_campaign
        if remaining and not has_pending and not at_limit:
            campaign.status = "completed"
            session.add(campaign)

    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/posts", status_code=302)
