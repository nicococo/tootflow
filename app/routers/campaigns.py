import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import get_settings
from app.database import get_session
from app.models import Campaign, Post, User, MastodonAccount
from app.auth import get_current_user
from app.csrf import verify_csrf
from app.limiter import limiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/campaigns")
settings = get_settings()
templates = Jinja2Templates(directory="app/templates")

TEMPLATES = ["product_launch", "open_source_release", "event_promotion"]
TONES = ["professional", "casual", "technical", "inspiring"]
LANGUAGES = ["English", "German", "French"]


@router.get("", response_class=HTMLResponse)
def list_campaigns(request: Request, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    campaigns = session.exec(select(Campaign).where(Campaign.user_id == user.id).order_by(Campaign.created_at.desc())).all()
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "campaigns": campaigns})


@router.get("/new", response_class=HTMLResponse)
def new_campaign_page(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(
        "campaign_create.html",
        {"request": request, "user": user, "templates": TEMPLATES, "tones": TONES, "languages": LANGUAGES},
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
    return templates.TemplateResponse(
        "campaign_detail.html",
        {"request": request, "user": user, "campaign": campaign, "posts": posts, "mastodon": mastodon,
         "ai_max": settings.openai_max_posts_per_campaign},
    )


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
