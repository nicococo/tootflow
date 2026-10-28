# Contributing to TootFlow

Thanks for helping out! This guide gets you from clone to green tests.

## Dev setup

**Requirements:** [uv](https://docs.astral.sh/uv/getting-started/installation/) — it
provides Python **3.12** automatically (3.13+ breaks the pinned `sqlmodel`; the
`.python-version` file pins it for you) and manages all dependencies + the lockfile.

```bash
git clone https://github.com/nicococo/tootflow.git
cd tootflow
uv sync                     # creates .venv, installs locked deps
cp .env.example .env
uv run start.py             # boots the app; SQLite DB is created on first startup
```

> On macOS, `psycopg2-binary` may need `brew install postgresql` if no prebuilt wheel
> exists for your platform. It's only needed for PostgreSQL — SQLite development works
> without it.

## Running the app

```bash
uv run start.py             # http://localhost:8000
```

The scheduler starts inside the app process. Log in with any Mastodon account —
dev instances register their own OAuth app per instance automatically.

## Running tests

```bash
uv run pytest tests/ -v
```

- 150+ tests, all offline (in-memory SQLite, mocked HTTP/AI). No network needed.
- The suite is the contract: **every PR must keep it green.** New features need tests.
- X publishing, OAuth flows, and the scheduler are covered in `tests/test_x_support.py`
  and `tests/test_multi_account.py` — copy their patterns (mock at the service boundary,
  use `asyncio.run()` for async tests, no `pytest-asyncio` dependency).

## Project layout

```
app/
├── main.py            # FastAPI app, middleware, lifespan
├── config.py          # pydantic-settings (env-driven)
├── models.py          # SQLModel tables
├── scheduler.py       # APScheduler: publishing + metrics jobs
├── crypto.py          # Fernet encryption for tokens/keys at rest
├── routers/           # auth (OAuth), campaigns, posts, metrics, account, admin
├── services/          # mastodon_service, x_service, ai_service
├── migrations/        # idempotent DDL scripts (run manually)
└── templates/         # Jinja2, brutalist design
```

## Conventions

- **Tokens and keys are encrypted at rest** (`app/crypto.py`) and are never persisted
  decrypted — use transient copies when passing them around. Regression tests guard this;
  keep it that way.
- **Minimalism over abstraction** — no frameworks-of-frameworks, no plugin systems of our
  own. Small, readable, boring diffs.
- **Security-first defaults**: CSRF on all mutating routes, rate limits on sensitive
  endpoints, SSRF validation on user-supplied URLs.
- Server-rendered Jinja2 + vanilla JS in the templates; no frontend build step.

## Migrations

Schema changes need an idempotent script in `app/migrations/` (see existing scripts for
the pattern — they must be safe to re-run and support both SQLite and PostgreSQL).
New *tables* are created automatically by `create_all`; column changes need a script.

## Submitting

1. Branch off `main`, keep diffs focused.
2. `python -m pytest tests/` green.
3. Open a PR — describe *what* and *why*, not just *how*.
