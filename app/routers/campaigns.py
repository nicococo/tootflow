import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import Session, select

from app.config import get_settings
from app.database import get_session
from app.models import Campaign, Metrics, Post, User, MastodonAccount
from app.auth import get_current_user
from app.csrf import verify_csrf
from app.limiter import limiter
from app.services.ai_service import generate_newsletter_digest, generate_linkedin_summary

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/campaigns")
settings = get_settings()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["settings"] = settings

FREE_CAMPAIGN_LIMIT = 1
# "completed" is intentionally excluded: finished unpaid campaigns free up the slot
FREE_ACTIVE_STATUSES = ("draft", "active", "running")


def count_active_unpaid_campaigns(session: Session, user_id: int) -> int:
    """Count campaigns that occupy the user's free slot.
    Only draft/active/running + unpaid + enabled campaigns count.
    Completed and disabled (e.g. refunded) campaigns do not block the slot."""
    result = session.exec(
        select(func.count()).select_from(Campaign).where(
            Campaign.user_id == user_id,
            Campaign.payment_status == "unpaid",
            Campaign.status.in_(FREE_ACTIVE_STATUSES),
            Campaign.enabled == True,
        )
    ).one()
    return result

TEMPLATES = [
    "product_launch",
    "open_source_release",
    "event_promotion",
    "knowledge_sharing",
    "developer_tips",
    "research_insights",
    "personal_brand",
    "community_building",
    "project_showcase",
    "newsletter_growth",
]
TONES = [
    "professional",
    "casual",
    "technical",
    "inspiring",
    "humorous",
    "riddles",
    "storytelling",
    "hot take",
    "minimalist",
    "socratic",
    "behind the scenes",
    "rant",
]
LANGUAGES = ["English", "German", "French"]


@router.get("", response_class=HTMLResponse)
def list_campaigns(request: Request, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    campaigns = session.exec(select(Campaign).where(Campaign.user_id == user.id).order_by(Campaign.created_at.desc())).all()
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "campaigns": campaigns})


@router.get("/new", response_class=HTMLResponse)
def new_campaign_page(request: Request, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    existing = session.exec(select(Campaign).where(Campaign.user_id == user.id).order_by(Campaign.created_at.desc())).all()
    active_unpaid = count_active_unpaid_campaigns(session, user.id)
    return templates.TemplateResponse(
        "campaign_create.html",
        {
            "request": request,
            "user": user,
            "templates": TEMPLATES,
            "tones": TONES,
            "languages": LANGUAGES,
            "existing_campaigns": existing,
            "free_limit_hit": active_unpaid >= FREE_CAMPAIGN_LIMIT,
        },
    )


@router.post("/new")
@limiter.limit("10/hour")
async def create_campaign(
    request: Request,
    name: str = Form(...),
    description: str = Form(...),
    template_type: str = Form(...),
    tone: str = Form(...),
    language: str = Form("English"),
    duration_days: int = Form(30),
    posts_per_week: int = Form(3),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    # Server-side allowlist validation (Fix #7)
    if template_type not in TEMPLATES:
        raise HTTPException(status_code=400, detail="Invalid template type")
    if tone not in TONES:
        raise HTTPException(status_code=400, detail="Invalid tone")
    if language not in LANGUAGES:
        raise HTTPException(status_code=400, detail="Invalid language")

    # Clamp numeric inputs
    duration_days = max(1, min(365, duration_days))
    posts_per_week = max(1, min(14, posts_per_week))

    # Free tier: max 1 active unpaid campaign
    active_unpaid = count_active_unpaid_campaigns(session, user.id)
    if active_unpaid >= FREE_CAMPAIGN_LIMIT:
        existing = session.exec(
            select(Campaign).where(Campaign.user_id == user.id)
            .order_by(Campaign.created_at.desc())
        ).all()
        return templates.TemplateResponse(
            "campaign_create.html",
            {
                "request": request,
                "user": user,
                "templates": TEMPLATES,
                "tones": TONES,
                "languages": LANGUAGES,
                "existing_campaigns": existing,
                "free_limit_hit": True,
            },
            status_code=400,
        )

    campaign = Campaign(
        user_id=user.id,
        name=name,
        description=description,
        template_type=template_type,
        tone=tone,
        language=language,
        duration_days=duration_days,
        posts_per_week=posts_per_week,
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    return RedirectResponse(f"/campaigns/{campaign.id}/posts", status_code=302)


@router.get("/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    posts = session.exec(select(Post).where(Post.campaign_id == campaign_id).order_by(Post.scheduled_time)).all()
    mastodon = session.exec(select(MastodonAccount).where(MastodonAccount.user_id == user.id)).first()
    now = datetime.utcnow()
    stale_pending_count = sum(1 for p in posts if p.status == "pending" and p.scheduled_time < now)
    # Overview metrics for all campaigns (publishing is free)
    overview_favs = overview_reblogs = overview_replies = 0
    published_posts = [p for p in posts if p.status == "published"]
    for p in published_posts:
        m = session.exec(select(Metrics).where(Metrics.post_id == p.id)).first()
        if m:
            overview_favs    += m.favourites
            overview_reblogs += m.reblogs
            overview_replies += m.replies
    return templates.TemplateResponse(
        "campaign_detail.html",
        {"request": request, "user": user, "campaign": campaign, "posts": posts, "mastodon": mastodon,
         "ai_max": settings.openai_max_posts_per_campaign, "stale_pending_count": stale_pending_count,
         "overview_favs": overview_favs, "overview_reblogs": overview_reblogs, "overview_replies": overview_replies},
    )


@router.get("/{campaign_id}/edit", response_class=HTMLResponse)
def edit_campaign_page(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        "campaign_create.html",
        {"request": request, "user": user, "templates": TEMPLATES, "tones": TONES, "languages": LANGUAGES,
         "edit_campaign": campaign},
    )


@router.post("/{campaign_id}/edit")
def update_campaign(
    campaign_id: int,
    name: str = Form(...),
    description: str = Form(...),
    template_type: str = Form(...),
    tone: str = Form(...),
    language: str = Form("English"),
    duration_days: int = Form(30),
    posts_per_week: int = Form(3),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    if template_type not in TEMPLATES:
        raise HTTPException(status_code=400, detail="Invalid template type")
    if tone not in TONES:
        raise HTTPException(status_code=400, detail="Invalid tone")
    if language not in LANGUAGES:
        raise HTTPException(status_code=400, detail="Invalid language")
    campaign.name = name.strip()[:200]
    campaign.description = description.strip()
    campaign.template_type = template_type
    campaign.tone = tone
    campaign.language = language
    campaign.duration_days = max(1, min(365, duration_days))
    campaign.posts_per_week = max(1, min(14, posts_per_week))
    session.add(campaign)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)


@router.post("/{campaign_id}/rename")
def rename_campaign(
    campaign_id: int,
    name: str = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    campaign.name = name.strip()[:200]
    session.add(campaign)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)


@router.post("/{campaign_id}/toggle")
def toggle_campaign(
    campaign_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    campaign.enabled = not campaign.enabled
    session.add(campaign)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)


@router.post("/{campaign_id}/reset-start-date")
def reset_start_date(
    campaign_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    if campaign.payment_status != "paid":
        raise HTTPException(status_code=403)
    campaign.start_date = datetime.utcnow()
    session.add(campaign)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)


@router.post("/{campaign_id}/export/newsletter")
async def export_newsletter(
    campaign_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    if campaign.payment_status != "paid":
        raise HTTPException(status_code=403, detail="Campaign not activated")
    if campaign.ai_posts_generated >= settings.openai_max_posts_per_campaign:
        raise HTTPException(status_code=403, detail="AI generation limit reached for this campaign")
    posts = session.exec(select(Post).where(Post.campaign_id == campaign_id).order_by(Post.scheduled_time)).all()
    if not posts:
        raise HTTPException(status_code=400, detail="No posts to export")
    try:
        content = await generate_newsletter_digest(
            campaign.name, campaign.description, campaign.tone, campaign.language,
            [p.content for p in posts],
        )
    except Exception as e:
        logger.error(f"Newsletter generation failed for campaign {campaign_id}: {e}")
        raise HTTPException(status_code=500, detail="Generation failed")
    campaign.ai_posts_generated += 1
    session.add(campaign)
    session.commit()
    return JSONResponse({"content": content})


@router.post("/{campaign_id}/export/linkedin")
async def export_linkedin(
    campaign_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    if campaign.payment_status != "paid":
        raise HTTPException(status_code=403, detail="Campaign not activated")
    if campaign.ai_posts_generated >= settings.openai_max_posts_per_campaign:
        raise HTTPException(status_code=403, detail="AI generation limit reached for this campaign")
    posts = session.exec(select(Post).where(Post.campaign_id == campaign_id).order_by(Post.scheduled_time)).all()
    if not posts:
        raise HTTPException(status_code=400, detail="No posts to export")
    try:
        content = await generate_linkedin_summary(
            campaign.name, campaign.description, campaign.tone, campaign.language,
            [p.content for p in posts],
        )
    except Exception as e:
        logger.error(f"LinkedIn generation failed for campaign {campaign_id}: {e}")
        raise HTTPException(status_code=500, detail="Generation failed")
    campaign.ai_posts_generated += 1
    session.add(campaign)
    session.commit()
    return JSONResponse({"content": content})


@router.post("/{campaign_id}/delete")
def delete_campaign(
    campaign_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    posts = session.exec(select(Post).where(Post.campaign_id == campaign_id)).all()
    for p in posts:
        session.delete(p)
    session.delete(campaign)
    session.commit()
    return RedirectResponse("/dashboard", status_code=302)
