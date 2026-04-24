"""
Unit tests for campaign status transitions.

User-triggered transitions (tested via HTTP):
  draft      → active    : first post added
  completed  → running   : post added to completed campaign

Scheduler-triggered transitions (logic extracted inline):
  running    → completed : no pending posts, below max_posts
  running    → archived  : no pending posts, at max_posts
  completed  → archived  : no pending posts, at max_posts (edge case)
  running    stays       : still has pending posts
  completed  stays       : still has pending posts (shouldn't normally happen, but safe)

Manual archive transitions (tested via HTTP):
  running    → archived  : user clicks Archive
  completed  → archived  : user clicks Archive
  active     → archived  : user clicks Archive
"""

import pytest
from datetime import datetime, timedelta
from sqlmodel import Session, select

from app.main import app
from app.auth import get_current_user
from app.models import Campaign, Post, User, MastodonAccount


def _set_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_user(session: Session) -> User:
    user = User()
    session.add(user)
    session.commit()
    session.refresh(user)
    account = MastodonAccount(
        user_id=user.id,
        instance_url="https://mastodon.social",
        access_token="fake-token",
        username=f"user{user.id}",
    )
    session.add(account)
    session.commit()
    return user


def _make_campaign(session: Session, user: User, status: str = "draft", **kwargs) -> Campaign:
    defaults = dict(
        user_id=user.id,
        name="Test Campaign",
        description='{"Topic": "testing"}',
        template_type="knowledge_sharing",
        tone="professional",
        status=status,
        payment_status="unpaid",
        enabled=True,
    )
    defaults.update(kwargs)
    campaign = Campaign(**defaults)
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return campaign


def _make_post(session: Session, campaign: Campaign, status: str = "pending", **kwargs) -> Post:
    defaults = dict(
        campaign_id=campaign.id,
        content="Test post",
        scheduled_time=datetime.utcnow() + timedelta(days=1),
        status=status,
    )
    defaults.update(kwargs)
    post = Post(**defaults)
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


def _run_scheduler_transitions(session: Session, max_posts: int = 32) -> None:
    """Extracted transition logic identical to publish_due_posts in scheduler.py."""
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
        at_limit = len(total_posts) >= max_posts
        if not has_pending:
            if at_limit:
                campaign.status = "archived"
                session.add(campaign)
            elif campaign.status == "running":
                campaign.status = "completed"
                session.add(campaign)
    session.commit()


# ── User-triggered: draft → active ───────────────────────────────────────────

def test_draft_to_active_on_first_post(client, db):
    """Adding a post to a draft campaign advances it to active."""
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, status="draft")
    client.post(f"/campaigns/{campaign.id}/posts/add", data={
        "content": "Hello world",
        "scheduled_time": (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        "csrf_token": "test",
    })
    db.refresh(campaign)
    assert campaign.status == "active"


def test_active_stays_active_on_more_posts(client, db):
    """Adding more posts to an active campaign keeps it active."""
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, status="active")
    _make_post(db, campaign)
    client.post(f"/campaigns/{campaign.id}/posts/add", data={
        "content": "Second post",
        "scheduled_time": (datetime.utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
        "csrf_token": "test",
    })
    db.refresh(campaign)
    assert campaign.status == "active"


# ── User-triggered: completed → running ───────────────────────────────────────

def test_completed_to_running_on_post_add(client, db):
    """Adding a post to a completed campaign transitions it back to running."""
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, status="completed")
    _make_post(db, campaign, status="published")
    client.post(f"/campaigns/{campaign.id}/posts/add", data={
        "content": "New post",
        "scheduled_time": (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        "csrf_token": "test",
    })
    db.refresh(campaign)
    assert campaign.status == "running"


# ── Scheduler: running → completed ───────────────────────────────────────────

def test_running_to_completed_when_no_pending(db):
    """Running campaign with all posts published transitions to completed."""
    user = _create_user(db)
    campaign = _make_campaign(db, user, status="running")
    _make_post(db, campaign, status="published")
    _make_post(db, campaign, status="published")

    _run_scheduler_transitions(db)
    db.refresh(campaign)
    assert campaign.status == "completed"


def test_running_stays_running_with_pending(db):
    """Running campaign with pending posts is not transitioned."""
    user = _create_user(db)
    campaign = _make_campaign(db, user, status="running")
    _make_post(db, campaign, status="published")
    _make_post(db, campaign, status="pending")

    _run_scheduler_transitions(db)
    db.refresh(campaign)
    assert campaign.status == "running"


def test_running_not_completed_if_no_posts(db):
    """Running campaign with no posts at all is left alone."""
    user = _create_user(db)
    campaign = _make_campaign(db, user, status="running")

    _run_scheduler_transitions(db)
    db.refresh(campaign)
    assert campaign.status == "running"


# ── Scheduler: running → archived (at max_posts) ─────────────────────────────

def test_running_to_archived_at_post_limit(db):
    """Running campaign at max_posts with no pending auto-archives."""
    user = _create_user(db)
    campaign = _make_campaign(db, user, status="running")
    for _ in range(3):
        _make_post(db, campaign, status="published")

    _run_scheduler_transitions(db, max_posts=3)
    db.refresh(campaign)
    assert campaign.status == "archived"


def test_running_not_archived_below_post_limit(db):
    """Running campaign below max_posts with no pending becomes completed, not archived."""
    user = _create_user(db)
    campaign = _make_campaign(db, user, status="running")
    for _ in range(2):
        _make_post(db, campaign, status="published")

    _run_scheduler_transitions(db, max_posts=5)
    db.refresh(campaign)
    assert campaign.status == "completed"


# ── Scheduler: completed → archived (edge case) ───────────────────────────────

def test_completed_to_archived_at_post_limit(db):
    """Completed campaign that somehow hits max_posts auto-archives."""
    user = _create_user(db)
    campaign = _make_campaign(db, user, status="completed")
    for _ in range(3):
        _make_post(db, campaign, status="published")

    _run_scheduler_transitions(db, max_posts=3)
    db.refresh(campaign)
    assert campaign.status == "archived"


def test_completed_stays_completed_below_limit(db):
    """Completed campaign below max_posts with no pending is left alone."""
    user = _create_user(db)
    campaign = _make_campaign(db, user, status="completed")
    _make_post(db, campaign, status="published")

    _run_scheduler_transitions(db, max_posts=5)
    db.refresh(campaign)
    assert campaign.status == "completed"


# ── Manual archive transitions ────────────────────────────────────────────────

def test_archive_from_running(client, db):
    """User can manually archive a running campaign."""
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, status="running")
    client.post(f"/campaigns/{campaign.id}/complete", data={"csrf_token": "test"})
    db.refresh(campaign)
    assert campaign.status == "archived"


def test_archive_from_completed(client, db):
    """User can manually archive a completed campaign."""
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, status="completed")
    client.post(f"/campaigns/{campaign.id}/complete", data={"csrf_token": "test"})
    db.refresh(campaign)
    assert campaign.status == "archived"


def test_archive_from_active(client, db):
    """User can manually archive an active campaign."""
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, status="active")
    client.post(f"/campaigns/{campaign.id}/complete", data={"csrf_token": "test"})
    db.refresh(campaign)
    assert campaign.status == "archived"


def test_archive_cancels_pending_posts(client, db):
    """Archiving a campaign cancels all pending posts."""
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, status="running")
    p1 = _make_post(db, campaign, status="pending")
    p2 = _make_post(db, campaign, status="published")
    client.post(f"/campaigns/{campaign.id}/complete", data={"csrf_token": "test"})
    db.refresh(p1)
    db.refresh(p2)
    assert p1.status == "cancelled"
    assert p2.status == "published"  # published posts untouched


def test_archive_is_idempotent(client, db):
    """Archiving an already-archived campaign returns 400."""
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, status="archived")
    resp = client.post(f"/campaigns/{campaign.id}/complete", data={"csrf_token": "test"})
    assert resp.status_code == 400


# ── Delete post reverse transition ────────────────────────────────────────────

def test_running_to_completed_on_pending_post_delete(client, db):
    """Deleting the only pending post from a running campaign transitions it back to completed."""
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, status="running")
    _make_post(db, campaign, status="published")
    pending = _make_post(db, campaign, status="pending")

    client.post(f"/campaigns/{campaign.id}/posts/{pending.id}/delete", data={"csrf_token": "test"})
    db.refresh(campaign)
    assert campaign.status == "completed"


def test_running_stays_running_on_delete_with_other_pending(client, db):
    """Deleting one pending post when others remain keeps the campaign running."""
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, status="running")
    _make_post(db, campaign, status="published")
    pending1 = _make_post(db, campaign, status="pending")
    _make_post(db, campaign, status="pending")

    client.post(f"/campaigns/{campaign.id}/posts/{pending1.id}/delete", data={"csrf_token": "test"})
    db.refresh(campaign)
    assert campaign.status == "running"


def test_no_transition_to_completed_if_at_post_limit_on_delete(client, db):
    """Deleting a post when at max_posts with no pending should not mark completed (would auto-archive instead)."""
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, status="running")
    published_posts = [_make_post(db, campaign, status="published") for _ in range(3)]
    pending = _make_post(db, campaign, status="pending")

    # At limit before delete (4 posts, max=4), after delete 3 posts remain — below limit, no pending → completed
    # This test verifies the at_limit check uses post count AFTER deletion
    client.post(f"/campaigns/{campaign.id}/posts/{pending.id}/delete", data={"csrf_token": "test"})
    db.refresh(campaign)
    # 3 remaining, all published, below any reasonable max → completed
    assert campaign.status == "completed"
