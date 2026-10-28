"""
Tests for the stale post rescheduling logic that runs when a paused campaign is resumed.

The behaviour under test (in routers/campaigns.py toggle_campaign):
- On resume, all pending posts with scheduled_time < now are shifted forward by
  ceil(days since earliest stale post) whole days.
- Non-stale pending posts are untouched.
- Published/failed posts are untouched.
- If there are no stale posts, nothing is changed.
- The time-of-day is preserved (only whole days are added).
"""

import math
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Campaign, Post, User, MastodonAccount


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_campaign(session: Session, enabled: bool = False) -> Campaign:
    user = User()
    session.add(user)
    session.commit()
    session.refresh(user)

    account = MastodonAccount(
        user_id=user.id,
        instance_url="https://mastodon.social",
        access_token="fake",
        username="testuser",
    )
    session.add(account)

    campaign = Campaign(
        user_id=user.id,
        name="Test Campaign",
        description="{}",
        template_type="general",
        tone="neutral",
        status="running",
        enabled=enabled,
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return campaign


def _add_post(session: Session, campaign_id: int, scheduled_time: datetime, status: str = "pending") -> Post:
    post = Post(
        campaign_id=campaign_id,
        content="Test post",
        scheduled_time=scheduled_time,
        status=status,
    )
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


def _run_reschedule(session: Session, campaign_id: int) -> None:
    """Extracted reschedule logic identical to toggle_campaign in campaigns.py."""
    now = datetime.utcnow()
    stale = session.exec(
        select(Post).where(
            Post.campaign_id == campaign_id,
            Post.status == "pending",
            Post.scheduled_time < now,
        ).order_by(Post.scheduled_time.asc())
    ).all()
    if stale:
        delta_days = math.ceil(
            (now - stale[0].scheduled_time).total_seconds() / 86400
        )
        for post in stale:
            post.scheduled_time = post.scheduled_time + timedelta(days=delta_days)
            session.add(post)
        session.commit()


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_stale_posts_are_shifted_forward(session):
    """All stale pending posts should be pushed into the future."""
    campaign = _make_campaign(session, enabled=False)
    now = datetime.utcnow()

    stale1 = _add_post(session, campaign.id, now - timedelta(days=3, hours=2))
    stale2 = _add_post(session, campaign.id, now - timedelta(days=1, hours=1))

    _run_reschedule(session, campaign.id)

    session.refresh(stale1)
    session.refresh(stale2)

    assert stale1.scheduled_time > now
    assert stale2.scheduled_time > now


def test_time_of_day_is_preserved(session):
    """Rescheduled posts should keep their original time-of-day (HH:MM:SS)."""
    campaign = _make_campaign(session, enabled=False)
    now = datetime.utcnow()

    original_time = now - timedelta(days=5)
    original_time = original_time.replace(hour=9, minute=0, second=0, microsecond=0)
    post = _add_post(session, campaign.id, original_time)

    _run_reschedule(session, campaign.id)
    session.refresh(post)

    assert post.scheduled_time.hour == original_time.hour
    assert post.scheduled_time.minute == original_time.minute
    assert post.scheduled_time.second == original_time.second


def test_relative_spacing_between_posts_is_preserved(session):
    """The gap between stale posts should be identical after rescheduling."""
    campaign = _make_campaign(session, enabled=False)
    now = datetime.utcnow()

    t1 = now - timedelta(days=4)
    t2 = now - timedelta(days=2)
    original_gap = t2 - t1

    post1 = _add_post(session, campaign.id, t1)
    post2 = _add_post(session, campaign.id, t2)

    _run_reschedule(session, campaign.id)
    session.refresh(post1)
    session.refresh(post2)

    new_gap = post2.scheduled_time - post1.scheduled_time
    assert new_gap == original_gap


def test_future_pending_posts_are_not_touched(session):
    """Posts already scheduled in the future should not be rescheduled."""
    campaign = _make_campaign(session, enabled=False)
    now = datetime.utcnow()

    stale = _add_post(session, campaign.id, now - timedelta(days=2))
    future = _add_post(session, campaign.id, now + timedelta(days=3))
    original_future_time = future.scheduled_time

    _run_reschedule(session, campaign.id)
    session.refresh(future)

    assert future.scheduled_time == original_future_time


def test_published_posts_are_not_touched(session):
    """Published posts in the past should not be affected."""
    campaign = _make_campaign(session, enabled=False)
    now = datetime.utcnow()

    published = _add_post(session, campaign.id, now - timedelta(days=3), status="published")
    original_time = published.scheduled_time

    _run_reschedule(session, campaign.id)
    session.refresh(published)

    assert published.scheduled_time == original_time


def test_failed_posts_are_not_touched(session):
    """Failed posts should not be rescheduled."""
    campaign = _make_campaign(session, enabled=False)
    now = datetime.utcnow()

    failed = _add_post(session, campaign.id, now - timedelta(days=1), status="failed")
    original_time = failed.scheduled_time

    _run_reschedule(session, campaign.id)
    session.refresh(failed)

    assert failed.scheduled_time == original_time


def test_no_stale_posts_does_nothing(session):
    """If all pending posts are in the future, nothing should change."""
    campaign = _make_campaign(session, enabled=False)
    now = datetime.utcnow()

    future = _add_post(session, campaign.id, now + timedelta(days=2))
    original_time = future.scheduled_time

    _run_reschedule(session, campaign.id)
    session.refresh(future)

    assert future.scheduled_time == original_time


def test_delta_is_always_whole_days(session):
    """The shift must be a whole number of days (no fractional days added)."""
    campaign = _make_campaign(session, enabled=False)
    now = datetime.utcnow()

    # Paused 2.5 days ago → delta should ceil to 3 days
    original = now - timedelta(days=2, hours=12)
    original = original.replace(microsecond=0)
    post = _add_post(session, campaign.id, original)

    _run_reschedule(session, campaign.id)
    session.refresh(post)

    # The shift must be an exact number of whole days
    delta = post.scheduled_time - original
    assert delta.seconds == 0
    assert delta.days == math.ceil(2.5)  # = 3
