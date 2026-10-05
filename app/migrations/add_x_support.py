"""
Migration: add x_status_id to Post (X.com publishing support).

Run against local SQLite (dev):
    python -m app.migrations.add_x_support

Run against remote prod PostgreSQL via .env.migrate:
    python -m app.migrations.add_x_support --prod

.env.migrate must contain:
    POSTGRES_URL=postgresql://user:pass@host:port/db

Safe to re-run — fully idempotent.
"""
import logging
import sys
from sqlmodel import Session, text

from app.migrations.add_account_ids import _make_engine

logger = logging.getLogger(__name__)


def _column_exists(session: Session, table: str, column: str) -> bool:
    try:
        result = session.exec(text(f"SELECT {column} FROM {table} LIMIT 0"))
        result.close()
        return True
    except Exception:
        session.rollback()
        return False


def run_ddl(session: Session) -> None:
    """Add Post.x_status_id. Idempotent."""
    if not _column_exists(session, "post", "x_status_id"):
        session.exec(text(
            "ALTER TABLE post ADD COLUMN x_status_id VARCHAR"
        ))
        session.commit()
        logger.info("Added post.x_status_id")
    else:
        logger.info("post.x_status_id already exists — skipping")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    prod = "--prod" in sys.argv
    target = "prod PostgreSQL (via .env.migrate)" if prod else "local SQLite"
    print(f"Running migration against {target}...")

    engine = _make_engine(prod)
    with Session(engine) as session:
        run_ddl(session)
    print("Migration complete.")
