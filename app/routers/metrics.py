import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import get_settings
from app.crypto import decrypt_token
from app.database import get_session
from app.limiter import limiter
from app.models import Campaign, Post, Metrics, User, MastodonAccount, FollowerSnapshot
from app.auth import get_current_user
from app.services import mastodon_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaigns")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["settings"] = get_settings()


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

    total_favs    = sum((pm["metrics"].favourites  if pm["metrics"] else 0) for pm in post_metrics)
    total_reblogs = sum((pm["metrics"].reblogs     if pm["metrics"] else 0) for pm in post_metrics)
    total_replies = sum((pm["metrics"].replies     if pm["metrics"] else 0) for pm in post_metrics)
    total_voters  = sum((pm["metrics"].poll_voters if pm["metrics"] else 0) for pm in post_metrics
                        if pm["post"].poll_options)

    # Follower snapshots for this user
    snapshots = session.exec(
        select(FollowerSnapshot)
        .where(FollowerSnapshot.user_id == user.id)
        .order_by(FollowerSnapshot.recorded_at)
    ).all()

    # Build weekly engagement chart
    chart = _build_weekly_chart(campaign, post_metrics, snapshots)

    follower_current = snapshots[-1].count if snapshots else None
    follower_start   = None
    follower_delta   = None
    if snapshots and campaign.start_date:
        # First snapshot on or after campaign start
        after_start = [s for s in snapshots if s.recorded_at >= campaign.start_date]
        if after_start:
            follower_start = after_start[0].count
            follower_delta = follower_current - follower_start
    elif len(snapshots) >= 2:
        follower_start = snapshots[0].count
        follower_delta = follower_current - follower_start

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
            "total_voters": total_voters,
            "chart": chart,
            "snapshots": snapshots,
            "follower_current": follower_current,
            "follower_delta": follower_delta,
        },
    )


def _build_weekly_chart(campaign: Campaign, post_metrics: list, snapshots: list = None) -> list:
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

        # Follower delta for this week
        fdelta = None
        if snapshots:
            week_snaps = [s for s in snapshots if week_start <= s.recorded_at < week_end]
            if len(week_snaps) >= 2:
                fdelta = week_snaps[-1].count - week_snaps[0].count
            elif week_snaps:
                prev = [s for s in snapshots if s.recorded_at < week_start]
                if prev:
                    fdelta = week_snaps[-1].count - prev[-1].count

        buckets.append({"label": f"W{w + 1}", "favs": favs, "reblogs": reblogs,
                        "replies": replies, "follower_delta": fdelta})

    max_total = max((b["favs"] + b["reblogs"] + b["replies"]) for b in buckets) or 1
    for b in buckets:
        total = b["favs"] + b["reblogs"] + b["replies"]
        b["bar_pct"]    = round(total / max_total * 100)
        b["fav_pct"]    = round(b["favs"]    / total * 100) if total else 0
        b["reblog_pct"] = round(b["reblogs"] / total * 100) if total else 0
        b["reply_pct"]  = 100 - b["fav_pct"] - b["reblog_pct"]

    # Scale follower delta bars
    max_fdelta = max((abs(b["follower_delta"]) for b in buckets
                      if b["follower_delta"] is not None), default=0) or 1
    for b in buckets:
        fd = b["follower_delta"]
        b["follower_pct"] = round(abs(fd) / max_fdelta * 100) if fd is not None else 0

    return buckets


# Separate router for post-level reply fetching
post_router = APIRouter(prefix="/posts")


@post_router.get("/{post_id}/replies", response_class=HTMLResponse)
@limiter.limit("30/hour")
async def post_replies(
    post_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404)

    campaign = session.get(Campaign, post.campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)

    if not post.mastodon_status_id:
        return HTMLResponse('<div class="reply-empty">No published status found for this post.</div>')

    account = session.exec(
        select(MastodonAccount).where(MastodonAccount.user_id == user.id)
    ).first()
    if not account:
        return HTMLResponse('<div class="reply-empty">No Mastodon account connected.</div>')

    account.access_token = decrypt_token(account.access_token)

    try:
        replies = await mastodon_service.fetch_replies(account, post.mastodon_status_id)
    except Exception as e:
        logger.error("fetch_replies failed for post %s: %s", post_id, e)
        return HTMLResponse('<div class="reply-empty">Could not load replies right now. Try again later.</div>')

    return templates.TemplateResponse(
        "replies_fragment.html",
        {"request": request, "replies": replies},
    )
