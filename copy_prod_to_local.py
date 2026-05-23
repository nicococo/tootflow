#!/usr/bin/env python3
"""
Copy production PostgreSQL data to the local SQLite database.

Reads credentials from .env.migrate:
    POSTGRES_URL=postgresql://user:pass@host:port/db
    SQLITE_URL=sqlite:///./tootflow.db   (optional, defaults to tootflow.db)

DESTRUCTIVE on local SQLite — all existing rows are deleted before insert.

Usage:
    python copy_prod_to_local.py
"""

import sys
from datetime import datetime, timezone
from sqlalchemy import create_engine, text


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
        print("Create it with at minimum: POSTGRES_URL=postgresql://...")
        sys.exit(1)
    return env


# FK-safe insert order (children after parents)
TABLES = [
    "user",
    "mastodonaccount",
    "xaccount",
    "accountlink",
    "campaign",
    "post",
    "metrics",
    "followersnapshot",
    "pageview",
    "siteconfig",
    "payment",
    "loginevent",
]


def _strip_tz(value):
    """SQLite doesn't handle timezone-aware datetimes — strip tzinfo if present."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _get_columns(conn, table: str) -> list[str]:
    result = conn.execute(text(f'SELECT * FROM "{table}" LIMIT 0'))
    return list(result.keys())


def copy():
    env = _load_env()
    postgres_url = env.get("POSTGRES_URL")
    sqlite_url   = env.get("SQLITE_URL", "sqlite:///./tootflow.db")

    if not postgres_url:
        print("Error: POSTGRES_URL not set in .env.migrate")
        sys.exit(1)

    print("This will DELETE all local SQLite data and replace it with prod data.")
    answer = input("Continue? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)

    print(f"\nSource : {postgres_url[:40]}...")
    print(f"Target : {sqlite_url}\n")

    print("Connecting to PostgreSQL...")
    connect_args = {"sslmode": "require"} if "postgresql" in postgres_url else {}
    src = create_engine(postgres_url, connect_args=connect_args)

    print("Connecting to SQLite...")
    dst = create_engine(sqlite_url, connect_args={"check_same_thread": False})

    # Ensure all tables exist in SQLite
    print("Ensuring local schema is up to date...")
    from app.models import SQLModel  # noqa: F401 — import triggers model registration
    import app.models  # noqa: F401
    SQLModel.metadata.create_all(dst)

    with src.connect() as src_conn, dst.connect() as dst_conn:
        # Disable FK enforcement in SQLite for the duration of the copy
        dst_conn.execute(text("PRAGMA foreign_keys = OFF"))

        for table in TABLES:
            try:
                rows = src_conn.execute(text(f'SELECT * FROM "{table}"')).fetchall()
            except Exception as e:
                print(f"  SKIP {table}: {e}")
                continue

            # Clear local table
            dst_conn.execute(text(f'DELETE FROM "{table}"'))

            if not rows:
                print(f"  {table}: empty")
                dst_conn.commit()
                continue

            columns = _get_columns(src_conn, table)
            placeholders = ", ".join(f":{c}" for c in columns)
            col_list     = ", ".join(f'"{c}"' for c in columns)
            insert_sql   = text(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})')

            data = []
            for row in rows:
                d = {col: _strip_tz(val) for col, val in zip(columns, row)}
                data.append(d)

            dst_conn.execute(insert_sql, data)
            dst_conn.commit()
            print(f"  {table}: {len(rows)} rows copied")

        dst_conn.execute(text("PRAGMA foreign_keys = ON"))

    print("\nCopy complete. Local SQLite now mirrors production.")


if __name__ == "__main__":
    copy()
