"""
Migration: remove X (Twitter) support.

Drops the `xaccount` table, `campaign.x_account_id`, and `post.x_status_id`.

Run against local SQLite (dev):
    python -m app.migrations.drop_x_support

Run against remote prod PostgreSQL via .env.migrate:
    python -m app.migrations.drop_x_support --prod

.env.migrate must contain:
    POSTGRES_URL=postgresql://user:pass@host:port/db

Safe to re-run — fully idempotent.
"""
import logging
import sys
from sqlmodel import Session, text

from app.migrations.add_account_ids import _make_engine

logger = logging.getLogger(__name__)


def _table_exists(session: Session, table: str) -> bool:
    try:
        session.exec(text(f"SELECT 1 FROM {table} LIMIT 0"))
        session.rollback()
        return True
    except Exception:
        session.rollback()
        return False


def _column_exists(session: Session, table: str, column: str) -> bool:
    try:
        result = session.exec(text(f"SELECT {column} FROM {table} LIMIT 0"))
        result.close()
        return True
    except Exception:
        session.rollback()
        return False


def run_ddl(session: Session) -> None:
    if _table_exists(session, "xaccount"):
        session.exec(text("DROP TABLE xaccount"))
        session.commit()
        logger.info("Dropped table xaccount")
    else:
        logger.info("Table xaccount does not exist — skipping")

    for table, column in (("campaign", "x_account_id"), ("post", "x_status_id")):
        if _column_exists(session, table, column):
            try:
                session.exec(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
                session.commit()
                logger.info(f"Dropped {table}.{column}")
            except Exception as e:
                session.rollback()
                logger.error(
                    "Could not drop %s.%s (%s). Upgrade SQLite >= 3.35 or "
                    "rebuild the table manually.", table, column, e
                )
                raise
        else:
            logger.info(f"{table}.{column} does not exist — skipping")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    prod = "--prod" in sys.argv
    target = "prod PostgreSQL (via .env.migrate)" if prod else "local SQLite"
    print(f"Running migration against {target}...")

    engine = _make_engine(prod)
    with Session(engine) as session:
        run_ddl(session)
    print("Migration complete.")
