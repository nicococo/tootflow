import asyncio
import json
import logging
from datetime import datetime, timedelta
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, select

from app.config import get_settings
from app.database import engine
from app.models import Campaign, Post, Metrics, MastodonAccount, FollowerSnapshot, XAccount
from app.services import mastodon_service, x_service
from app.crypto import decrypt_token

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


def _resolve_mastodon_account(session: Session, campaign: Campaign) -> MastodonAccount | None:
    """Return the Mastodon account to use for a campaign.

    If mastodon_account_id is set, use that specific account.
    Otherwise fall back to the user's first Mastodon account (legacy behaviour).
    """
    if campaign.mastodon_account_id is not None:
        return session.get(MastodonAccount, campaign.mastodon_account_id)
    return session.exec(
        select(MastodonAccount).where(MastodonAccount.user_id == campaign.user_id)
    ).first()


async def _publish_to_x(session: Session, campaign: Campaign, post: Post) -> None:
    """Publish a single post to X. Mirrors Mastodon error policy:
    4xx → permanent failure; 5xx/429/network → leave pending for retry."""
    account = session.get(XAccount, campaign.x_account_id)
    if not account:
        logger.warning(f"No X account for campaign {campaign.id}, skipping post {post.id}")
        return

    try:
        # Transient copy — decrypted tokens never touch the session (can't be flushed)
        tmp = XAccount(
            user_id=account.user_id,
            username=account.username,
            access_token=decrypt_token(account.access_token),
            access_token_secret=decrypt_token(account.access_token_secret),
        )
        result = await x_service.publish_post(tmp, post.content)
        post.status = "published"
        post.x_status_id = result.get("id")
        post.published_at = datetime.utcnow()
        session.add(post)

        if campaign.status == "active":
            campaign.status = "running"
            session.add(campaign)

        logger.info(f"Published post {post.id} to X for campaign {campaign.id}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code < 500 and e.response.status_code != 429:
            post.status = "failed"
            session.add(post)
            logger.error(f"X post {post.id} permanently failed (HTTP {e.response.status_code}): {e}")
        else:
            logger.warning(f"X post {post.id} transient error (HTTP {e.response.status_code}), will retry: {e}")
    except Exception as e:
        logger.warning(f"X post {post.id} transient error, will retry: {e}")


async def publish_due_posts():
    """Publish posts that are due for active/running campaigns."""
    settings = get_settings()
    now = datetime.utcnow()
    with Session(engine) as session:
        due_posts = session.exec(
            select(Post)
            .join(Campaign)
            .where(
                Post.status == "pending",
                Post.scheduled_time <= now,
                Campaign.status.in_(["active", "running"]),
                Campaign.enabled == True,
            )
            .order_by(Post.scheduled_time.asc())
        ).all()

        for post in due_posts:
            campaign = session.get(Campaign, post.campaign_id)

            if campaign.x_account_id is not None:
                await _publish_to_x(session, campaign, post)
                continue

            account = _resolve_mastodon_account(session, campaign)

            if not account:
                logger.warning(f"No Mastodon account for campaign {campaign.id}, skipping post {post.id}")
                continue

            try:
                # Transient copy — decrypted token never touches the session,
                # so session.commit() can't persist it (Fix #1)
                tmp_account = MastodonAccount(
                    id=account.id,
                    user_id=account.user_id,
                    instance_url=account.instance_url,
                    username=account.username,
                    access_token=decrypt_token(account.access_token),
                )
                _poll_opts = json.loads(post.poll_options) if post.poll_options else None
                result = await mastodon_service.publish_post(
                    tmp_account,
                    post.content,
                    poll_options=_poll_opts,
                    poll_expires_in=post.poll_expires_in,
                )
                post.status = "published"
                post.mastodon_status_id = result["id"]
                post.published_at = datetime.utcnow()
                session.add(post)

                # Mark campaign as running once first post publishes
                if campaign.status == "active":
                    campaign.status = "running"
                    session.add(campaign)

                logger.info(f"Published post {post.id} for campaign {campaign.id}")
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    # 4xx — content/auth problem, won't fix itself
                    post.status = "failed"
                    session.add(post)
                    logger.error(f"Post {post.id} permanently failed (HTTP {e.response.status_code}): {e}")
                else:
                    # 5xx — server-side or rate limit, leave pending for next run
                    logger.warning(f"Post {post.id} transient error (HTTP {e.response.status_code}), will retry: {e}")
            except Exception as e:
                # Network error, timeout, etc. — leave pending for next run
                logger.warning(f"Post {post.id} transient error, will retry: {e}")

        session.commit()

        # Status transitions after publishing
        active_campaigns = session.exec(
            select(Campaign).where(Campaign.status.in_(["running", "completed"]))
        ).all()
        for campaign in active_campaigns:
            total_posts = session.exec(
                select(Post).where(Post.campaign_id == campaign.id)
            ).all()
            if not total_posts:
                continue
            has_pending = any(p.status == "pending" for p in total_posts)
            at_limit = len(total_posts) >= settings.max_posts_per_campaign
            if not has_pending:
                if at_limit:
                    campaign.status = "archived"
                    session.add(campaign)
                    logger.info(f"Campaign {campaign.id} auto-archived (post limit reached, none pending)")
                elif campaign.status == "running":
                    campaign.status = "completed"
                    session.add(campaign)
                    logger.info(f"Campaign {campaign.id} marked completed (no pending posts)")
        session.commit()


async def update_metrics():
    """Fetch engagement metrics for published posts (paid campaigns, recent, capped per user)."""
    settings = get_settings()
    cutoff = datetime.utcnow() - timedelta(days=settings.metrics_max_days)

    with Session(engine) as session:
        published_posts = session.exec(
            select(Post)
            .join(Campaign, Post.campaign_id == Campaign.id)
            .where(
                Post.status == "published",
                Post.mastodon_status_id.isnot(None),
                Post.published_at >= cutoff,
                Campaign.payment_status == "paid",
                Campaign.status.in_(["active", "running", "completed"]),
            )
            .order_by(Post.published_at.desc())
        ).all()

        # Enforce per-user cap
        posts_by_user: dict[int, list] = {}
        for post in published_posts:
            campaign = session.get(Campaign, post.campaign_id)
            uid = campaign.user_id
            posts_by_user.setdefault(uid, [])
            if len(posts_by_user[uid]) < settings.metrics_max_posts_per_user:
                posts_by_user[uid].append(post)
        published_posts = [p for posts in posts_by_user.values() for p in posts]

        # Cache decrypted tokens per user to avoid double-decrypting
        decrypted: dict[int, str] = {}

        for post in published_posts:
            campaign = session.get(Campaign, post.campaign_id)
            account = _resolve_mastodon_account(session, campaign)
            if not account:
                continue

            user_id = account.user_id
            if user_id not in decrypted:
                try:
                    decrypted[user_id] = decrypt_token(account.access_token)
                except Exception as e:
                    logger.error(f"Failed to decrypt token for user {user_id}: {e}")
                    continue

            try:
                tmp = MastodonAccount(
                    instance_url=account.instance_url,
                    access_token=decrypted[user_id],
                    user_id=user_id,
                )
                data = await mastodon_service.fetch_status_metrics(tmp, post.mastodon_status_id)
                metrics = session.exec(select(Metrics).where(Metrics.post_id == post.id)).first()
                if metrics:
                    metrics.favourites = data["favourites"]
                    metrics.reblogs = data["reblogs"]
                    metrics.replies = data["replies"]
                    metrics.poll_voters = data["poll_voters"]
                    metrics.last_updated = datetime.utcnow()
                else:
                    metrics = Metrics(
                        post_id=post.id,
                        favourites=data["favourites"],
                        reblogs=data["reblogs"],
                        replies=data["replies"],
                        poll_voters=data["poll_voters"],
                    )
                session.add(metrics)
            except Exception as e:
                logger.error(f"Failed to update metrics for post {post.id}: {e}")

        session.commit()

        # Follower snapshots — one per user per day
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        all_accounts = session.exec(select(MastodonAccount)).all()
        for account in all_accounts:
            user_id = account.user_id
            already = session.exec(
                select(FollowerSnapshot)
                .where(FollowerSnapshot.user_id == user_id)
                .where(FollowerSnapshot.recorded_at >= today_start)
                .where(FollowerSnapshot.recorded_at < today_end)
            ).first()

            if user_id not in decrypted:
                try:
                    decrypted[user_id] = decrypt_token(account.access_token)
                except Exception as e:
                    logger.error(f"Failed to decrypt token for follower snapshot, user {user_id}: {e}")
                    continue

            try:
                info = await mastodon_service.get_account_info(
                    account.instance_url, decrypted[user_id]
                )
                count = info.get("followers_count", 0)
                if already:
                    already.count = count
                    session.add(already)
                else:
                    session.add(FollowerSnapshot(user_id=user_id, count=count))
                logger.info(f"Follower snapshot user {user_id}: {count}")
            except Exception as e:
                logger.error(f"Failed to fetch followers for user {user_id}: {e}")

        session.commit()


def start_scheduler():
    scheduler.add_job(publish_due_posts, "interval", minutes=1, id="publish_posts", max_instances=1)
    scheduler.add_job(update_metrics, "interval", hours=24, id="update_metrics")
    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler():
    scheduler.shutdown()
