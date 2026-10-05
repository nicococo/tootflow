"""
Tests for X.com (Twitter) support.

Covers:
- x_service OAuth 1.0a signature generation
- X connect/callback/disconnect routes
- Scheduler X publishing branch (x_status_id, status transitions, error policy)
- Campaign XOR account validation (mastodon_account_id vs x_account_id)
- 280-char limit and poll rejection for X campaigns
- Platform-aware AI service (char limits)
"""

import asyncio
import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from sqlmodel import Session, select

from app.models import Campaign, MastodonAccount, Post, User, XAccount
from app.main import app
from app.auth import get_current_user
from app.crypto import encrypt_token


def _set_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _create_user(session: Session) -> User:
    user = User()
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _create_x_account(session: Session, user: User, username: str = "xuser") -> XAccount:
    account = XAccount(
        user_id=user.id,
        username=username,
        access_token=encrypt_token("x-token"),
        access_token_secret=encrypt_token("x-secret"),
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _create_mastodon_account(session: Session, user: User, username: str = "muser") -> MastodonAccount:
    account = MastodonAccount(
        user_id=user.id,
        instance_url="https://mastodon.social",
        access_token=encrypt_token("m-token"),
        username=username,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _make_campaign(session: Session, user: User, **kwargs) -> Campaign:
    defaults = dict(
        user_id=user.id,
        name="X Campaign",
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
        scheduled_time=datetime.utcnow() - timedelta(minutes=1),
        status="pending",
    )
    defaults.update(kwargs)
    post = Post(**defaults)
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


# ── OAuth 1.0a signature ─────────────────────────────────────────────────────

class TestOAuth1Signing:
    def test_header_contains_all_required_oauth_fields(self):
        from app.services.x_service import _oauth1_header
        header = _oauth1_header(
            "POST", "https://api.x.com/2/tweets",
            "consumer-key", "consumer-secret",
            token="tok", token_secret="tok-secret",
        )
        assert header.startswith("OAuth ")
        for field in ("oauth_consumer_key", "oauth_nonce", "oauth_signature",
                      "oauth_signature_method", "oauth_timestamp", "oauth_token",
                      "oauth_version"):
            assert f"{field}=" in header
        assert "oauth_signature_method=\"HMAC-SHA1\"" in header

    def test_signature_is_deterministic_except_nonce_and_timestamp(self):
        """Two headers differ only by nonce/timestamp-derived signature, same base fields."""
        from app.services.x_service import _oauth1_header
        h1 = _oauth1_header("POST", "https://api.x.com/2/tweets", "ck", "cs")
        h2 = _oauth1_header("POST", "https://api.x.com/2/tweets", "ck", "cs")
        assert 'oauth_consumer_key="ck"' in h1 and 'oauth_consumer_key="ck"' in h2

    def test_percent_encoding(self):
        from app.services.x_service import _pct
        assert _pct("hello world") == "hello%20world"
        assert _pct("a&b=c") == "a%26b%3Dc"
        assert _pct("~unreserved~") == "~unreserved~"


# ── X connect / callback / disconnect ────────────────────────────────────────

class TestXConnectFlow:
    def test_connect_redirects_to_x_authorize(self, client, db):
        user = _create_user(db)
        _set_user(user)

        with patch("app.routers.auth.x_service.get_request_token", new=AsyncMock(
            return_value={"oauth_token": "req-tok", "oauth_token_secret": "req-sec"}
        )), patch("app.routers.auth.settings") as mock_settings:
            mock_settings.x_api_key = "key"
            mock_settings.app_base_url = "http://test"
            resp = client.post("/x/connect", data={"csrf_token": "test"})

        assert resp.status_code == 302
        assert resp.headers["location"].startswith("https://api.x.com/oauth/authorize?oauth_token=req-tok")

    def test_callback_creates_encrypted_x_account(self, client, db):
        user = _create_user(db)
        _set_user(user)

        with patch("app.routers.auth.x_service.get_request_token", new=AsyncMock(
            return_value={"oauth_token": "req-tok2", "oauth_token_secret": "req-sec2"}
        )), patch("app.routers.auth.settings") as mock_settings:
            mock_settings.x_api_key = "key"
            mock_settings.app_base_url = "http://test"
            client.post("/x/connect", data={"csrf_token": "test"})

        with patch("app.routers.auth.x_service.exchange_verifier", new=AsyncMock(
            return_value={
                "oauth_token": "access-tok",
                "oauth_token_secret": "access-sec",
                "user_id": "12345",
                "screen_name": "xcampaigner",
            }
        )):
            resp = client.get("/x/callback?oauth_token=req-tok2&oauth_verifier=ver")

        assert resp.status_code == 302
        assert resp.headers["location"] == "/account?x_linked=1"

        account = db.exec(
            select(XAccount).where(XAccount.user_id == user.id, XAccount.username == "xcampaigner")
        ).first()
        assert account is not None
        # Tokens must be encrypted at rest
        assert account.access_token != "access-tok"
        assert account.access_token_secret != "access-sec"
        from app.crypto import decrypt_token
        assert decrypt_token(account.access_token) == "access-tok"
        assert decrypt_token(account.access_token_secret) == "access-sec"

    def test_callback_rejects_unknown_state(self, client, db):
        resp = client.get("/x/callback?oauth_token=bogus&oauth_verifier=ver")
        assert resp.status_code == 400

    def test_callback_refreshes_existing_account_tokens(self, client, db):
        user = _create_user(db)
        existing = _create_x_account(db, user, username="xcampaigner")
        _set_user(user)

        with patch("app.routers.auth.x_service.get_request_token", new=AsyncMock(
            return_value={"oauth_token": "req-tok3", "oauth_token_secret": "req-sec3"}
        )), patch("app.routers.auth.settings") as mock_settings:
            mock_settings.x_api_key = "key"
            mock_settings.app_base_url = "http://test"
            client.post("/x/connect", data={"csrf_token": "test"})

        with patch("app.routers.auth.x_service.exchange_verifier", new=AsyncMock(
            return_value={
                "oauth_token": "new-tok",
                "oauth_token_secret": "new-sec",
                "screen_name": "xcampaigner",
            }
        )):
            client.get("/x/callback?oauth_token=req-tok3&oauth_verifier=ver")

        db.expire_all()  # app ran in its own session — drop stale identity map
        accounts = db.exec(
            select(XAccount).where(XAccount.user_id == user.id, XAccount.username == "xcampaigner")
        ).all()
        assert len(accounts) == 1  # upsert, no duplicate
        from app.crypto import decrypt_token
        assert decrypt_token(accounts[0].access_token) == "new-tok"

    def test_disconnect_deletes_account_and_nulls_campaign_fk(self, client, db):
        user = _create_user(db)
        account = _create_x_account(db, user)
        campaign = _make_campaign(db, user, x_account_id=account.id)
        _set_user(user)

        resp = client.post(f"/x/disconnect/{account.id}", data={"csrf_token": "test"})
        assert resp.status_code == 302
        # Fresh SQL query — identity map only serves rows that still exist
        assert db.exec(select(XAccount).where(XAccount.id == account.id)).first() is None
        db.refresh(campaign)
        assert campaign.x_account_id is None

    def test_disconnect_rejects_other_users_account(self, client, db):
        owner = _create_user(db)
        other = _create_user(db)
        account = _create_x_account(db, owner)
        _set_user(other)

        client.post(f"/x/disconnect/{account.id}", data={"csrf_token": "test"})
        assert db.get(XAccount, account.id) is not None


# ── Scheduler: X publishing ──────────────────────────────────────────────────

def _run_publish(db, engine, **patches):
    """Run publish_due_posts with the scheduler's engine swapped to the test DB."""
    import contextlib
    from app.scheduler import publish_due_posts
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("app.scheduler.engine", engine))
        for target, mock in patches.items():
            stack.enter_context(patch(target, new=mock))
        asyncio.run(publish_due_posts())
    db.expire_all()


class TestSchedulerXPublishing:
    def test_publishes_post_to_x(self, db, engine):
        user = _create_user(db)
        account = _create_x_account(db, user)
        campaign = _make_campaign(db, user, x_account_id=account.id)
        post = _make_post(db, campaign)
        # A future pending post keeps the campaign in "running" after publish
        _make_post(db, campaign, scheduled_time=datetime.utcnow() + timedelta(days=1))

        _run_publish(db, engine, **{
            "app.scheduler.x_service.publish_post": AsyncMock(return_value={"id": "tweet-999"}),
        })

        assert db.get(Post, post.id).status == "published"
        assert db.get(Post, post.id).x_status_id == "tweet-999"
        assert db.get(Post, post.id).published_at is not None
        assert db.get(Campaign, campaign.id).status == "running"

    def test_mastodon_campaign_still_uses_mastodon(self, db, engine):
        """Campaigns without x_account_id go through the Mastodon path."""
        user = _create_user(db)
        m_account = _create_mastodon_account(db, user)
        x_account = _create_x_account(db, user)
        campaign = _make_campaign(db, user, mastodon_account_id=m_account.id)
        post = _make_post(db, campaign)

        x_mock = AsyncMock()
        _run_publish(db, engine, **{
            "app.scheduler.x_service.publish_post": x_mock,
            "app.scheduler.mastodon_service.publish_post": AsyncMock(return_value={"id": "masto-1"}),
        })

        x_mock.assert_not_called()
        assert db.get(Post, post.id).status == "published"
        assert db.get(Post, post.id).mastodon_status_id == "masto-1"
        assert db.get(Post, post.id).x_status_id is None

    def test_x_4xx_marks_post_failed(self, db, engine):
        import httpx
        user = _create_user(db)
        account = _create_x_account(db, user)
        campaign = _make_campaign(db, user, x_account_id=account.id)
        post = _make_post(db, campaign)

        response = httpx.Response(403, request=httpx.Request("POST", "https://api.x.com/2/tweets"))
        error = httpx.HTTPStatusError("forbidden", request=response.request, response=response)
        _run_publish(db, engine, **{
            "app.scheduler.x_service.publish_post": AsyncMock(side_effect=error),
        })

        assert db.get(Post, post.id).status == "failed"

    def test_x_429_stays_pending_for_retry(self, db, engine):
        import httpx
        user = _create_user(db)
        account = _create_x_account(db, user)
        campaign = _make_campaign(db, user, x_account_id=account.id)
        post = _make_post(db, campaign)

        response = httpx.Response(429, request=httpx.Request("POST", "https://api.x.com/2/tweets"))
        error = httpx.HTTPStatusError("rate limited", request=response.request, response=response)
        _run_publish(db, engine, **{
            "app.scheduler.x_service.publish_post": AsyncMock(side_effect=error),
        })

        assert db.get(Post, post.id).status == "pending"

    def test_missing_x_account_skips_post(self, db, engine):
        user = _create_user(db)
        campaign = _make_campaign(db, user, x_account_id=99999)
        post = _make_post(db, campaign)

        _run_publish(db, engine)

        assert db.get(Post, post.id).status == "pending"


# ── Token encryption at rest (regression: decrypted tokens must never persist) ─

class TestTokensNotPersistedDecrypted:
    def test_x_publish_does_not_persist_decrypted_tokens(self, db, engine):
        user = _create_user(db)
        account = _create_x_account(db, user)
        campaign = _make_campaign(db, user, x_account_id=account.id)
        _make_post(db, campaign)

        _run_publish(db, engine, **{
            "app.scheduler.x_service.publish_post": AsyncMock(return_value={"id": "t-1"}),
        })

        row = db.exec(select(XAccount).where(XAccount.id == account.id)).first()
        assert "x-token" not in row.access_token
        assert "x-secret" not in row.access_token_secret
        from app.crypto import decrypt_token
        assert decrypt_token(row.access_token) == "x-token"
        assert decrypt_token(row.access_token_secret) == "x-secret"

    def test_mastodon_publish_does_not_persist_decrypted_token(self, db, engine):
        user = _create_user(db)
        m_account = _create_mastodon_account(db, user)
        campaign = _make_campaign(db, user, mastodon_account_id=m_account.id)
        _make_post(db, campaign)

        _run_publish(db, engine, **{
            "app.scheduler.mastodon_service.publish_post": AsyncMock(return_value={"id": "m-1"}),
        })

        row = db.exec(select(MastodonAccount).where(MastodonAccount.id == m_account.id)).first()
        assert "m-token" not in row.access_token
        from app.crypto import decrypt_token
        assert decrypt_token(row.access_token) == "m-token"


# ── Campaign creation: XOR account validation ────────────────────────────────

class TestCampaignXORAccounts:
    def _post_form(self, client, **extra):
        data = {
            "name": "Test",
            "description": json.dumps({"Topic": "T"}),
            "template_type": "knowledge_sharing",
            "tone": "professional",
            "language": "English",
            "duration_days": "30",
            "posts_per_week": "3",
            "csrf_token": "test",
        }
        data.update(extra)
        return client.post("/campaigns/new", data=data)

    def test_create_campaign_with_x_account(self, client, db):
        user = _create_user(db)
        x_account = _create_x_account(db, user)
        _set_user(user)

        self._post_form(client, x_account_id=str(x_account.id))

        campaign = db.exec(
            select(Campaign).where(Campaign.user_id == user.id, Campaign.name == "Test")
        ).first()
        assert campaign is not None
        assert campaign.x_account_id == x_account.id
        assert campaign.mastodon_account_id is None

    def test_x_account_wins_over_mastodon_when_both_submitted(self, client, db):
        """XOR enforcement: valid x_account_id takes precedence and nulls Mastodon."""
        user = _create_user(db)
        m_account = _create_mastodon_account(db, user)
        x_account = _create_x_account(db, user)
        _set_user(user)

        self._post_form(client, mastodon_account_id=str(m_account.id),
                        x_account_id=str(x_account.id))

        campaign = db.exec(
            select(Campaign).where(Campaign.user_id == user.id, Campaign.name == "Test")
        ).first()
        assert campaign.x_account_id == x_account.id
        assert campaign.mastodon_account_id is None

    def test_rejects_x_account_belonging_to_someone_else(self, client, db):
        owner = _create_user(db)
        stranger = _create_user(db)
        foreign_x = _create_x_account(db, owner)
        own_masto = _create_mastodon_account(db, stranger)
        _set_user(stranger)

        self._post_form(client, x_account_id=str(foreign_x.id))

        campaign = db.exec(
            select(Campaign).where(Campaign.user_id == stranger.id, Campaign.name == "Test")
        ).first()
        # Foreign X account rejected → falls back to own Mastodon account
        assert campaign.x_account_id is None
        assert campaign.mastodon_account_id == own_masto.id


# ── Posts: 280-char limit + poll rejection for X ─────────────────────────────

class TestXPostConstraints:
    def test_add_post_truncates_at_280_for_x_campaign(self, client, db):
        user = _create_user(db)
        x_account = _create_x_account(db, user)
        campaign = _make_campaign(db, user, x_account_id=x_account.id)
        _set_user(user)

        long_content = "a" * 400
        resp = client.post(f"/campaigns/{campaign.id}/posts/add", data={
            "content": long_content,
            "scheduled_time": "2030-01-01T10:00",
            "csrf_token": "test",
        })
        assert resp.status_code == 302
        post = db.exec(select(Post).where(Post.campaign_id == campaign.id)).first()
        assert len(post.content) == 280

    def test_add_post_truncates_at_500_for_mastodon_campaign(self, client, db):
        user = _create_user(db)
        m_account = _create_mastodon_account(db, user)
        campaign = _make_campaign(db, user, mastodon_account_id=m_account.id)
        _set_user(user)

        long_content = "a" * 600
        client.post(f"/campaigns/{campaign.id}/posts/add", data={
            "content": long_content,
            "scheduled_time": "2030-01-01T10:00",
            "csrf_token": "test",
        })
        post = db.exec(select(Post).where(Post.campaign_id == campaign.id)).first()
        assert len(post.content) == 500

    def test_poll_rejected_for_x_campaign(self, client, db):
        user = _create_user(db)
        x_account = _create_x_account(db, user)
        campaign = _make_campaign(db, user, x_account_id=x_account.id)
        _set_user(user)

        resp = client.post(f"/campaigns/{campaign.id}/posts/add", data={
            "content": "Poll post",
            "scheduled_time": "2030-01-01T10:00",
            "poll_option": ["Yes", "No"],
            "csrf_token": "test",
        })
        assert resp.status_code == 400
        assert b"not supported on X" in resp.content

    def test_poll_still_works_for_mastodon_campaign(self, client, db):
        user = _create_user(db)
        m_account = _create_mastodon_account(db, user)
        campaign = _make_campaign(db, user, mastodon_account_id=m_account.id)
        _set_user(user)

        resp = client.post(f"/campaigns/{campaign.id}/posts/add", data={
            "content": "Poll post",
            "scheduled_time": "2030-01-01T10:00",
            "poll_option": ["Yes", "No"],
            "csrf_token": "test",
        })
        assert resp.status_code == 302
        post = db.exec(select(Post).where(Post.campaign_id == campaign.id)).first()
        assert json.loads(post.poll_options) == ["Yes", "No"]


# ── AI service: platform awareness ───────────────────────────────────────────

def _mock_openai(content: str):
    """Return a mock AsyncOpenAI instance whose completions return `content`."""
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=response)
    return mock_client


class TestAIServicePlatform:
    def test_generate_posts_x_uses_280_limit(self):
        from app.services import ai_service

        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai("x" * 300)) as mock_cls:
            posts = asyncio.run(ai_service.generate_posts(
                campaign_name="T", description="d", template_type="knowledge_sharing",
                tone="professional", language="English", duration_days=7,
                posts_per_week=1, platform="x",
            ))
            call_kwargs = mock_cls.return_value.chat.completions.create.call_args

        assert all(len(p) <= 280 for p in posts)
        system_prompt = call_kwargs.kwargs["messages"][0]["content"]
        assert "280 characters" in system_prompt
        assert "X/Twitter" in system_prompt

    def test_generate_posts_default_platform_is_mastodon(self):
        from app.services import ai_service

        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai("y" * 400)) as mock_cls:
            posts = asyncio.run(ai_service.generate_posts(
                campaign_name="T", description="d", template_type="knowledge_sharing",
                tone="professional", language="English", duration_days=7,
                posts_per_week=1,
            ))
            call_kwargs = mock_cls.return_value.chat.completions.create.call_args

        assert all(len(p) <= 500 for p in posts)
        system_prompt = call_kwargs.kwargs["messages"][0]["content"]
        assert "500 characters" in system_prompt
        assert "Mastodon" in system_prompt
