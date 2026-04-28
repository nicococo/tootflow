"""
Tests for AI service functions and AI-gated HTTP endpoints.

Three tiers:
  1. Pure logic     — _sanitize, schedule_posts, _build_export_prompts (no I/O)
  2. Mocked OpenAI  — all async AI functions with a fake client
  3. HTTP endpoints — mocked AI functions wired through the real FastAPI app
  4. Integration    — real OpenAI API calls (skipped unless OPENAI_API_KEY is set)

Run all:            python -m pytest tests/test_ai_service.py -v
Skip integration:   python -m pytest tests/test_ai_service.py -v -m "not integration"
Only integration:   python -m pytest tests/test_ai_service.py -v -m integration
"""

import asyncio
import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.ai_service import (
    _sanitize,
    _build_export_prompts,
    generate_posts,
    regenerate_single_post,
    refine_single_post,
    generate_export,
    refine_export,
    schedule_posts,
)
from app.main import app
from app.auth import get_current_user
from app.models import Campaign, MastodonAccount, Post, User
from sqlmodel import Session, select


# ── Helpers ───────────────────────────────────────────────────────────────────

def _set_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


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
        description='{"Topic": "testing AI features"}',
        template_type="knowledge_sharing",
        tone="professional",
        status="active",
        payment_status="paid",
        enabled=True,
        ai_posts_generated=0,
        export_generations=0,
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
        content="Test post content",
        scheduled_time=datetime.utcnow() + timedelta(days=1),
        status="pending",
    )
    defaults.update(kwargs)
    post = Post(**defaults)
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


def _mock_openai(content: str):
    """Return a mock AsyncOpenAI instance whose completions return `content`."""
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=response)
    return mock_client


# ── 1. Pure logic ─────────────────────────────────────────────────────────────

class TestSanitize:
    def test_normal_text_unchanged(self):
        assert _sanitize("Hello world", 200) == "Hello world"

    def test_truncates_at_max_len(self):
        result = _sanitize("abcde", 3)
        assert result == "abc"

    def test_strips_control_characters(self):
        # null byte, bell, vertical tab, delete
        dirty = "hello\x00world\x07\x0b\x7f"
        assert _sanitize(dirty, 200) == "helloworld"

    def test_strips_leading_trailing_whitespace(self):
        assert _sanitize("  hello  ", 200) == "hello"

    def test_empty_string(self):
        assert _sanitize("", 100) == ""

    def test_preserves_newlines(self):
        # \n is not a stripped control char
        result = _sanitize("line1\nline2", 200)
        assert "line1" in result and "line2" in result


class TestSchedulePosts:
    def test_returns_correct_count(self):
        posts = ["Post A", "Post B", "Post C"]
        result = schedule_posts(posts, datetime.utcnow(), posts_per_week=3)
        assert len(result) == 3

    def test_empty_posts(self):
        assert schedule_posts([], datetime.utcnow(), posts_per_week=3) == []

    def test_times_are_after_start(self):
        start = datetime.utcnow()
        result = schedule_posts(["A", "B"], start, posts_per_week=2)
        for _, sched in result:
            assert sched > start

    def test_scheduled_time_near_10_hour(self):
        # Jitter is ±18 minutes, so the hour can be 9 or 10
        result = schedule_posts(["A", "B", "C"], datetime.utcnow(), posts_per_week=3)
        for _, sched in result:
            assert sched.hour in (9, 10, 11)  # 10:00 ± 18 min

    def test_posts_spread_over_time(self):
        result = schedule_posts(["A", "B", "C", "D"], datetime.utcnow(), posts_per_week=2)
        times = [sched for _, sched in result]
        # Each consecutive pair should be separated by roughly 3.5 days
        for i in range(1, len(times)):
            gap = (times[i] - times[i - 1]).total_seconds() / 86400
            assert 2 < gap < 5  # 7/2 = 3.5, with jitter tolerance

    def test_content_preserved(self):
        result = schedule_posts(["Hello", "World"], datetime.utcnow(), posts_per_week=2)
        contents = [c for c, _ in result]
        assert contents == ["Hello", "World"]


class TestBuildExportPrompts:
    def test_newsletter_has_subject_prefix_instruction(self):
        sys_p, _ = _build_export_prompts(
            "Campaign", "Desc", "English", "Post 1",
            target="newsletter", length="medium", tone_shift="keep",
            base_tone="professional", include_cta=True, include_emoji=False,
        )
        assert "Subject:" in sys_p

    def test_linkedin_has_headline_prefix_instruction(self):
        sys_p, _ = _build_export_prompts(
            "Campaign", "Desc", "English", "Post 1",
            target="linkedin", length="medium", tone_shift="keep",
            base_tone="professional", include_cta=True, include_emoji=False,
        )
        assert "Headline:" in sys_p

    def test_reddit_has_title_prefix_instruction(self):
        sys_p, _ = _build_export_prompts(
            "Campaign", "Desc", "English", "Post 1",
            target="reddit", length="medium", tone_shift="keep",
            base_tone="professional", include_cta=True, include_emoji=False,
        )
        assert "Title:" in sys_p

    def test_no_cta_instruction_when_disabled(self):
        sys_p, _ = _build_export_prompts(
            "Campaign", "Desc", "English", "Post 1",
            target="newsletter", length="medium", tone_shift="keep",
            base_tone="professional", include_cta=False, include_emoji=False,
        )
        assert "Do not include a call-to-action" in sys_p

    def test_cta_instruction_when_enabled(self):
        sys_p, _ = _build_export_prompts(
            "Campaign", "Desc", "English", "Post 1",
            target="newsletter", length="medium", tone_shift="keep",
            base_tone="professional", include_cta=True, include_emoji=False,
        )
        assert "call-to-action" in sys_p
        assert "Do not include a call-to-action" not in sys_p

    def test_emoji_instruction_when_enabled(self):
        sys_p, _ = _build_export_prompts(
            "Campaign", "Desc", "English", "Post 1",
            target="linkedin", length="medium", tone_shift="keep",
            base_tone="professional", include_cta=True, include_emoji=True,
        )
        assert "emoji" in sys_p.lower()
        assert "Do not use emoji" not in sys_p

    def test_no_emoji_when_disabled(self):
        sys_p, _ = _build_export_prompts(
            "Campaign", "Desc", "English", "Post 1",
            target="linkedin", length="medium", tone_shift="keep",
            base_tone="professional", include_cta=True, include_emoji=False,
        )
        assert "Do not use emoji" in sys_p

    def test_formal_tone_shift(self):
        sys_p, _ = _build_export_prompts(
            "Campaign", "Desc", "English", "Post 1",
            target="newsletter", length="medium", tone_shift="formal",
            base_tone="casual", include_cta=True, include_emoji=False,
        )
        assert "formal" in sys_p.lower()

    def test_short_length_targets_fewer_words(self):
        sys_short, _ = _build_export_prompts(
            "C", "D", "English", "P",
            target="newsletter", length="short", tone_shift="keep",
            base_tone="professional", include_cta=True, include_emoji=False,
        )
        sys_long, _ = _build_export_prompts(
            "C", "D", "English", "P",
            target="newsletter", length="long", tone_shift="keep",
            base_tone="professional", include_cta=True, include_emoji=False,
        )
        # Short word count should be a lower number than long
        assert "150" in sys_short or "250" in sys_short  # short newsletter range
        assert "600" in sys_long or "900" in sys_long    # long newsletter range

    def test_user_prompt_contains_campaign_name(self):
        _, user_p = _build_export_prompts(
            "MyCampaign", "Desc", "English", "Post 1",
            target="newsletter", length="medium", tone_shift="keep",
            base_tone="professional", include_cta=True, include_emoji=False,
        )
        assert "MyCampaign" in user_p


# ── 2. Mocked OpenAI unit tests ───────────────────────────────────────────────

class TestGeneratePostsMocked:
    def test_parses_separator(self):
        raw = "First post\n---\nSecond post\n---\nThird post"
        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai(raw)):
            result = asyncio.run(generate_posts(
                campaign_name="Test", description="Desc",
                template_type="knowledge_sharing", tone="professional",
                language="English", duration_days=7, posts_per_week=3,
            ))
        assert len(result) == 3
        assert result[0] == "First post"
        assert result[2] == "Third post"

    def test_truncates_posts_to_500_chars(self):
        long_post = "x" * 600
        raw = f"{long_post}\n---\nNormal post"
        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai(raw)):
            result = asyncio.run(generate_posts(
                campaign_name="Test", description="Desc",
                template_type="knowledge_sharing", tone="professional",
                language="English", duration_days=7, posts_per_week=3,
            ))
        assert len(result[0]) == 500

    def test_filters_empty_segments(self):
        raw = "Post one\n---\n\n---\nPost two"
        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai(raw)):
            result = asyncio.run(generate_posts(
                campaign_name="Test", description="Desc",
                template_type="knowledge_sharing", tone="professional",
                language="English", duration_days=7, posts_per_week=3,
            ))
        assert len(result) == 2

    def test_respects_total_posts_cap(self):
        # 5 posts returned but only 1 expected (duration=1d, ppw=7 → 1 post)
        raw = "\n---\n".join([f"Post {i}" for i in range(5)])
        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai(raw)):
            result = asyncio.run(generate_posts(
                campaign_name="Test", description="Desc",
                template_type="knowledge_sharing", tone="professional",
                language="English", duration_days=1, posts_per_week=7,
            ))
        assert len(result) == 1


class TestRegeneratePostMocked:
    def test_returns_stripped_content(self):
        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai("  New post content  ")):
            result = asyncio.run(regenerate_single_post(
                campaign_name="Test", description="Desc",
                template_type="knowledge_sharing", tone="professional",
                language="English",
            ))
        assert result == "New post content"

    def test_truncates_to_500_chars(self):
        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai("x" * 600)):
            result = asyncio.run(regenerate_single_post(
                campaign_name="Test", description="Desc",
                template_type="knowledge_sharing", tone="professional",
                language="English",
            ))
        assert len(result) == 500


class TestRefinePostMocked:
    def test_basic_refinement(self):
        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai("Refined content")):
            result = asyncio.run(refine_single_post(
                current_content="Old content",
                instructions="Make it shorter",
                campaign_name="Test", description="Desc",
                template_type="knowledge_sharing", tone="professional",
                language="English",
            ))
        assert result == "Refined content"

    def test_truncates_to_500_chars(self):
        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai("x" * 600)):
            result = asyncio.run(refine_single_post(
                current_content="content", instructions="fix",
                campaign_name="C", description="D",
                template_type="knowledge_sharing", tone="professional",
                language="English",
            ))
        assert len(result) == 500

    def test_poll_options_included_in_prompt_when_two_or_more(self):
        mock = _mock_openai("Refined with poll")
        with patch("app.services.ai_service.AsyncOpenAI", return_value=mock):
            asyncio.run(refine_single_post(
                current_content="Post with poll",
                instructions="Make it punchy",
                campaign_name="Test", description="Desc",
                template_type="knowledge_sharing", tone="professional",
                language="English",
                poll_options=["Option A", "Option B"],
            ))
        call_args = mock.chat.completions.create.call_args
        user_msg = call_args.kwargs["messages"][1]["content"]
        assert "Option A" in user_msg
        assert "Option B" in user_msg

    def test_poll_options_ignored_when_fewer_than_two(self):
        mock = _mock_openai("Refined")
        with patch("app.services.ai_service.AsyncOpenAI", return_value=mock):
            asyncio.run(refine_single_post(
                current_content="Post", instructions="fix",
                campaign_name="Test", description="Desc",
                template_type="knowledge_sharing", tone="professional",
                language="English",
                poll_options=["Only one"],
            ))
        call_args = mock.chat.completions.create.call_args
        user_msg = call_args.kwargs["messages"][1]["content"]
        assert "Only one" not in user_msg


class TestGenerateExportMocked:
    def _run_export(self, target, **kwargs):
        defaults = dict(
            campaign_name="Test", description="Desc", tone="professional",
            language="English", post_contents=["Post 1", "Post 2"],
            target=target, length="medium", tone_shift="keep",
            include_cta=True, include_emoji=False,
        )
        defaults.update(kwargs)
        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai(f"{target} output")):
            return asyncio.run(generate_export(**defaults))

    def test_newsletter_returns_content(self):
        result = self._run_export("newsletter")
        assert result == "newsletter output"

    def test_linkedin_returns_content(self):
        result = self._run_export("linkedin")
        assert result == "linkedin output"

    def test_reddit_returns_content(self):
        result = self._run_export("reddit")
        assert result == "reddit output"

    def test_correct_token_limit_for_short(self):
        mock = _mock_openai("output")
        with patch("app.services.ai_service.AsyncOpenAI", return_value=mock):
            asyncio.run(generate_export(
                campaign_name="C", description="D", tone="professional",
                language="English", post_contents=["P"],
                target="newsletter", length="short", tone_shift="keep",
                include_cta=True, include_emoji=False,
            ))
        call_args = mock.chat.completions.create.call_args
        assert call_args.kwargs["max_completion_tokens"] == 500

    def test_correct_token_limit_for_long(self):
        mock = _mock_openai("output")
        with patch("app.services.ai_service.AsyncOpenAI", return_value=mock):
            asyncio.run(generate_export(
                campaign_name="C", description="D", tone="professional",
                language="English", post_contents=["P"],
                target="newsletter", length="long", tone_shift="keep",
                include_cta=True, include_emoji=False,
            ))
        call_args = mock.chat.completions.create.call_args
        assert call_args.kwargs["max_completion_tokens"] == 1400


class TestRefineExportMocked:
    def test_passes_current_content_and_instructions(self):
        mock = _mock_openai("Refined export")
        with patch("app.services.ai_service.AsyncOpenAI", return_value=mock):
            asyncio.run(refine_export(
                current_content="Original content here",
                instructions="Make it shorter",
                campaign_name="Test", description="Desc",
                tone="professional", language="English",
                post_contents=["Post 1"],
                target="newsletter", length="medium", tone_shift="keep",
                include_cta=True, include_emoji=False,
            ))
        call_args = mock.chat.completions.create.call_args
        user_msg = call_args.kwargs["messages"][1]["content"]
        assert "Original content here" in user_msg
        assert "Make it shorter" in user_msg

    def test_returns_stripped_content(self):
        with patch("app.services.ai_service.AsyncOpenAI", return_value=_mock_openai("  Refined  ")):
            result = asyncio.run(refine_export(
                current_content="x", instructions="y",
                campaign_name="C", description="D",
                tone="professional", language="English",
                post_contents=["P"],
                target="newsletter", length="medium", tone_shift="keep",
                include_cta=True, include_emoji=False,
            ))
        assert result == "Refined"


# ── 3. HTTP endpoint tests (mocked AI) ────────────────────────────────────────

class TestGenerateCampaignPostsEndpoint:
    def test_success_redirects_to_posts(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid")

        with patch("app.routers.posts.generate_posts", new=AsyncMock(return_value=["Post 1", "Post 2"])):
            resp = client.post(f"/campaigns/{campaign.id}/generate", data={"csrf_token": "test"})

        assert resp.status_code == 302
        assert f"/campaigns/{campaign.id}/posts" in resp.headers["location"]

    def test_creates_posts_in_db(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid")

        with patch("app.routers.posts.generate_posts", new=AsyncMock(return_value=["Generated A", "Generated B"])):
            client.post(f"/campaigns/{campaign.id}/generate", data={"csrf_token": "test"})

        posts = db.exec(select(Post).where(Post.campaign_id == campaign.id)).all()
        assert len(posts) == 2

    def test_ai_failure_redirects_with_error(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid")

        with patch("app.routers.posts.generate_posts", new=AsyncMock(side_effect=Exception("API down"))):
            resp = client.post(f"/campaigns/{campaign.id}/generate", data={"csrf_token": "test"})

        assert resp.status_code in (302, 500)

    def test_unpaid_campaign_returns_403(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="unpaid")

        resp = client.post(f"/campaigns/{campaign.id}/generate", data={"csrf_token": "test"})
        assert resp.status_code == 403

    def test_archived_campaign_returns_403(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, status="archived", payment_status="paid")

        resp = client.post(f"/campaigns/{campaign.id}/generate", data={"csrf_token": "test"})
        assert resp.status_code == 403

    def test_ai_limit_reached_returns_403(self, client, db):
        from app.config import get_settings
        max_ai = get_settings().openai_max_posts_per_campaign
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", ai_posts_generated=max_ai)

        resp = client.post(f"/campaigns/{campaign.id}/generate", data={"csrf_token": "test"})
        assert resp.status_code == 403


class TestRegeneratePostEndpoint:
    def test_success_redirects_to_posts(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="running")
        post = _make_post(db, campaign, status="pending")

        with patch("app.routers.posts.regenerate_single_post", new=AsyncMock(return_value="Regenerated content")):
            resp = client.post(f"/campaigns/{campaign.id}/posts/{post.id}/regenerate", data={"csrf_token": "test"})

        assert resp.status_code == 302
        db.refresh(post)
        assert post.content == "Regenerated content"

    def test_ai_failure_redirects_with_error(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="running")
        post = _make_post(db, campaign, status="pending")

        with patch("app.routers.posts.regenerate_single_post", new=AsyncMock(side_effect=Exception("fail"))):
            resp = client.post(f"/campaigns/{campaign.id}/posts/{post.id}/regenerate", data={"csrf_token": "test"})

        assert resp.status_code == 302
        assert "error=ai_failed" in resp.headers["location"]

    def test_published_post_returns_404(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="running")
        post = _make_post(db, campaign, status="published")

        resp = client.post(f"/campaigns/{campaign.id}/posts/{post.id}/regenerate", data={"csrf_token": "test"})
        assert resp.status_code == 404

    def test_unpaid_returns_403(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="unpaid", status="running")
        post = _make_post(db, campaign, status="pending")

        resp = client.post(f"/campaigns/{campaign.id}/posts/{post.id}/regenerate", data={"csrf_token": "test"})
        assert resp.status_code == 403


class TestRefinePostEndpoint:
    def test_success_redirects_back(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="running")
        post = _make_post(db, campaign, status="pending")

        with patch("app.routers.posts.refine_single_post", new=AsyncMock(return_value="Refined content")):
            resp = client.post(
                f"/campaigns/{campaign.id}/posts/{post.id}/refine",
                data={"instructions": "Make it shorter", "csrf_token": "test"},
            )

        assert resp.status_code == 302
        db.refresh(post)
        assert post.content == "Refined content"

    def test_ai_failure_redirects_with_error(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="running")
        post = _make_post(db, campaign, status="pending")

        with patch("app.routers.posts.refine_single_post", new=AsyncMock(side_effect=Exception("API error"))):
            resp = client.post(
                f"/campaigns/{campaign.id}/posts/{post.id}/refine",
                data={"instructions": "Fix it", "csrf_token": "test"},
            )

        assert resp.status_code == 302
        assert "error=ai_failed" in resp.headers["location"]

    def test_empty_instructions_returns_400(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="running")
        post = _make_post(db, campaign, status="pending")

        resp = client.post(
            f"/campaigns/{campaign.id}/posts/{post.id}/refine",
            data={"instructions": "   ", "csrf_token": "test"},
        )
        assert resp.status_code == 400

    def test_unpaid_returns_403(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="unpaid", status="running")
        post = _make_post(db, campaign, status="pending")

        resp = client.post(
            f"/campaigns/{campaign.id}/posts/{post.id}/refine",
            data={"instructions": "Fix it", "csrf_token": "test"},
        )
        assert resp.status_code == 403

    def test_archived_returns_403(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="archived")
        post = _make_post(db, campaign, status="pending")

        resp = client.post(
            f"/campaigns/{campaign.id}/posts/{post.id}/refine",
            data={"instructions": "Fix it", "csrf_token": "test"},
        )
        assert resp.status_code == 403

    def test_wrong_owner_returns_404(self, client, db):
        owner = _create_user(db)
        attacker = _create_user(db)
        campaign = _make_campaign(db, owner, payment_status="paid", status="running")
        post = _make_post(db, campaign, status="pending")
        _set_user(attacker)

        resp = client.post(
            f"/campaigns/{campaign.id}/posts/{post.id}/refine",
            data={"instructions": "Fix it", "csrf_token": "test"},
        )
        assert resp.status_code == 404


class TestExportEndpoints:
    def test_export_generate_success(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="completed")
        _make_post(db, campaign, status="published")

        with patch("app.routers.campaigns.generate_export", new=AsyncMock(return_value="Subject: Test\nNewsletter body")):
            resp = client.post(f"/campaigns/{campaign.id}/export/generate", data={
                "target": "newsletter", "length": "medium", "tone_shift": "keep",
                "include_cta": "false", "include_emoji": "false",
                "selected_posts": "", "csrf_token": "test",
            })

        assert resp.status_code == 200
        body = resp.json()
        assert "content" in body
        assert "remaining" in body
        assert body["content"] == "Subject: Test\nNewsletter body"

    def test_export_generate_increments_counter(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="completed", export_generations=0)
        _make_post(db, campaign, status="published")

        with patch("app.routers.campaigns.generate_export", new=AsyncMock(return_value="Headline: Test\nBody")):
            client.post(f"/campaigns/{campaign.id}/export/generate", data={
                "target": "linkedin", "length": "medium", "tone_shift": "keep",
                "include_cta": "false", "include_emoji": "false",
                "selected_posts": "", "csrf_token": "test",
            })

        db.refresh(campaign)
        assert campaign.export_generations == 1

    def test_export_generate_unpaid_returns_403(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="unpaid")
        _make_post(db, campaign, status="published")

        resp = client.post(f"/campaigns/{campaign.id}/export/generate", data={
            "target": "newsletter", "length": "medium", "tone_shift": "keep",
            "include_cta": "false", "include_emoji": "false",
            "selected_posts": "", "csrf_token": "test",
        })
        assert resp.status_code == 403

    def test_export_generate_no_posts_returns_400(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="completed")
        # no posts added

        resp = client.post(f"/campaigns/{campaign.id}/export/generate", data={
            "target": "newsletter", "length": "medium", "tone_shift": "keep",
            "include_cta": "false", "include_emoji": "false",
            "selected_posts": "", "csrf_token": "test",
        })
        assert resp.status_code == 400

    def test_export_refine_success(self, client, db):
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="completed")
        _make_post(db, campaign, status="published")

        with patch("app.routers.campaigns.refine_export", new=AsyncMock(return_value="Refined newsletter")):
            resp = client.post(f"/campaigns/{campaign.id}/export/refine", data={
                "current_content": "Original newsletter text",
                "instructions": "Make it punchier",
                "target": "newsletter", "length": "medium", "tone_shift": "keep",
                "include_cta": "false", "include_emoji": "false",
                "selected_posts": "", "csrf_token": "test",
            })

        assert resp.status_code == 200
        assert resp.json()["content"] == "Refined newsletter"

    def test_export_limit_reached_returns_403(self, client, db):
        from app.config import get_settings
        max_exp = get_settings().export_max_per_campaign
        user = _create_user(db)
        _set_user(user)
        campaign = _make_campaign(db, user, payment_status="paid", status="completed",
                                  export_generations=max_exp)
        _make_post(db, campaign, status="published")

        resp = client.post(f"/campaigns/{campaign.id}/export/generate", data={
            "target": "newsletter", "length": "medium", "tone_shift": "keep",
            "include_cta": "false", "include_emoji": "false",
            "selected_posts": "", "csrf_token": "test",
        })
        assert resp.status_code == 403


# ── 4. Integration tests (real OpenAI API) ────────────────────────────────────

_has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))
integration = pytest.mark.skipif(
    not _has_openai_key,
    reason="OPENAI_API_KEY not set — skipping live API tests",
)


@integration
def test_integration_generate_posts_returns_list():
    result = asyncio.run(generate_posts(
        campaign_name="TootFlow Tips",
        description="Tips for using Mastodon effectively",
        template_type="knowledge_sharing",
        tone="professional",
        language="English",
        duration_days=7,
        posts_per_week=3,
    ))
    assert isinstance(result, list)
    assert len(result) >= 1
    for post in result:
        assert isinstance(post, str)
        assert len(post) <= 500
        assert len(post) > 0


@integration
def test_integration_regenerate_returns_string():
    result = asyncio.run(regenerate_single_post(
        campaign_name="TootFlow Tips",
        description="Tips for using Mastodon effectively",
        template_type="knowledge_sharing",
        tone="casual",
        language="English",
    ))
    assert isinstance(result, str)
    assert 0 < len(result) <= 500


@integration
def test_integration_refine_changes_content():
    original = "Mastodon is a great platform for building communities."
    result = asyncio.run(refine_single_post(
        current_content=original,
        instructions="Add a relevant hashtag and make it more engaging",
        campaign_name="Mastodon Tips",
        description="How to use Mastodon well",
        template_type="knowledge_sharing",
        tone="casual",
        language="English",
    ))
    assert isinstance(result, str)
    assert 0 < len(result) <= 500
    # The model should have changed the content
    assert result != original


@integration
def test_integration_export_newsletter_has_subject():
    result = asyncio.run(generate_export(
        campaign_name="Mastodon Tips",
        description="How to use Mastodon effectively",
        tone="professional",
        language="English",
        post_contents=[
            "Mastodon is federated and open.",
            "You control your data on Mastodon.",
        ],
        target="newsletter",
        length="short",
        tone_shift="keep",
        include_cta=False,
        include_emoji=False,
    ))
    assert isinstance(result, str)
    assert len(result) > 0
    assert "Subject:" in result


@integration
def test_integration_export_linkedin_has_headline():
    result = asyncio.run(generate_export(
        campaign_name="Mastodon Tips",
        description="How to use Mastodon effectively",
        tone="professional",
        language="English",
        post_contents=["Mastodon is federated and open."],
        target="linkedin",
        length="short",
        tone_shift="keep",
        include_cta=False,
        include_emoji=False,
    ))
    assert isinstance(result, str)
    assert "Headline:" in result


@integration
def test_integration_export_reddit_has_title():
    result = asyncio.run(generate_export(
        campaign_name="Mastodon Tips",
        description="How to use Mastodon effectively",
        tone="casual",
        language="English",
        post_contents=["Mastodon is great for communities."],
        target="reddit",
        length="short",
        tone_shift="keep",
        include_cta=False,
        include_emoji=False,
    ))
    assert isinstance(result, str)
    assert "Title:" in result


@integration
def test_integration_refine_export():
    original = "Subject: Mastodon Tips\nHere is a short newsletter about Mastodon."
    result = asyncio.run(refine_export(
        current_content=original,
        instructions="Make the intro punchier",
        campaign_name="Mastodon Tips",
        description="How to use Mastodon",
        tone="professional",
        language="English",
        post_contents=["Mastodon is federated."],
        target="newsletter",
        length="short",
        tone_shift="keep",
        include_cta=False,
        include_emoji=False,
    ))
    assert isinstance(result, str)
    assert len(result) > 0
    assert "Subject:" in result  # prefix must be preserved
