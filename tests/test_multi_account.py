"""
Tests for multi-account and platform architecture.

Covers:
- Campaign.mastodon_account_id field (nullable FK)
- XAccount stub model exists
- Scheduler resolves account via mastodon_account_id when set
- Scheduler falls back to user's first account when mastodon_account_id is None
- Migration backfill logic
- Campaign creation with explicit account assignment
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from sqlmodel import Session, select

from app.models import Campaign, MastodonAccount, Post, User, XAccount
from app.main import app
from app.auth import get_current_user


def _set_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _create_user(session: Session) -> User:
    user = User()
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _create_mastodon_account(session: Session, user: User, username: str = "testuser") -> MastodonAccount:
    account = MastodonAccount(
        user_id=user.id,
        instance_url="https://mastodon.social",
        access_token="fake-token",
        username=username,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _make_campaign(session: Session, user: User, mastodon_account_id=None, **kwargs) -> Campaign:
    defaults = dict(
        user_id=user.id,
        name="Test Campaign",
        description='{"Topic": "testing"}',
        template_type="knowledge_sharing",
        tone="professional",
        status="active",
        payment_status="unpaid",
        enabled=True,
        mastodon_account_id=mastodon_account_id,
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
        scheduled_time=datetime.utcnow() - timedelta(minutes=1),
        status="pending",
    )
    defaults.update(kwargs)
    post = Post(**defaults)
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


# ── Model: Campaign.mastodon_account_id ──────────────────────────────────────

class TestCampaignAccountIdField:
    def test_campaign_can_be_created_with_mastodon_account_id(self, db):
        user = _create_user(db)
        account = _create_mastodon_account(db, user)
        campaign = _make_campaign(db, user, mastodon_account_id=account.id)
        db.refresh(campaign)
        assert campaign.mastodon_account_id == account.id

    def test_campaign_mastodon_account_id_is_nullable(self, db):
        user = _create_user(db)
        _create_mastodon_account(db, user)
        campaign = _make_campaign(db, user, mastodon_account_id=None)
        db.refresh(campaign)
        assert campaign.mastodon_account_id is None

    def test_campaign_without_account_id_still_valid(self, db):
        """Existing campaigns without mastodon_account_id continue to work."""
        user = _create_user(db)
        _create_mastodon_account(db, user)
        campaign = _make_campaign(db, user)
        assert campaign.id is not None
        assert campaign.mastodon_account_id is None

    def test_two_campaigns_different_accounts_same_user(self, db):
        """A user can have campaigns assigned to different Mastodon accounts."""
        user = _create_user(db)
        account1 = _create_mastodon_account(db, user, username="personal")
        account2 = _create_mastodon_account(db, user, username="project")
        c1 = _make_campaign(db, user, mastodon_account_id=account1.id, name="Personal")
        c2 = _make_campaign(db, user, mastodon_account_id=account2.id, name="Project")
        assert c1.mastodon_account_id == account1.id
        assert c2.mastodon_account_id == account2.id
        assert c1.mastodon_account_id != c2.mastodon_account_id


# ── Model: XAccount stub ─────────────────────────────────────────────────────

class TestXAccountModel:
    def test_xaccount_model_exists(self):
        """XAccount model is importable and has expected fields."""
        assert XAccount is not None

    def test_xaccount_can_be_created(self, db):
        user = _create_user(db)
        account = XAccount(
            user_id=user.id,
            username="testuser",
            access_token="fake-x-token",
            access_token_secret="fake-secret",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        assert account.id is not None
        assert account.user_id == user.id
        assert account.username == "testuser"

    def test_campaign_can_reference_x_account(self, db):
        user = _create_user(db)
        x_account = XAccount(
            user_id=user.id,
            username="testuser",
            access_token="fake-x-token",
            access_token_secret="fake-secret",
        )
        db.add(x_account)
        db.commit()
        db.refresh(x_account)
        campaign = _make_campaign(db, user, x_account_id=x_account.id)
        db.refresh(campaign)
        assert campaign.x_account_id == x_account.id


# ── Scheduler: account resolution ────────────────────────────────────────────

class TestSchedulerAccountResolution:
    """
    Tests for the _resolve_mastodon_account helper extracted from publish_due_posts.
    Verifies correct account lookup with and without mastodon_account_id.
    """

    def test_resolves_account_by_mastodon_account_id_when_set(self, db):
        """When campaign.mastodon_account_id is set, use that specific account."""
        from app.scheduler import _resolve_mastodon_account
        user = _create_user(db)
        account1 = _create_mastodon_account(db, user, username="account1")
        account2 = _create_mastodon_account(db, user, username="account2")
        campaign = _make_campaign(db, user, mastodon_account_id=account2.id)

        resolved = _resolve_mastodon_account(db, campaign)
        assert resolved is not None
        assert resolved.id == account2.id
        assert resolved.username == "account2"

    def test_falls_back_to_user_account_when_account_id_is_none(self, db):
        """When campaign.mastodon_account_id is None, fall back to user's first account."""
        from app.scheduler import _resolve_mastodon_account
        user = _create_user(db)
        account = _create_mastodon_account(db, user, username="fallback")
        campaign = _make_campaign(db, user, mastodon_account_id=None)

        resolved = _resolve_mastodon_account(db, campaign)
        assert resolved is not None
        assert resolved.id == account.id

    def test_returns_none_when_no_account_exists(self, db):
        """When no Mastodon account exists for user, return None."""
        from app.scheduler import _resolve_mastodon_account
        user = _create_user(db)
        campaign = _make_campaign(db, user, mastodon_account_id=None)

        resolved = _resolve_mastodon_account(db, campaign)
        assert resolved is None

    def test_returns_none_when_account_id_points_to_missing_account(self, db):
        """When mastodon_account_id references a deleted/missing account, return None."""
        from app.scheduler import _resolve_mastodon_account
        user = _create_user(db)
        campaign = _make_campaign(db, user, mastodon_account_id=99999)

        resolved = _resolve_mastodon_account(db, campaign)
        assert resolved is None


# ── Migration backfill logic ──────────────────────────────────────────────────

class TestMigrationBackfill:
    def test_backfill_sets_account_id_for_existing_campaigns(self, db):
        """Migration backfill assigns mastodon_account_id to campaigns that have none."""
        from app.migrations.add_account_ids import backfill_mastodon_account_ids
        user = _create_user(db)
        account = _create_mastodon_account(db, user)
        campaign = _make_campaign(db, user, mastodon_account_id=None)
        assert campaign.mastodon_account_id is None

        backfill_mastodon_account_ids(db)
        db.refresh(campaign)
        assert campaign.mastodon_account_id == account.id

    def test_backfill_does_not_overwrite_existing_account_id(self, db):
        """Backfill is idempotent — campaigns already assigned keep their account."""
        from app.migrations.add_account_ids import backfill_mastodon_account_ids
        user = _create_user(db)
        account1 = _create_mastodon_account(db, user, username="first")
        account2 = _create_mastodon_account(db, user, username="second")
        campaign = _make_campaign(db, user, mastodon_account_id=account2.id)

        backfill_mastodon_account_ids(db)
        db.refresh(campaign)
        # Should still point to account2, not overwritten with account1
        assert campaign.mastodon_account_id == account2.id

    def test_backfill_skips_users_with_no_mastodon_account(self, db):
        """Backfill does not crash or corrupt campaigns for users with no Mastodon account."""
        from app.migrations.add_account_ids import backfill_mastodon_account_ids
        user = _create_user(db)
        # No MastodonAccount created
        campaign = _make_campaign(db, user, mastodon_account_id=None)

        backfill_mastodon_account_ids(db)
        db.refresh(campaign)
        assert campaign.mastodon_account_id is None  # unchanged


# ── HTTP: Campaign creation with account assignment ───────────────────────────

class TestCampaignCreationWithAccount:
    def test_create_campaign_with_single_account_auto_assigns(self, client, db):
        """When user has one Mastodon account, new campaigns are auto-assigned to it."""
        user = _create_user(db)
        account = _create_mastodon_account(db, user)
        _set_user(user)

        import json
        client.post("/campaigns/new", data={
            "name": "Auto Assigned",
            "description": json.dumps({"Topic": "Test"}),
            "template_type": "knowledge_sharing",
            "tone": "professional",
            "language": "English",
            "duration_days": "30",
            "posts_per_week": "3",
            "csrf_token": "test",
        })

        campaign = db.exec(
            select(Campaign).where(Campaign.user_id == user.id, Campaign.name == "Auto Assigned")
        ).first()
        assert campaign is not None
        assert campaign.mastodon_account_id == account.id
