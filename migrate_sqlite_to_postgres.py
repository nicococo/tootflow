#!/usr/bin/env python3
"""
Migrate data from local SQLite to remote PostgreSQL.
Preserves all IDs. Safe to re-run — clears target tables first.

Usage:
    python migrate_sqlite_to_postgres.py
"""

import os
import sys
from sqlalchemy import create_engine, text

def _load_env(path=".env.migrate"):
    """Minimal .env loader — no extra dependencies needed."""
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        print(f"Error: {path} not found. Create it with SQLITE_URL and POSTGRES_URL.")
        sys.exit(1)
    return env

_env = _load_env()
SQLITE_URL  = _env.get("SQLITE_URL",  "sqlite:///./tootflow.db")
POSTGRES_URL = _env.get("POSTGRES_URL")
if not POSTGRES_URL:
    print("Error: POSTGRES_URL not set in .env.migrate")
    sys.exit(1)

# Migration order respects foreign key dependencies
TABLES = [
    "user",
    "mastodonaccount",
    "campaign",
    "post",
    "metrics",
    "followersnapshot",
    "pageview",
    "siteconfig",
    "payment",
]


# Boolean columns per table (SQLite stores as 0/1, PostgreSQL needs True/False)
BOOL_COLUMNS = {
    "campaign": ["enabled"],
}

def _bool_columns(table):
    return BOOL_COLUMNS.get(table, [])


def get_columns(conn, table):
    result = conn.execute(text(f"SELECT * FROM \"{table}\" LIMIT 0"))
    return list(result.keys())


def migrate():
    print("Connecting to SQLite...")
    src = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})

    print("Connecting to PostgreSQL...")
    dst = create_engine(POSTGRES_URL)

    # Ensure all tables exist in PostgreSQL (create from models)
    print("Creating tables in PostgreSQL if not exist...")
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from app.models import (
        User, MastodonAccount, Campaign, Post, Metrics,
        FollowerSnapshot, PageView, SiteConfig, Payment
    )
    from sqlmodel import SQLModel
    SQLModel.metadata.create_all(dst)

    with src.connect() as src_conn, dst.connect() as dst_conn:
        for table in TABLES:
            # Check table exists in source
            try:
                rows = src_conn.execute(text(f'SELECT * FROM "{table}"')).fetchall()
            except Exception as e:
                print(f"  SKIP {table}: {e}")
                continue

            if not rows:
                print(f"  {table}: empty, skipping")
                continue

            columns = get_columns(src_conn, table)
            print(f"  {table}: {len(rows)} rows → ", end="", flush=True)

            # Disable FK checks, truncate, re-enable
            dst_conn.execute(text("SET session_replication_role = replica"))
            dst_conn.execute(text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE'))

            # Insert rows
            placeholders = ", ".join(f":{c}" for c in columns)
            col_list = ", ".join(f'"{c}"' for c in columns)
            insert_sql = text(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})')

            # SQLite stores booleans as 0/1 integers; PostgreSQL needs True/False
            bool_cols = _bool_columns(table)
            data = []
            for row in rows:
                d = dict(zip(columns, row))
                for col in bool_cols:
                    if col in d and d[col] is not None:
                        d[col] = bool(d[col])
                data.append(d)
            dst_conn.execute(insert_sql, data)

            # Re-enable FK checks
            dst_conn.execute(text("SET session_replication_role = DEFAULT"))
            dst_conn.commit()

            print(f"done")

        # Reset sequences so next INSERT gets correct next ID
        print("Resetting PostgreSQL sequences...")
        seq_tables = [t for t in TABLES if t != "siteconfig"]  # siteconfig has no int PK
        for table in seq_tables:
            try:
                dst_conn.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('\"{table}\"', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM \"{table}\"), 0) + 1, false)"
                ))
                dst_conn.commit()
            except Exception as e:
                print(f"  Could not reset sequence for {table}: {e}")

    print("\nMigration complete.")


if __name__ == "__main__":
    migrate()
