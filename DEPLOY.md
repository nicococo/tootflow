# Self-Hosting TootFlow

TootFlow is a small, single-service app designed for single-box deployments.
This guide covers Docker Compose (recommended) and manual setup.

## Docker Compose (recommended)

```bash
git clone https://github.com/nicococo/tootflow.git
cd tootflow
cp .env.example .env
# Edit .env: set SECRET_KEY (long random string) and APP_BASE_URL
docker compose up -d --build
```

Open `http://localhost:8000` (or your `APP_BASE_URL`) and log in with your
Mastodon account. Done.

### PostgreSQL (optional)

SQLite is perfectly fine for personal use. For a longer-lived instance:

```bash
docker compose --profile postgres up -d --build
```

Then set in `.env`:

```
DATABASE_URL=postgresql://tootflow:tootflow@db:5432/tootflow
```

Change the password. Run the app behind a reverse proxy with TLS (Caddy, Traefik,
nginx) — production cookies are `Secure`-only and need HTTPS.

## Configuration

All configuration is environment-based — see [`.env.example`](.env.example) for the
full reference. Highlights:

| Variable | Notes |
|---|---|
| `SECRET_KEY` | **Required.** Long random string. Encrypts stored tokens; changing it invalidates them. |
| `APP_BASE_URL` | Must match the public URL — used for OAuth callbacks. |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Optional instance-wide AI fallback. Users' own keys (Account page) take precedence. |
| `X_API_KEY`, `X_API_SECRET` | X OAuth app credentials. Create an app at the X Developer Portal with OAuth 1.0a (Read+Write) and callback `<APP_BASE_URL>/x/callback`. Empty = X support disabled. |
| `ADMIN_SECRET` | Enables `/admin?secret=<value>` — a read-mostly analytics dashboard. Leave empty to disable. |
| `MAX_FREE_CAMPAIGNS` | Per-user campaign cap (anti-abuse for open instances). |

## Upgrades

```bash
git pull
docker compose up -d --build
```

## Manual setup (no Docker)

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) — it manages
Python 3.12 and all dependencies:

```bash
cp .env.example .env        # set SECRET_KEY and APP_BASE_URL
uv run start.py             # installs deps, creates the SQLite DB, serves on :8000
```

## Migrations

Schema changes ship as **idempotent scripts** in `app/migrations/` — they are *not*
run automatically. After pulling an upgrade, check the release notes; if a migration
is mentioned, run it once:

```bash
docker compose exec app uv run --no-sync python -m app.migrations.<script_name>
# or, without Docker:
uv run python -m app.migrations.<script_name>
```

They are safe to re-run.

## Operational notes

- **Single worker only.** The APScheduler (publishing every minute, metrics daily)
  runs inside the app process — multiple workers would duplicate it.
- **Backups**: for SQLite, stop the container and copy the volume
  (`docker run --rm -v tootflow_app-data:/data alpine tar czf - /data`).
- **Hosted-instance hygiene**: if you run a public instance, keep registration
  gated (`registration_open` is manageable via `/admin`), keep `MAX_FREE_CAMPAIGNS`
  sane, and read [SECURITY.md](SECURITY.md).

## Legal pages (Impressum / ToS / Privacy)

Optional. Set the `OPERATOR_*` variables to show the legal footer and pages —
relevant if you operate a public instance in the EU. Self-hosters can ignore them;
the footer hides itself when `OPERATOR_NAME` is empty.
