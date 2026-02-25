import asyncio
import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, select

from app.database import engine
from app.models import Campaign, Post, Metrics, MastodonAccount
from app.services import mastodon_service
from app.crypto import decrypt_token

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def publish_due_posts():
    """Publish posts that are due for active/running campaigns."""
    now = datetime.utcnow()
    with Session(engine) as session:
        due_posts = session.exec(
            select(Post)
            .join(Campaign)
            .where(
                Post.status == "pending",
                Post.scheduled_time <= now,
                Campaign.status.in_(["active", "running"]),
            )
        ).all()

        for post in due_posts:
            campaign = session.get(Campaign, post.campaign_id)
            account = session.exec(
                select(MastodonAccount).where(MastodonAccount.user_id == campaign.user_id)
            ).first()

            if not account:
                logger.warning(f"No Mastodon account for campaign {campaign.id}, skipping post {post.id}")
                continue

            try:
                # Decrypt token for use — do not persist the decrypted value (Fix #1)
                account.access_token = decrypt_token(account.access_token)
                result = await mastodon_service.publish_post(account, post.content)
                post.status = "published"
                post.mastodon_status_id = result["id"]
                post.published_at = datetime.utcnow()
                session.add(post)

                # Mark campaign as running once first post publishes
                if campaign.status == "active":
                    campaign.status = "running"
                    session.add(campaign)

                logger.info(f"Published post {post.id} for campaign {campaign.id}")
            except Exception as e:
                post.status = "failed"
                session.add(post)
                logger.error(f"Failed to publish post {post.id}: {e}")

        session.commit()

        # Mark completed campaigns
        running = session.exec(
            select(Campaign).where(Campaign.status == "running")
        ).all()
        for campaign in running:
            if campaign.end_date and now >= campaign.end_date:
                pending = session.exec(
                    select(Post).where(
                        Post.campaign_id == campaign.id,
                        Post.status == "pending",
                    )
                ).all()
                if not pending:
                    campaign.status = "completed"
                    session.add(campaign)
        session.commit()


async def update_metrics():
    """Fetch updated engagement metrics for all published posts."""
    with Session(engine) as session:
        published_posts = session.exec(
            select(Post).where(
                Post.status == "published",
                Post.mastodon_status_id.isnot(None),
            )
        ).all()

        for post in published_posts:
            campaign = session.get(Campaign, post.campaign_id)
            account = session.exec(
                select(MastodonAccount).where(MastodonAccount.user_id == campaign.user_id)
            ).first()
            if not account:
                continue

            try:
                account.access_token = decrypt_token(account.access_token)
                data = await mastodon_service.fetch_status_metrics(account, post.mastodon_status_id)
                metrics = session.exec(select(Metrics).where(Metrics.post_id == post.id)).first()
                if metrics:
                    metrics.favourites = data["favourites"]
                    metrics.reblogs = data["reblogs"]
                    metrics.replies = data["replies"]
                    metrics.last_updated = datetime.utcnow()
                else:
                    metrics = Metrics(
                        post_id=post.id,
                        favourites=data["favourites"],
                        reblogs=data["reblogs"],
                        replies=data["replies"],
                    )
                session.add(metrics)
            except Exception as e:
                logger.error(f"Failed to update metrics for post {post.id}: {e}")

        session.commit()


def start_scheduler():
    scheduler.add_job(publish_due_posts, "interval", minutes=1, id="publish_posts")
    scheduler.add_job(update_metrics, "interval", hours=24, id="update_metrics")
    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler():
    scheduler.shutdown()
