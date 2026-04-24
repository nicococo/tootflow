"""
Unit tests for campaign CRUD operations.

Tests run against an in-memory SQLite database. Auth and CSRF dependencies
are overridden. The TestClient is module-scoped so the app lifespan
(scheduler) starts and stops only once per test run.

Coverage:
- format_description_for_ai() helper
- Campaign detail page (200, 404 missing, 404 wrong user)
- Create campaign (redirect, DB persistence)
- Delete campaign + cascade (posts, metrics, payments)
- Toggle pause/resume + stale post rescheduling
- Ownership enforcement across all mutating endpoints
- Edit campaign
"""

import json
import pytest
from datetime import datetime, timedelta
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool
from fastapi.testclient import TestClient

from app.main import app
from app.database import get_session
from app.auth import get_current_user
from app.csrf import verify_csrf
from app.models import Campaign, Metrics, MastodonAccount, Payment, Post, User
from app.routers.campaigns import format_description_for_ai


# ── DB + app fixtures (module-scoped: lifespan starts once) ───────────────────

@pytest.fixture(name="engine", scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(name="client", scope="module")
def client_fixture(engine):
    """Single TestClient for the whole module — avoids re-starting the scheduler."""
    def override_session():
        with Session(engine) as session:
            yield session

    # auth override is set per-test via _set_user(); start with a sentinel
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[verify_csrf] = lambda: None

    with TestClient(app, follow_redirects=False) as client:
        yield client

    app.dependency_overrides.clear()


def _set_user(user: User):
    """Swap the auth override to return a specific user."""
    app.dependency_overrides[get_current_user] = lambda: user


# ── Per-test session + user helpers ───────────────────────────────────────────

@pytest.fixture(name="db")
def db_fixture(engine):
    """Fresh session for each test; rolls back after."""
    with Session(engine) as session:
        yield session


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


def _make_campaign(session: Session, user: User, **kwargs) -> Campaign:
    defaults = dict(
        user_id=user.id,
        name="Test Campaign",
        description='{"Topic": "testing"}',
        template_type="knowledge_sharing",
        tone="professional",
        status="active",
        payment_status="unpaid",
        enabled=True,
    )
    defaults.update(kwargs)
    campaign = Campaign(**defaults)
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return campaign


def _make_post(session: Session, campaign: Campaign, **kwargs) -> Post:
    defaults = dict(
        campaign_id=campaign.id,
        content="Test post",
        scheduled_time=datetime.utcnow() + timedelta(days=1),
        status="pending",
    )
    defaults.update(kwargs)
    post = Post(**defaults)
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


# ── format_description_for_ai (pure logic, no HTTP) ──────────────────────────

def test_format_description_json():
    desc = json.dumps({"Topic": "AI", "Audience": "developers"})
    result = format_description_for_ai(desc)
    assert "Topic: AI" in result
    assert "Audience: developers" in result


def test_format_description_skips_empty_values():
    desc = json.dumps({"Topic": "AI", "Empty": ""})
    result = format_description_for_ai(desc)
    assert "Empty" not in result


def test_format_description_legacy_plain_text():
    assert format_description_for_ai("plain text") == "plain text"


def test_format_description_empty():
    assert format_description_for_ai("") == ""


# ── Campaign detail ────────────────────────────────────────────────────────────

def test_campaign_detail_200(client, db):
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user)
    resp = client.get(f"/campaigns/{campaign.id}")
    assert resp.status_code == 200


def test_campaign_detail_404_missing(client, db):
    user = _create_user(db)
    _set_user(user)
    resp = client.get("/campaigns/99999")
    assert resp.status_code == 404


def test_campaign_detail_404_other_user(client, db):
    owner = _create_user(db)
    visitor = _create_user(db)
    campaign = _make_campaign(db, owner)
    _set_user(visitor)
    resp = client.get(f"/campaigns/{campaign.id}")
    assert resp.status_code == 404


# ── Create campaign ────────────────────────────────────────────────────────────

def test_create_campaign_redirects(client, db):
    user = _create_user(db)
    _set_user(user)
    resp = client.post("/campaigns/new", data={
        "name": "New Campaign",
        "description": json.dumps({"Topic": "Test"}),
        "template_type": "knowledge_sharing",
        "tone": "professional",
        "language": "English",
        "duration_days": "30",
        "posts_per_week": "3",
        "csrf_token": "test",
    })
    assert resp.status_code == 302
    assert "/campaigns/" in resp.headers["location"]


def test_create_campaign_persists(client, db):
    user = _create_user(db)
    _set_user(user)
    client.post("/campaigns/new", data={
        "name": "Persisted",
        "description": json.dumps({"Topic": "Test"}),
        "template_type": "knowledge_sharing",
        "tone": "professional",
        "language": "English",
        "duration_days": "30",
        "posts_per_week": "3",
        "csrf_token": "test",
    })
    campaigns = db.exec(
        select(Campaign).where(Campaign.user_id == user.id, Campaign.name == "Persisted")
    ).all()
    assert len(campaigns) == 1
    assert campaigns[0].status == "draft"
    assert campaigns[0].payment_status == "unpaid"


# ── Delete campaign ────────────────────────────────────────────────────────────

def test_delete_campaign_redirects(client, db):
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user)
    resp = client.post(f"/campaigns/{campaign.id}/delete", data={"csrf_token": "test"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard"


def test_delete_campaign_removes_from_db(client, db):
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user)
    cid = campaign.id
    client.post(f"/campaigns/{cid}/delete", data={"csrf_token": "test"})
    db.expire_all()
    assert db.get(Campaign, cid) is None


def test_delete_cascades_posts(client, db):
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user)
    post = _make_post(db, campaign)
    pid = post.id
    client.post(f"/campaigns/{campaign.id}/delete", data={"csrf_token": "test"})
    db.expire_all()
    assert db.get(Post, pid) is None


def test_delete_cascades_metrics(client, db):
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user)
    post = _make_post(db, campaign)
    metrics = Metrics(post_id=post.id, favourites=5, reblogs=2, replies=1)
    db.add(metrics)
    db.commit()
    mid = metrics.id
    client.post(f"/campaigns/{campaign.id}/delete", data={"csrf_token": "test"})
    db.expire_all()
    assert db.get(Metrics, mid) is None


def test_delete_cascades_payment(client, db):
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, payment_status="paid")
    payment = Payment(
        user_id=user.id, campaign_id=campaign.id,
        lemonsqueezy_order_id="ord_1", amount=8.0, currency="EUR", status="paid",
    )
    db.add(payment)
    db.commit()
    pay_id = payment.id
    client.post(f"/campaigns/{campaign.id}/delete", data={"csrf_token": "test"})
    db.expire_all()
    assert db.get(Payment, pay_id) is None


def test_delete_404_other_user(client, db):
    owner = _create_user(db)
    attacker = _create_user(db)
    campaign = _make_campaign(db, owner)
    _set_user(attacker)
    resp = client.post(f"/campaigns/{campaign.id}/delete", data={"csrf_token": "test"})
    assert resp.status_code == 404
    db.expire_all()
    assert db.get(Campaign, campaign.id) is not None


# ── Toggle pause / resume ──────────────────────────────────────────────────────

def test_toggle_pauses_campaign(client, db):
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, enabled=True)
    client.post(f"/campaigns/{campaign.id}/toggle", data={"csrf_token": "test"})
    db.refresh(campaign)
    assert campaign.enabled is False


def test_toggle_resumes_campaign(client, db):
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, enabled=False)
    client.post(f"/campaigns/{campaign.id}/toggle", data={"csrf_token": "test"})
    db.refresh(campaign)
    assert campaign.enabled is True


def test_toggle_reschedules_stale_posts_on_resume(client, db):
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, enabled=False)
    stale = _make_post(db, campaign, scheduled_time=datetime.utcnow() - timedelta(days=3))
    client.post(f"/campaigns/{campaign.id}/toggle", data={"csrf_token": "test"})
    db.refresh(stale)
    assert stale.scheduled_time > datetime.utcnow()


def test_toggle_does_not_move_future_posts(client, db):
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, enabled=False)
    _make_post(db, campaign, scheduled_time=datetime.utcnow() - timedelta(days=2))
    future_time = datetime.utcnow() + timedelta(days=5)
    future = _make_post(db, campaign, scheduled_time=future_time)
    client.post(f"/campaigns/{campaign.id}/toggle", data={"csrf_token": "test"})
    db.refresh(future)
    assert future.scheduled_time == future_time


def test_toggle_404_other_user(client, db):
    owner = _create_user(db)
    attacker = _create_user(db)
    campaign = _make_campaign(db, owner, enabled=True)
    _set_user(attacker)
    resp = client.post(f"/campaigns/{campaign.id}/toggle", data={"csrf_token": "test"})
    assert resp.status_code == 404
    db.refresh(campaign)
    assert campaign.enabled is True


# ── Edit campaign ──────────────────────────────────────────────────────────────

def test_edit_campaign_updates_name(client, db):
    user = _create_user(db)
    _set_user(user)
    campaign = _make_campaign(db, user, name="Old Name")
    client.post(f"/campaigns/{campaign.id}/edit", data={
        "name": "New Name",
        "description": campaign.description,
        "template_type": campaign.template_type,
        "tone": campaign.tone,
        "language": "English",
        "duration_days": "30",
        "posts_per_week": "3",
        "csrf_token": "test",
    })
    db.refresh(campaign)
    assert campaign.name == "New Name"


def test_edit_campaign_404_other_user(client, db):
    owner = _create_user(db)
    attacker = _create_user(db)
    campaign = _make_campaign(db, owner, name="Original")
    _set_user(attacker)
    resp = client.post(f"/campaigns/{campaign.id}/edit", data={
        "name": "Hacked",
        "description": "{}",
        "template_type": "knowledge_sharing",
        "tone": "professional",
        "language": "English",
        "duration_days": "30",
        "posts_per_week": "3",
        "csrf_token": "test",
    })
    assert resp.status_code == 404
    db.refresh(campaign)
    assert campaign.name == "Original"
