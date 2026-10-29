"""
Tests for the tootflow CLI (agent-harness interface).

Covers:
- user/account resolution (single-user assumption, auto-bootstrap)
- campaigns create/list/get/archive/remove
- posts add/import (auto-schedule)/list/get/edit/move/remove/send
- ops publish + metrics-refresh (mocked Mastodon)
- JSON output, --yes enforcement, exit codes
"""

import asyncio
import io
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.cli.main import main
from app.models import Campaign, MastodonAccount, Metrics, Post
from app.crypto import encrypt_token


@pytest.fixture()
def cli_db(monkeypatch):
    """Fresh in-memory DB per test — the CLI must see a deterministic world."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    import app.cli.common as common
    import app.scheduler as scheduler
    monkeypatch.setattr(common, "engine", engine)
    monkeypatch.setattr(scheduler, "engine", engine)
    yield engine


@pytest.fixture()
def db(cli_db):
    with Session(cli_db) as session:
        yield session


def _run(*argv, expect_code=0):
    code = main(list(argv))
    assert code == expect_code, f"exit {code} for {argv}"


def _add_account(db, username="muser"):
    from app.cli.common import resolve_user
    user = resolve_user(db)
    acc = MastodonAccount(
        user_id=user.id,
        instance_url="https://mastodon.social",
        access_token=encrypt_token("tok"),
        username=username,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


def _campaign(db, **kwargs):
    from app.cli.common import resolve_user
    user = resolve_user(db)
    defaults = dict(
        user_id=user.id,
        name="CLI Camp",
        description="{}",
        template_type="knowledge_sharing",
        tone="professional",
        status="active",
        start_date=datetime.utcnow() - timedelta(days=1),
    )
    defaults.update(kwargs)
    c = Campaign(**defaults)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _with_stdin(payload: str):
    return patch("sys.stdin", new=io.StringIO(payload))


# ── User/account resolution ──────────────────────────────────────────────────

class TestResolution:
    def test_first_call_bootstraps_user(self, db, capsys):
        _run("accounts", "list")
        out = capsys.readouterr().out
        assert "No accounts connected" in out

    def test_ambiguous_user_requires_user_id(self, db):
        from app.models import User
        db.add(User())
        db.add(User())
        db.commit()
        with pytest.raises(SystemExit) as e:
            main(["accounts", "list"])
        assert e.value.code == 1

    def test_multiple_accounts_require_account_flag(self, db):
        _add_account(db, "one")
        _add_account(db, "two")
        # campaigns create must pick a posting account — ambiguous without --account
        with pytest.raises(SystemExit) as e:
            main(["campaigns", "create", "--name", "X"])
        assert e.value.code == 1

    def test_account_by_handle_and_id(self, db, capsys):
        acc = _add_account(db, "one")
        second = MastodonAccount(
            user_id=acc.user_id, instance_url="https://fosstodon.org",
            access_token=encrypt_token("t"), username="two",
        )
        db.add(second)
        db.commit()
        db.refresh(second)
        # resolution by handle and by id, on campaign creation
        _run("campaigns", "create", "--name", "A", "--account", "@one@mastodon.social", "--json")
        _run("campaigns", "create", "--name", "B", "--account", str(second.id), "--json")
        a = db.exec(select(Campaign).where(Campaign.name == "A")).first()
        b = db.exec(select(Campaign).where(Campaign.name == "B")).first()
        assert a.mastodon_account_id == acc.id
        assert b.mastodon_account_id == second.id


# ── Campaigns ────────────────────────────────────────────────────────────────

class TestCampaigns:
    def test_create_starts_active_with_dates(self, db, capsys):
        _add_account(db)
        _run("campaigns", "create", "--name", "Launch", "--json")
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "active"
        assert payload["start_date"] is not None
        assert payload["end_date"] is not None

    def test_list_and_get_json(self, db, capsys):
        _add_account(db)
        c = _campaign(db)
        _run("campaigns", "list", "--json")
        rows = json.loads(capsys.readouterr().out)
        assert [r["id"] for r in rows] == [c.id]
        assert rows[0]["posts_total"] == 0

        _run("campaigns", "get", str(c.id), "--json")
        data = json.loads(capsys.readouterr().out)
        assert data["id"] == c.id and data["posts"] == []

    def test_archive_cancels_pending(self, db, capsys):
        _add_account(db)
        c = _campaign(db)
        p = Post(campaign_id=c.id, content="x",
                 scheduled_time=datetime.utcnow() + timedelta(days=1), status="pending")
        db.add(p)
        db.commit()
        _run("campaigns", "archive", str(c.id), "--json")
        out = json.loads(capsys.readouterr().out)
        assert out["cancelled_posts"] == 1
        db.expire_all()  # CLI ran in its own session — drop stale identity map
        assert p.status == "cancelled"
        assert c.status == "archived"

    def test_remove_requires_yes(self, db):
        _add_account(db)
        c = _campaign(db)
        with pytest.raises(SystemExit) as e:
            main(["campaigns", "remove", str(c.id)])
        assert e.value.code == 2  # usage error, nothing deleted
        assert db.get(Campaign, c.id) is not None

    def test_remove_deletes_posts_and_metrics(self, db, capsys):
        _add_account(db)
        c = _campaign(db)
        p = Post(campaign_id=c.id, content="x",
                 scheduled_time=datetime.utcnow(), status="published")
        db.add(p)
        db.commit()
        m = Metrics(post_id=p.id, favourites=3)
        db.add(m)
        db.commit()
        cid, pid, mid = c.id, p.id, m.id  # plain ints — the instances get deleted below
        _run("campaigns", "remove", str(c.id), "--yes", "--json")
        out = json.loads(capsys.readouterr().out)
        assert out == {"removed": c.id, "posts_deleted": 1}
        # fresh SQL queries with captured ids — expired instances would raise
        assert db.exec(select(Campaign).where(Campaign.id == cid)).first() is None
        assert db.exec(select(Post).where(Post.id == pid)).first() is None
        assert db.exec(select(Metrics).where(Metrics.id == mid)).first() is None


# ── Posts ────────────────────────────────────────────────────────────────────

class TestPosts:
    def test_add_with_explicit_time_and_poll(self, db, capsys):
        _add_account(db)
        c = _campaign(db)
        _run("posts", "add", "--campaign", str(c.id),
             "--content", "Hello world", "--at", "2030-01-01T10:00",
             "--poll", "Yes,No", "--json")
        p = json.loads(capsys.readouterr().out)
        assert p["content"] == "Hello world"
        assert p["poll_options"] == '["Yes", "No"]'
        assert p["scheduled_time"].startswith("2030-01-01T10:00")

    def test_add_advances_draft_to_active(self, db, capsys):
        _add_account(db)
        c = _campaign(db, status="draft")
        _run("posts", "add", "--campaign", str(c.id),
             "--content", "s", "--at", "2030-01-01T10:00")
        db.refresh(c)
        assert c.status == "active"
        assert c.start_date is not None

    def test_add_rejects_single_poll_option(self, db):
        _add_account(db)
        c = _campaign(db)
        with pytest.raises(SystemExit) as e:
            main(["posts", "add", "--campaign", str(c.id), "--content", "x",
                  "--at", "2030-01-01T10:00", "--poll", "Only"])
        assert e.value.code == 1

    def test_add_rejects_both_content_and_file(self, db):
        _add_account(db)
        c = _campaign(db)
        with pytest.raises(SystemExit) as e:
            main(["posts", "add", "--campaign", str(c.id), "--content", "x",
                  "--file", "f.md", "--at", "2030-01-01T10:00"])
        assert e.value.code == 1

    def test_import_auto_schedules(self, db, capsys):
        _add_account(db)
        c = _campaign(db, posts_per_week=7)
        payload = [
            {"content": "one"},
            {"content": "two", "at": "2030-06-01T09:00", "poll": ["a", "b"]},
            {"content": "three"},
        ]
        with _with_stdin(json.dumps(payload)):
            _run("posts", "import", "--campaign", str(c.id),
                 "--json-file", "-", "--json")
        out = json.loads(capsys.readouterr().out)
        assert len(out["added"]) == 3
        assert out["added"][1]["scheduled_time"].startswith("2030-06-01T09:00")
        # auto-scheduled ones get distinct times from the spacing logic
        assert out["added"][0]["scheduled_time"] != out["added"][2]["scheduled_time"]
        times = {p.scheduled_time for p in db.exec(select(Post).where(Post.campaign_id == c.id)).all()}
        assert len(times) == 3

    def test_import_skip_duplicate(self, db, capsys):
        _add_account(db)
        c = _campaign(db)
        seed = Post(campaign_id=c.id, content="one",
                    scheduled_time=datetime.utcnow() + timedelta(days=1), status="pending")
        db.add(seed)
        db.commit()
        payload = [{"content": "one"}, {"content": "two"}]
        with _with_stdin(json.dumps(payload)):
            _run("posts", "import", "--campaign", str(c.id),
                 "--json-file", "-", "--skip-duplicate", "--json")
        out = json.loads(capsys.readouterr().out)
        assert len(out["added"]) == 1
        assert out["added"][0]["content"] == "two"
        assert len(out["skipped"]) == 1

    def test_move_updates_schedule(self, db, capsys):
        _add_account(db)
        c = _campaign(db)
        p = Post(campaign_id=c.id, content="x",
                 scheduled_time=datetime.utcnow() + timedelta(days=1), status="pending")
        db.add(p)
        db.commit()
        _run("posts", "move", str(p.id), "--at", "2031-01-01T08:00", "--json")
        data = json.loads(capsys.readouterr().out)
        assert data["scheduled_time"].startswith("2031-01-01T08:00")

    def test_edit_rejects_published(self, db):
        _add_account(db)
        c = _campaign(db)
        p = Post(campaign_id=c.id, content="x",
                 scheduled_time=datetime.utcnow(), status="published")
        db.add(p)
        db.commit()
        with pytest.raises(SystemExit) as e:
            main(["posts", "edit", str(p.id), "--content", "new"])
        assert e.value.code == 1

    def test_remove_requires_yes(self, db):
        _add_account(db)
        c = _campaign(db)
        p = Post(campaign_id=c.id, content="x",
                 scheduled_time=datetime.utcnow(), status="pending")
        db.add(p)
        db.commit()
        with pytest.raises(SystemExit) as e:
            main(["posts", "remove", str(p.id)])
        assert e.value.code == 2
        assert db.get(Post, p.id) is not None

    def test_send_publishes_now(self, db, capsys):
        _add_account(db)
        c = _campaign(db)
        p = Post(campaign_id=c.id, content="x",
                 scheduled_time=datetime.utcnow() + timedelta(days=5), status="pending")
        db.add(p)
        db.commit()

        with patch("app.scheduler.mastodon_service.publish_post",
                   AsyncMock(return_value={"id": "m-77"})):
            _run("posts", "send", str(p.id), "--json")

        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "published"
        assert out["mastodon_status_id"] == "m-77"
        db.refresh(c)
        assert c.status == "running"  # first publish advances the campaign

    def test_send_paused_campaign_fails(self, db):
        _add_account(db)
        c = _campaign(db, enabled=False)
        p = Post(campaign_id=c.id, content="x",
                 scheduled_time=datetime.utcnow(), status="pending")
        db.add(p)
        db.commit()
        with pytest.raises(SystemExit) as e:
            main(["posts", "send", str(p.id)])
        assert e.value.code == 1


# ── Ops ──────────────────────────────────────────────────────────────────────

class TestOps:
    def test_publish_reports_status_flips(self, db, capsys):
        _add_account(db)
        c = _campaign(db)
        due = Post(campaign_id=c.id, content="due",
                   scheduled_time=datetime.utcnow() - timedelta(minutes=5), status="pending")
        future = Post(campaign_id=c.id, content="future",
                      scheduled_time=datetime.utcnow() + timedelta(days=1), status="pending")
        db.add_all([due, future])
        db.commit()

        with patch("app.scheduler.mastodon_service.publish_post",
                   AsyncMock(return_value={"id": "m-1"})):
            _run("ops", "publish", "--json")

        out = json.loads(capsys.readouterr().out)
        assert len(out) == 1  # only the due post published
        assert out[0]["post_id"] == due.id
        assert out[0]["status_id"] == "m-1"

    def test_publish_nothing_due(self, db, capsys):
        _add_account(db)
        _run("ops", "publish", "--json")
        assert json.loads(capsys.readouterr().out) == []

    def test_metrics_refresh_runs(self, db, capsys):
        with patch("app.scheduler.update_metrics", new=AsyncMock()):
            _run("ops", "metrics-refresh", "--json")
        assert json.loads(capsys.readouterr().out) == {"ok": True}


# ── Schema (agent tool manifest) ─────────────────────────────────────────────

class TestSchema:
    def test_schema_lists_all_commands_with_metadata(self, db, capsys):
        _run("schema")
        schema = json.loads(capsys.readouterr().out)
        assert schema["tool"] == "tootflow"
        assert schema["run_via"] == ["uv", "run", "tootflow"]
        names = {c["name"] for c in schema["commands"]}
        expected = {"accounts_add", "accounts_list", "accounts_verify", "accounts_remove",
                    "campaigns_create", "campaigns_list", "campaigns_get",
                    "campaigns_archive", "campaigns_remove",
                    "posts_add", "posts_import", "posts_list", "posts_get",
                    "posts_edit", "posts_move", "posts_remove", "posts_send",
                    "posts_metrics", "ops_publish", "ops_metrics-refresh"}
        assert expected <= names

    def test_schema_parameters_are_typed_and_flagged(self, db, capsys):
        _run("schema")
        schema = json.loads(capsys.readouterr().out)
        by_name = {c["name"]: c for c in schema["commands"]}

        add = by_name["accounts_add"]
        instance = next(p for p in add["parameters"] if p["name"] == "--instance")
        assert instance["required"] is True
        assert instance["type"] == "string"

        create = by_name["campaigns_create"]
        template = next(p for p in create["parameters"] if p["name"] == "--template")
        assert template["type"] == "string"
        assert "knowledge_sharing" in template["choices"]
        assert template["required"] is False
        assert template["default"] == "knowledge_sharing"

        rm = by_name["campaigns_remove"]
        yes = next(p for p in rm["parameters"] if p["name"] == "--yes")
        assert yes["type"] == "boolean" and yes["flag"] is True

        getp = by_name["posts_get"]
        pos = next(p for p in getp["parameters"] if p["name"] == "post_id")
        assert pos["type"] == "integer" and pos["required"] is True

    def test_schema_descriptions_present(self, db, capsys):
        _run("schema")
        schema = json.loads(capsys.readouterr().out)
        for cmd in schema["commands"]:
            assert cmd["description"], f"{cmd['name']} has no description"
            assert cmd["invocation"][0] == "tootflow"
