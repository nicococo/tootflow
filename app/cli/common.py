"""Shared CLI plumbing: DB access, user/account resolution, output, exit codes."""
import json
import sys
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlmodel import Session, select

from app.config import get_settings
from app.crypto import encrypt_token
from app.database import engine
from app.models import Campaign, MastodonAccount, User

settings = get_settings()

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def load_env() -> None:
    """Load .env from the working directory (no-op if missing)."""
    load_dotenv()


@contextmanager
def db_session():
    """Session bound to the app's engine (respects DATABASE_URL)."""
    with Session(engine) as session:
        yield session


def resolve_user(session: Session, user_id: int | None = None) -> User:
    """Resolve THE user for single-user operation.

    - 0 users  → create one (fresh install: `accounts add` bootstraps)
    - 1 user   → that user
    - N users  → require explicit --user-id
    """
    users = session.exec(select(User)).all()
    if user_id is not None:
        user = session.get(User, user_id)
        if not user:
            fail(f"No user with id {user_id}", EXIT_ERROR)
        return user
    if len(users) == 0:
        user = User()
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    if len(users) == 1:
        return users[0]
    fail(
        f"Database has {len(users)} users — pass --user-id "
        f"({', '.join(str(u.id) for u in users[:5])}…)",
        EXIT_ERROR,
    )


def resolve_account(session: Session, user: User, account: str | None = None) -> MastodonAccount:
    """Resolve the posting account: --account '@user@instance' (or id), else the only/first one."""
    accounts = session.exec(
        select(MastodonAccount).where(MastodonAccount.user_id == user.id).order_by(MastodonAccount.id)
    ).all()
    if not accounts:
        fail("No Mastodon account connected. Run: tootflow accounts add --instance https://mastodon.social", EXIT_ERROR)
    if account is None:
        if len(accounts) > 1:
            fail(
                "Multiple accounts connected — pass --account (@user@instance or id): "
                + ", ".join(f"{a.id}={_handle(a)}" for a in accounts),
                EXIT_ERROR,
            )
        return accounts[0]
    for a in accounts:
        if account.isdigit() and a.id == int(account):
            return a
        if account.lstrip("@") == _handle(a).lstrip("@"):
            return a
    fail(f"No such account: {account}", EXIT_ERROR)


def resolve_campaign(session: Session, user: User, campaign_id: int) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        fail(f"No campaign {campaign_id} for this user", EXIT_ERROR)
    return campaign


def _handle(acc: MastodonAccount) -> str:
    host = acc.instance_url.replace("https://", "").replace("http://", "").rstrip("/")
    return f"{acc.username}@{host}"


def to_dict(obj) -> dict:
    """SQLModel/pydantic → JSON-safe dict (datetimes ISO-formatted)."""
    data = obj.model_dump(mode="json")
    return data


def out_json(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


def out(payload: str = "") -> None:
    print(payload)


def fail(message: str, code: int = EXIT_ERROR) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def require_yes(yes: bool, what: str) -> None:
    if not yes:
        fail(f"refusing to {what} without --yes", EXIT_USAGE)


def store_token(session: Session, user: User, instance_url: str, username: str, token: str) -> MastodonAccount:
    """Upsert a Mastodon account with an encrypted token."""
    existing = session.exec(
        select(MastodonAccount).where(
            MastodonAccount.user_id == user.id,
            MastodonAccount.instance_url == instance_url,
            MastodonAccount.username == username,
        )
    ).first()
    if existing:
        existing.access_token = encrypt_token(token)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing
    acc = MastodonAccount(
        user_id=user.id,
        instance_url=instance_url,
        access_token=encrypt_token(token),
        username=username,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc
