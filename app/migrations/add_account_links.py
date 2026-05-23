"""
Migration: create accountlink table

Run against local SQLite (dev):
    python -m app.migrations.add_account_links

Run against remote prod PostgreSQL via .env.migrate:
    python -m app.migrations.add_account_links --prod

Safe to re-run — fully idempotent.
"""
import logging
import sys

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
        from sqlalchemy import create_engine
        env = _load_env()
        url = env.get("POSTGRES_URL")
        if not url:
            print("Error: POSTGRES_URL not set in .env.migrate")
            sys.exit(1)
        connect_args = {"sslmode": "require"} if "postgresql" in url else {}
        return create_engine(url, connect_args=connect_args)
    else:
        from app.database import engine
        return engine


def run_ddl(session) -> None:
    from app.models import AccountLink, SQLModel
    SQLModel.metadata.tables["accountlink"].create(session.bind, checkfirst=True)
    logger.info("accountlink table ensured")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    from sqlmodel import Session
    prod = "--prod" in sys.argv
    target = "prod PostgreSQL (via .env.migrate)" if prod else "local SQLite"
    print(f"Running migration against {target}...")
    engine = _make_engine(prod)
    with Session(engine) as session:
        run_ddl(session)
    print("Migration complete.")
