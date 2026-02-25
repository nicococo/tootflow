from datetime import timedelta

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import get_session
from app.models import Campaign, Post, Metrics, User, MastodonAccount
from app.auth import get_current_user

router = APIRouter(prefix="/campaigns")
templates = Jinja2Templates(directory="app/templates")


@router.get("/{campaign_id}/metrics", response_class=HTMLResponse)
def campaign_metrics(
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

    post_metrics = []
    for post in posts:
        m = session.exec(select(Metrics).where(Metrics.post_id == post.id)).first()
        post_metrics.append({"post": post, "metrics": m})

    total_favs    = sum((pm["metrics"].favourites if pm["metrics"] else 0) for pm in post_metrics)
    total_reblogs = sum((pm["metrics"].reblogs    if pm["metrics"] else 0) for pm in post_metrics)
    total_replies = sum((pm["metrics"].replies    if pm["metrics"] else 0) for pm in post_metrics)

    # Build weekly engagement chart
    chart = _build_weekly_chart(campaign, post_metrics)

    return templates.TemplateResponse(
        "metrics.html",
        {
            "request": request,
            "user": user,
            "campaign": campaign,
            "post_metrics": post_metrics,
            "total_favs": total_favs,
            "total_reblogs": total_reblogs,
            "total_replies": total_replies,
            "chart": chart,
        },
    )


def _build_weekly_chart(campaign: Campaign, post_metrics: list) -> list:
    """Return weekly engagement buckets for the chart.
    Always returns at least one bucket using campaign dates as the timeline."""
    from datetime import datetime as dt

    # Use all posts for the timeline; metrics default to 0 if not yet collected
    all_posts = [
        (
            pm["post"],
            pm["metrics"].favourites if pm["metrics"] else 0,
            pm["metrics"].reblogs    if pm["metrics"] else 0,
            pm["metrics"].replies    if pm["metrics"] else 0,
        )
        for pm in post_metrics
    ]

    # Determine timeline reference points
    ref = (
        campaign.start_date
        or (min(p.scheduled_time for p, *_ in all_posts) if all_posts else dt.utcnow())
    )
    max_date = (
        campaign.end_date
        or (max(p.scheduled_time for p, *_ in all_posts) if all_posts else None)
        or ref + timedelta(days=campaign.duration_days)
    )
    total_weeks = max(1, int((max_date - ref).days / 7) + 1)

    buckets = []
    for w in range(total_weeks):
        week_start = ref + timedelta(weeks=w)
        week_end   = week_start + timedelta(weeks=1)
        favs    = sum(f for p, f, rb, rp in all_posts if week_start <= p.scheduled_time < week_end)
        reblogs = sum(rb for p, f, rb, rp in all_posts if week_start <= p.scheduled_time < week_end)
        replies = sum(rp for p, f, rb, rp in all_posts if week_start <= p.scheduled_time < week_end)
        buckets.append({"label": f"W{w + 1}", "favs": favs, "reblogs": reblogs, "replies": replies})

    max_total = max((b["favs"] + b["reblogs"] + b["replies"]) for b in buckets) or 1
    for b in buckets:
        total = b["favs"] + b["reblogs"] + b["replies"]
        b["bar_pct"]    = round(total / max_total * 100)
        b["fav_pct"]    = round(b["favs"]    / total * 100) if total else 0
        b["reblog_pct"] = round(b["reblogs"] / total * 100) if total else 0
        b["reply_pct"]  = 100 - b["fav_pct"] - b["reblog_pct"]

    return buckets
