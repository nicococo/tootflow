"""
Tests for bring-your-own-model (BYOK) AI configuration.

Covers:
- Client resolution precedence: user config → env fallback → none
- Encryption of stored API keys
- Save/test/remove routes
- Generation endpoints reject gracefully when no provider is configured
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlmodel import Session, select

from app.crypto import encrypt_token, decrypt_token
from app.models import Campaign, Post, User, UserAIConfig
from app.main import app
from app.auth import get_current_user
from app.services import ai_service


def _set_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _create_user(session: Session) -> User:
    user = User()
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _make_campaign(session: Session, user: User, **kwargs) -> Campaign:
    defaults = dict(
        user_id=user.id,
        name="C",
        description='{"Topic": "t"}',
        template_type="knowledge_sharing",
        tone="professional",
        status="active",
        enabled=True,
    )
    defaults.update(kwargs)
    campaign = Campaign(**defaults)
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return campaign


def _make_post(session: Session, campaign: Campaign, **kwargs) -> Post:
    from datetime import datetime, timedelta
    post = Post(
        campaign_id=campaign.id,
        content="Test",
        scheduled_time=datetime.utcnow() - timedelta(minutes=1),
        status="pending",
        **kwargs,
    )
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


def _mock_openai(content: str = "Generated"):
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=response)
    return mock_client


# ── Client resolution ────────────────────────────────────────────────────────

class TestBuildAIClient:
    def test_user_config_wins_over_env(self, db):
        from app.services.ai_service import build_ai_client
        cfg = UserAIConfig(
            user_id=1,
            api_key=encrypt_token("user-key"),
            base_url="https://openrouter.ai/api/v1",
            model="openai/gpt-4o-mini",
        )
        with patch("app.services.ai_service.settings") as mock_settings:
            mock_settings.openai_api_key = "env-key"
            client, model = build_ai_client(cfg)
        assert client is not None
        assert client.api_key == "user-key"  # decrypted
        assert str(client.base_url).rstrip("/") == "https://openrouter.ai/api/v1"
        assert model == "openai/gpt-4o-mini"

    def test_env_fallback_when_no_user_config(self, db):
        from app.services.ai_service import build_ai_client
        with patch("app.services.ai_service.settings") as mock_settings:
            mock_settings.openai_api_key = "env-key"
            client, model = build_ai_client(None)
        assert client is not None
        assert client.api_key == "env-key"
        assert model is None  # env model is applied later

    def test_none_when_nothing_configured(self, db):
        from app.services.ai_service import build_ai_client
        with patch("app.services.ai_service.settings") as mock_settings:
            mock_settings.openai_api_key = ""
            client, model = build_ai_client(None)
        assert client is None
        assert model is None

    def test_stored_key_is_encrypted_at_rest(self, db):
        user = _create_user(db)
        db.add(UserAIConfig(user_id=user.id, api_key=encrypt_token("plain-key")))
        db.commit()
        row = db.exec(select(UserAIConfig).where(UserAIConfig.user_id == user.id)).first()
        assert row.api_key != "plain-key"
        assert decrypt_token(row.api_key) == "plain-key"


# ── Save / test / remove routes ──────────────────────────────────────────────

class TestAIAccountRoutes:
    def test_save_creates_encrypted_config(self, client, db):
        user = _create_user(db)
        _set_user(user)
        resp = client.post("/account/ai/save", data={
            "api_key": "sk-user-key",
            "base_url": "https://openrouter.ai/api/v1/",
            "model": "openai/gpt-4o-mini",
            "csrf_token": "test",
        })
        assert resp.status_code == 302
        assert "ai_saved=1" in resp.headers["location"]
        cfg = db.exec(select(UserAIConfig).where(UserAIConfig.user_id == user.id)).first()
        assert cfg is not None
        assert cfg.api_key != "sk-user-key"
        assert decrypt_token(cfg.api_key) == "sk-user-key"
        assert cfg.base_url == "https://openrouter.ai/api/v1"  # trailing slash stripped
        assert cfg.model == "openai/gpt-4o-mini"

    def test_save_keeps_existing_key_when_blank(self, client, db):
        user = _create_user(db)
        db.add(UserAIConfig(user_id=user.id, api_key=encrypt_token("old-key"), model="m1"))
        db.commit()
        _set_user(user)
        client.post("/account/ai/save", data={
            "api_key": "", "base_url": "", "model": "m2", "csrf_token": "test",
        })
        cfg = db.exec(select(UserAIConfig).where(UserAIConfig.user_id == user.id)).first()
        assert decrypt_token(cfg.api_key) == "old-key"
        assert cfg.model == "m2"

    def test_save_rejects_non_http_base_url(self, client, db):
        user = _create_user(db)
        _set_user(user)
        resp = client.post("/account/ai/save", data={
            "api_key": "k", "base_url": "ftp://evil", "model": "", "csrf_token": "test",
        })
        assert "ai_error=" in resp.headers["location"]

    def test_remove_deletes_config(self, client, db):
        user = _create_user(db)
        db.add(UserAIConfig(user_id=user.id, api_key=encrypt_token("k")))
        db.commit()
        _set_user(user)
        resp = client.post("/account/ai/remove", data={"csrf_token": "test"})
        assert resp.status_code == 302
        assert "ai_removed=1" in resp.headers["location"]
        assert db.exec(select(UserAIConfig).where(UserAIConfig.user_id == user.id)).first() is None

    def test_test_route_reports_missing_key(self, client, db):
        user = _create_user(db)
        _set_user(user)
        with patch.object(ai_service.settings, "openai_api_key", ""):
            resp = client.post("/account/ai/test", data={"csrf_token": "test"})
        assert resp.json() == {"ok": False, "error": "No API key configured."}

    def test_test_route_success(self, client, db):
        user = _create_user(db)
        db.add(UserAIConfig(user_id=user.id, api_key=encrypt_token("k")))
        db.commit()
        _set_user(user)
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(return_value=[])
        with patch("app.routers.account.build_ai_client", return_value=(mock_client, None)):
            resp = client.post("/account/ai/test", data={"csrf_token": "test"})
        assert resp.json() == {"ok": True}


# ── Generation endpoints: graceful degradation ───────────────────────────────

class TestNoProviderConfigured:
    def test_generate_returns_400_with_hint(self, client, db):
        user = _create_user(db)
        campaign = _make_campaign(db, user)
        _set_user(user)
        with patch.object(ai_service.settings, "openai_api_key", ""):
            resp = client.post(f"/campaigns/{campaign.id}/generate", data={"csrf_token": "test"})
        assert resp.status_code == 400
        assert b"Account page" in resp.content

    def test_generate_uses_user_key_when_env_empty(self, client, db):
        user = _create_user(db)
        db.add(UserAIConfig(user_id=user.id, api_key=encrypt_token("user-key")))
        db.commit()
        campaign = _make_campaign(db, user)
        _set_user(user)

        with patch.object(ai_service.settings, "openai_api_key", ""):
            with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai("P1\n---\nP2")):
                resp = client.post(f"/campaigns/{campaign.id}/generate", data={"csrf_token": "test"})

        assert resp.status_code == 302
        posts = db.exec(select(Post).where(Post.campaign_id == campaign.id)).all()
        assert len(posts) == 2
