# TootFlow — Deployment Process

## Overview

Deployments follow a 5-stage gate: **tests → local testing → migrations → git push → mlox deploy**.

Each stage is a hard stop — don't proceed if a stage fails.

---

## Stage 1 — Unit Tests

```bash
python -m pytest tests/ -m "not integration" -v
```

All tests must pass. Any failure stops the process.

To also run live AI integration tests (requires `OPENAI_API_KEY`):

```bash
python -m pytest tests/ -v
```

---

## Stage 2 — Local User Testing

```bash
./start.sh
```

Runs the app locally against SQLite using your local `.env`.

Manually test the changed feature end-to-end in the browser.

> **Note:** SQLite and PostgreSQL behave differently in subtle ways (boolean
> casting, FK enforcement, some query syntax). Changes that touch the DB
> layer should be tested with extra care — a bug can pass SQLite and surface
> only on prod.

---

## Stage 3 — Migrations (if `models.py` changed)

If you added or changed any SQLModel fields, a migration script is required.
`create_db_and_tables()` only creates *new* tables — it never alters existing ones.

Migration scripts live in `app/migrations/`. Each script is idempotent (safe to re-run).

**Run locally first:**
```bash
python -m app.migrations.<script_name>
```

**Then run against prod PostgreSQL:**
```bash
python -m app.migrations.<script_name> --prod
```

The `--prod` flag reads `POSTGRES_URL` from `.env.migrate` (gitignored).

> **Order matters:** always migrate *before* deploying new code. New columns
> are nullable so the running app continues to work against the updated schema
> during the deploy window.

---

## Stage 4 — Git Push

```bash
git add <files>
git commit -m "your message"
git push
```

---

## Stage 5 — Deploy via mlox

TootFlow is deployed using [mlox](https://mlox.org)'s `RepoDeployDockerService`.

On trigger, mlox:
1. Pulls the latest git commit on the server
2. Re-reads the `docker-compose.yml` for updated env tokens
3. Runs `docker compose up --build -d app`
4. The app health check at `/health` confirms a clean start

### When `requirements.txt` changed

mlox's standard deploy uses the Docker layer cache. If you changed
`requirements.txt`, trigger a fresh rebuild to ensure new packages are picked up:

```bash
# On the server, or via mlox shell access:
docker compose build --no-cache app && docker compose up -d app
```

### Verifying the deploy

After mlox reports success:

```bash
# Watch logs for 2 minutes
docker compose logs -f app

# Check admin dashboard
https://tootflow.com/admin
```

Smoke test the changed feature on the live site.

### If the deploy fails

The health check will time out and mlox will report failure. To rollback:

```bash
# Find the previous working commit
git log --oneline -5

# On the server:
git checkout <previous-commit>
docker compose up --build -d app
```

---

## Quick Reference

| Stage | Command | Stop if |
|-------|---------|---------|
| 1. Tests | `python -m pytest tests/ -m "not integration" -v` | Any test fails |
| 2. Local testing | `./start.sh` | Feature broken locally |
| 3. Migrations | `python -m app.migrations.<name> --prod` | Migration errors |
| 4. Git push | `git push` | Push rejected |
| 5. mlox deploy | via mlox RepoDeployDockerService | Health check fails |

---

## Environment Files

| File | Purpose | Committed? |
|------|---------|------------|
| `.env` | Local dev config | No |
| `.env.migrate` | Prod DB credentials for migration scripts | No |
| `docker-compose.yml` | Service definition, env token discovery for mlox | Yes |
