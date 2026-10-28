"""
Migration: remove the payments system (SaaS → open source transition).

Drops the `payment` table and the `campaign.payment_status` column.
All monetization gates were removed from the code; campaigns no longer
track payment state.

Run against local SQLite (dev):
    python -m app.migrations.drop_payments

Run against remote prod PostgreSQL via .env.migrate:
    python -m app.migrations.drop_payments --prod

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
    """Drop payment table and campaign.payment_status. Idempotent."""
    if _table_exists(session, "payment"):
        session.exec(text("DROP TABLE payment"))
        session.commit()
        logger.info("Dropped table payment")
    else:
        logger.info("Table payment does not exist — skipping")

    if _column_exists(session, "campaign", "payment_status"):
        try:
            session.exec(text("ALTER TABLE campaign DROP COLUMN payment_status"))
            session.commit()
            logger.info("Dropped campaign.payment_status")
        except Exception as e:
            session.rollback()
            # SQLite < 3.35 cannot DROP COLUMN — needs a table rebuild.
            logger.error(
                "Could not drop campaign.payment_status (%s). "
                "Upgrade SQLite >= 3.35 or rebuild the campaign table manually.", e
            )
            raise
    else:
        logger.info("campaign.payment_status does not exist — skipping")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    prod = "--prod" in sys.argv
    target = "prod PostgreSQL (via .env.migrate)" if prod else "local SQLite"
    print(f"Running migration against {target}...")

    engine = _make_engine(prod)
    with Session(engine) as session:
        run_ddl(session)
    print("Migration complete.")
