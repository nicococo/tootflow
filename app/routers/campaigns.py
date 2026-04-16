import json
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import get_settings
from app.database import get_session
from app.models import Campaign, Metrics, Post, User, MastodonAccount
from app.auth import get_current_user
from app.csrf import verify_csrf
from app.limiter import limiter
from app.services.ai_service import generate_export, refine_export

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/campaigns")
settings = get_settings()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["settings"] = settings


def format_description_for_ai(description: str) -> str:
    """Return description as 'Label: value\\n...' for AI prompts. Handles both JSON and legacy plain text."""
    if not description:
        return ""
    try:
        data = json.loads(description)
        if isinstance(data, dict):
            return "\n".join(f"{k}: {v}" for k, v in data.items() if v)
    except (json.JSONDecodeError, ValueError):
        pass
    return description

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

TEMPLATE_FIELDS = {
    "product_launch": [
        {"label": "Product name",              "hint": "e.g. MyApp v2.0"},
        {"label": "One-line description",      "hint": "What it does in one sentence"},
        {"label": "Problem it solves",         "hint": "What pain does it remove?",             "multiline": True},
        {"label": "Target audience",           "hint": "Be specific — who exactly benefits?"},
        {"label": "Key features or benefits",  "hint": "List the main ones",                    "multiline": True},
        {"label": "What makes it different",   "hint": "Compared to existing alternatives"},
        {"label": "Launch date",               "hint": "e.g. April 1st or 'available now'"},
    ],
    "open_source_release": [
        {"label": "Project name",                    "hint": "e.g. fastparse"},
        {"label": "What it does",                    "hint": "One clear sentence"},
        {"label": "Problem it solves",               "hint": "Why does this need to exist?",         "multiline": True},
        {"label": "Who should use it",               "hint": "Target users or use cases"},
        {"label": "Tech stack",                      "hint": "e.g. Python, Rust, TypeScript"},
        {"label": "What makes it interesting",       "hint": "The hook — what surprises people",     "multiline": True},
        {"label": "Repository URL",                  "hint": "e.g. github.com/you/project"},
    ],
    "event_promotion": [
        {"label": "Event name",                      "hint": "e.g. PyConf 2026"},
        {"label": "Date and location",               "hint": "e.g. June 12, Berlin / online"},
        {"label": "What it's about",                 "hint": "Topic and format",                     "multiline": True},
        {"label": "Who should attend",               "hint": "Target audience"},
        {"label": "What attendees will get",         "hint": "Key takeaways or value",               "multiline": True},
        {"label": "How to register",                 "hint": "URL or instructions"},
    ],
    "knowledge_sharing": [
        {"label": "Topic",                           "hint": "e.g. distributed systems, burnout, vim"},
        {"label": "The core insight or lesson",      "hint": "The thing you wish you'd known earlier", "multiline": True},
        {"label": "Why it matters",                  "hint": "Stakes or consequences"},
        {"label": "Who benefits",                    "hint": "Target audience"},
        {"label": "Your experience or context",      "hint": "Why you're the one sharing this",      "multiline": True},
    ],
    "developer_tips": [
        {"label": "Topic or technology",             "hint": "e.g. Docker, async Python, Git"},
        {"label": "Tips to share",                   "hint": "List them, one per line",              "multiline": True},
        {"label": "Target skill level",              "hint": "Beginner / intermediate / advanced"},
        {"label": "Why these tips are valuable",     "hint": "What problem do they solve?"},
        {"label": "Real-world context",              "hint": "When does this actually come up?",     "multiline": True},
    ],
    "research_insights": [
        {"label": "Research topic",                  "hint": "What you studied or investigated"},
        {"label": "Key finding",                     "hint": "The headline result",                  "multiline": True},
        {"label": "Why it's surprising or important","hint": "The 'so what'"},
        {"label": "Who should care",                 "hint": "Who this is relevant for"},
        {"label": "Methodology in brief",            "hint": "How you arrived at this finding"},
    ],
    "personal_brand": [
        {"label": "Your name and role",              "hint": "e.g. Sara, senior ML engineer"},
        {"label": "Area of expertise",               "hint": "Your main domain or skill"},
        {"label": "Who you help and how",            "hint": "Be specific about the value you deliver", "multiline": True},
        {"label": "What makes you unique",           "hint": "Your angle or contrarian take"},
        {"label": "Your mission or vision",          "hint": "What you're working toward"},
    ],
    "community_building": [
        {"label": "Community or project name",       "hint": "e.g. Rust Berlin, OpenData Collective"},
        {"label": "What it's about",                 "hint": "Mission and focus",                    "multiline": True},
        {"label": "Who should join",                 "hint": "Ideal members"},
        {"label": "What members get or do",          "hint": "Value and activities",                 "multiline": True},
        {"label": "How to join",                     "hint": "Link or first step"},
    ],
    "project_showcase": [
        {"label": "Project name",                    "hint": "e.g. HomeMetrics"},
        {"label": "What problem it solves",          "hint": "The need that drove you to build it"},
        {"label": "How it works",                    "hint": "Brief technical overview",             "multiline": True},
        {"label": "Tech stack",                      "hint": "Languages, frameworks, tools"},
        {"label": "Biggest challenge overcome",      "hint": "What was hard?",                       "multiline": True},
        {"label": "What you're most proud of",       "hint": "The part you'd show off first"},
    ],
    "newsletter_growth": [
        {"label": "Newsletter name",                 "hint": "e.g. The Pragmatic Engineer"},
        {"label": "Topic or niche",                  "hint": "What you cover"},
        {"label": "Who it's for",                    "hint": "Ideal reader profile"},
        {"label": "What subscribers get",            "hint": "Format and value per issue",           "multiline": True},
        {"label": "Publishing frequency",            "hint": "e.g. weekly, every other Tuesday"},
        {"label": "Why subscribe",                   "hint": "Unique value vs. other newsletters"},
    ],
}


@router.get("", response_class=HTMLResponse)
def list_campaigns(request: Request, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    campaigns = session.exec(select(Campaign).where(Campaign.user_id == user.id).order_by(Campaign.created_at.desc())).all()
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "campaigns": campaigns})


@router.get("/new", response_class=HTMLResponse)
def new_campaign_page(request: Request, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    existing = session.exec(select(Campaign).where(Campaign.user_id == user.id).order_by(Campaign.created_at.desc())).all()
    return templates.TemplateResponse(
        "campaign_create.html",
        {
            "request": request,
            "user": user,
            "templates": TEMPLATES,
            "tones": TONES,
            "languages": LANGUAGES,
            "existing_campaigns": existing,
            "template_fields": TEMPLATE_FIELDS,
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
    # Parse description into labeled fields
    description_fields = []
    if campaign.description:
        try:
            data = json.loads(campaign.description)
            if isinstance(data, dict):
                description_fields = [{"label": k, "value": v} for k, v in data.items() if v]
        except (json.JSONDecodeError, ValueError):
            # Legacy plain text: wrap as single field
            description_fields = [{"label": "Description", "value": campaign.description}]
    return templates.TemplateResponse(
        "campaign_detail.html",
        {"request": request, "user": user, "campaign": campaign, "posts": posts, "mastodon": mastodon,
         "ai_max": settings.openai_max_posts_per_campaign, "stale_pending_count": stale_pending_count,
         "overview_favs": overview_favs, "overview_reblogs": overview_reblogs, "overview_replies": overview_replies,
         "description_fields": description_fields},
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
         "template_fields": TEMPLATE_FIELDS, "edit_campaign": campaign},
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


@router.get("/{campaign_id}/export", response_class=HTMLResponse)
def export_page(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    if campaign.payment_status != "paid":
        raise HTTPException(status_code=403, detail="Campaign not activated")
    posts = session.exec(select(Post).where(Post.campaign_id == campaign_id).order_by(Post.scheduled_time)).all()
    export_remaining = max(0, settings.export_max_per_campaign - campaign.export_generations)
    try:
        export_drafts = json.loads(campaign.export_drafts or "{}")
    except (json.JSONDecodeError, ValueError):
        export_drafts = {}
    return templates.TemplateResponse("export.html", {
        "request": request, "user": user,
        "campaign": campaign, "posts": posts,
        "export_remaining": export_remaining,
        "export_max": settings.export_max_per_campaign,
        "export_drafts": export_drafts,
    })


def _save_export_draft(campaign: Campaign, target: str, content: str, session: Session) -> None:
    """Persist content into the campaign's export_drafts JSON blob for the given target."""
    try:
        drafts = json.loads(campaign.export_drafts or "{}")
    except (json.JSONDecodeError, ValueError):
        drafts = {}
    drafts[target] = content
    campaign.export_drafts = json.dumps(drafts)
    session.add(campaign)
    session.commit()


def _resolve_post_contents(all_posts, selected_posts: str) -> list[str]:
    """Return post contents filtered by selected_posts CSV of IDs (empty = all)."""
    if selected_posts.strip():
        ids = {int(x) for x in selected_posts.split(",") if x.strip().isdigit()}
        return [p.content for p in all_posts if p.id in ids]
    return [p.content for p in all_posts]


@router.post("/{campaign_id}/export/generate")
async def export_generate(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
    target: str = Form("newsletter"),
    length: str = Form("medium"),
    tone_shift: str = Form("keep"),
    include_cta: bool = Form(False),
    include_emoji: bool = Form(False),
    selected_posts: str = Form(""),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    if campaign.payment_status != "paid":
        raise HTTPException(status_code=403, detail="Campaign not activated")
    if campaign.export_generations >= settings.export_max_per_campaign:
        raise HTTPException(status_code=403, detail="Export generation limit reached for this campaign")

    all_posts = session.exec(select(Post).where(Post.campaign_id == campaign_id).order_by(Post.scheduled_time)).all()
    if not all_posts:
        raise HTTPException(status_code=400, detail="No posts to export")

    contents = _resolve_post_contents(all_posts, selected_posts)
    if not contents:
        raise HTTPException(status_code=400, detail="No posts selected")

    try:
        content = await generate_export(
            campaign_name=campaign.name,
            description=format_description_for_ai(campaign.description),
            tone=campaign.tone,
            language=campaign.language,
            post_contents=contents,
            target=target,
            length=length,
            tone_shift=tone_shift,
            include_cta=include_cta,
            include_emoji=include_emoji,
        )
    except Exception as e:
        logger.error(f"Export generation failed for campaign {campaign_id}: {e}")
        raise HTTPException(status_code=500, detail="Generation failed")

    campaign.export_generations += 1
    _save_export_draft(campaign, target, content, session)
    return JSONResponse({
        "content": content,
        "remaining": max(0, settings.export_max_per_campaign - campaign.export_generations),
    })


@router.post("/{campaign_id}/export/refine")
async def export_refine(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
    current_content: str = Form(...),
    instructions: str = Form(...),
    target: str = Form("newsletter"),
    length: str = Form("medium"),
    tone_shift: str = Form("keep"),
    include_cta: bool = Form(False),
    include_emoji: bool = Form(False),
    selected_posts: str = Form(""),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    if campaign.payment_status != "paid":
        raise HTTPException(status_code=403, detail="Campaign not activated")
    if campaign.export_generations >= settings.export_max_per_campaign:
        raise HTTPException(status_code=403, detail="Export generation limit reached for this campaign")

    all_posts = session.exec(select(Post).where(Post.campaign_id == campaign_id).order_by(Post.scheduled_time)).all()
    contents = _resolve_post_contents(all_posts, selected_posts)

    try:
        content = await refine_export(
            current_content=current_content,
            instructions=instructions,
            campaign_name=campaign.name,
            description=format_description_for_ai(campaign.description),
            tone=campaign.tone,
            language=campaign.language,
            post_contents=contents,
            target=target,
            length=length,
            tone_shift=tone_shift,
            include_cta=include_cta,
            include_emoji=include_emoji,
        )
    except Exception as e:
        logger.error(f"Export refinement failed for campaign {campaign_id}: {e}")
        raise HTTPException(status_code=500, detail="Refinement failed")

    campaign.export_generations += 1
    _save_export_draft(campaign, target, content, session)
    return JSONResponse({
        "content": content,
        "remaining": max(0, settings.export_max_per_campaign - campaign.export_generations),
    })


@router.post("/{campaign_id}/export/save-draft")
async def export_save_draft(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
    target: str = Form(...),
    content: str = Form(...),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    if campaign.payment_status != "paid":
        raise HTTPException(status_code=403, detail="Campaign not activated")
    _save_export_draft(campaign, target, content, session)
    return JSONResponse({"ok": True})


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
