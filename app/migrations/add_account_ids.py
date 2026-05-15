"""
Migration: add mastodon_account_id and x_account_id to Campaign,
           and create the XAccount table.

Run against local SQLite (dev):
    python -m app.migrations.add_account_ids

Run against remote prod PostgreSQL via .env.migrate:
    python -m app.migrations.add_account_ids --prod

.env.migrate must contain:
    POSTGRES_URL=postgresql://user:pass@host:port/db

Safe to re-run — fully idempotent.
"""
import logging
import os
import sys
from sqlalchemy import create_engine
from sqlmodel import Session, select, text

logger = logging.getLogger(__name__)


def _load_env(path=".env.migrate") -> dict:
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        print(f"Error: {path} not found.")
        sys.exit(1)
    return env


def _make_engine(prod: bool):
    if prod:
        env = _load_env()
        url = env.get("POSTGRES_URL")
        if not url:
            print("Error: POSTGRES_URL not set in .env.migrate")
            sys.exit(1)
        # Handle self-signed cert on prod PostgreSQL
        connect_args = {"sslmode": "require"} if "postgresql" in url else {}
        return create_engine(url, connect_args=connect_args)
    else:
        from app.database import engine
        return engine


def _column_exists(session: Session, table: str, column: str) -> bool:
    """Check if a column exists — works for both SQLite and PostgreSQL."""
    try:
        result = session.exec(text(f"SELECT {column} FROM {table} LIMIT 0"))
        result.close()
        return True
    except Exception:
        session.rollback()
        return False


def run_ddl(session: Session) -> None:
    """Add new columns to campaign table. Idempotent."""
    if not _column_exists(session, "campaign", "mastodon_account_id"):
        session.exec(text(
            "ALTER TABLE campaign ADD COLUMN mastodon_account_id INTEGER "
            "REFERENCES mastodonaccount(id)"
        ))
        session.commit()
        logger.info("Added campaign.mastodon_account_id")
    else:
        logger.info("campaign.mastodon_account_id already exists — skipping")

    if not _column_exists(session, "campaign", "x_account_id"):
        session.exec(text(
            "ALTER TABLE campaign ADD COLUMN x_account_id INTEGER"
        ))
        session.commit()
        logger.info("Added campaign.x_account_id")
    else:
        logger.info("campaign.x_account_id already exists — skipping")

    # Create xaccount table if it doesn't exist yet
    # SQLModel's create_all handles this on app start, but we need it here
    # for the FK reference above to work on prod before app restarts.
    from app.models import XAccount, SQLModel
    SQLModel.metadata.tables["xaccount"].create(session.bind, checkfirst=True)
    logger.info("xaccount table ensured")


def backfill_mastodon_account_ids(session: Session) -> None:
    """Set mastodon_account_id on campaigns that don't have one yet. Idempotent."""
    from app.models import Campaign, MastodonAccount

    campaigns = session.exec(
        select(Campaign).where(Campaign.mastodon_account_id == None)  # noqa: E711
    ).all()

    updated = 0
    for campaign in campaigns:
        account = session.exec(
            select(MastodonAccount).where(MastodonAccount.user_id == campaign.user_id)
        ).first()
        if account:
            campaign.mastodon_account_id = account.id
            session.add(campaign)
            updated += 1

    session.commit()
    logger.info(f"Backfilled mastodon_account_id for {updated} campaigns")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    prod = "--prod" in sys.argv
    target = "prod PostgreSQL (via .env.migrate)" if prod else "local SQLite"
    print(f"Running migration against {target}...")

    engine = _make_engine(prod)
    with Session(engine) as session:
        run_ddl(session)
        backfill_mastodon_account_ids(session)
    print("Migration complete.")
