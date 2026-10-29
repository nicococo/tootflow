<p align="center">
  <img src="banner_1600x300.png" alt="TootFlow" width="100%">
</p>

<h1 align="center">TootFlow</h1>

<p align="center">
  <strong>Plan, draft, and automate Mastodon campaigns — free and open source.</strong><br>
  AI-assisted post scheduling with full editorial control. Web app plus an agent-friendly CLI.
</p>

<p align="center">
  <a href="#-quickstart">Quickstart</a> ·
  <a href="#-built-for-agents">Agents</a> ·
  <a href="#-features">Features</a> ·
  <a href="#-configuration">Configuration</a> ·
  <a href="#-self-hosting">Self-hosting</a> ·
  <a href="#-contributing">Contributing</a>
</p>

---

TootFlow turns a topic into a full social media campaign: you describe what you want to
share, it drafts the posts, spaces them out over your chosen duration, and publishes
automatically — while you get to exist offline sometimes.

Built for [OSS maintainers](#), researchers, and solopreneurs who ship things but hate
remembering to post about them — and for the agents that help them do it.

## ✨ Features

- **Campaigns** — pick a template (product launch, open-source release, dev tips, …), a tone,
  a language, a cadence. TootFlow builds the schedule.
- **AI post generation** — bring your own key from any OpenAI-compatible provider
  (OpenAI, OpenRouter, a local Ollama, …). Every draft is fully editable before it goes anywhere.
- **Automated publishing** — a background scheduler publishes due posts every minute,
  with sensible retry semantics (transient errors retry, permanent ones fail visibly).
- **Engagement metrics** — favourites, boosts, replies, and follower snapshots, tracked
  per post with weekly charts.
- **Export Studio** — repurpose a finished campaign into a newsletter digest, LinkedIn
  article, or Reddit post.
- **Multi-account & linking** — run campaigns from several Mastodon accounts and link
  them to see everything combined.
- **Security by default** — OAuth-only login, encrypted-at-rest tokens (Mastodon, AI keys),
  CSRF protection, rate limiting, GDPR data export and hard-delete.

## 🚀 Quickstart

**Docker (recommended):**

```bash
git clone https://github.com/nicococo/tootflow.git
cd tootflow
cp .env.example .env        # set SECRET_KEY and APP_BASE_URL
docker compose up -d --build
```

Open `http://localhost:8000`, log in with your Mastodon account, done.

**Local (via [uv](https://docs.astral.sh/uv/)):**

```bash
git clone https://github.com/nicococo/tootflow.git
cd tootflow
cp .env.example .env        # set SECRET_KEY
uv run start.py             # installs deps, creates the SQLite DB, starts the app
```

> Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) — it manages
> Python 3.12 automatically (`.python-version`), so you don't need Python installed.

**First AI post:** log in → Account → *AI Model* → paste an API key from any
OpenAI-compatible provider (or set `OPENAI_API_KEY` in `.env` for an instance-wide default).

## 🤖 Built for agents

Prefer letting your coding agent do the drafting? TootFlow ships a single-user CLI
designed as a **first-class agent tool** — the agent creates, TootFlow operates:

```bash
uv run tootflow accounts add --instance https://mastodon.social   # one-time OAuth
uv run tootflow campaigns create --name "v2.0 launch" --posts-per-week 3
echo '[{"content": "Shipping v2.0 today 🎉"}]' | \
    uv run tootflow posts import --campaign 1 --json-file - --skip-duplicate
uv run tootflow ops publish        # publishes everything due — trigger it anytime
```

The division of labor: **the agent writes the words, TootFlow owns the operations** —
tokens, scheduling, publishing, retries, metrics. Deterministic commands instead of
fragile browser automation.

Why it works well inside OpenCode, Claude Code, Codex, or any harness:

- **Self-describing** — `uv run tootflow schema` prints a JSON manifest of every
  command, parameter, choice, and convention. The agent bootstraps itself from one
  command; it never reads your docs.
- **Machine-first output** — stable `--json` schemas on every read command, meaningful
  exit codes (`0`/`1`/`2`), errors on stderr. Branch on them, never scrape them.
- **Safe by default** — no interactive prompts, destructive commands need explicit
  `--yes`, `--skip-duplicate` prevents agent retries from double-posting.
- **Yours to trigger** — publishing is a one-shot command: run it manually, from an
  in-harness loop, or from any trigger you like. No daemon, no vendor lock-in.

Full reference in [docs/cli.md](docs/cli.md), the recommended agent loop in
[docs/agent-harness.md](docs/agent-harness.md).

## 🧩 Features at a glance

| | |
|---|---|
| 📅 **Scheduling** | Campaign duration + posts/week → automatic schedule, editable per post |
| 🤖 **AI drafting** | 10 templates, 12 tones, per-post refine — your model, your key, your costs |
| 🐘 **Mastodon** | Full support incl. polls; multi-instance, multi-account |
| 📊 **Metrics** | Per-post engagement + follower growth, fetched daily |
| 📤 **Export Studio** | Newsletter digest, LinkedIn article, Reddit post — from any campaign |
| 🖥️ **CLI** | Single-user CLI for agent harnesses — agents draft, TootFlow schedules & publishes |
| 🔐 **Privacy** | No email, no passwords, no tracking. GDPR export & delete built in |

## ⚙️ Configuration

All configuration is environment-based — see [`.env.example`](.env.example) for the full
reference. The essentials:

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | — | JWT signing — **set a strong random string** |
| `DATABASE_URL` | `sqlite:///./tootflow.db` | SQLite (dev) or PostgreSQL (prod) |
| `APP_BASE_URL` | `http://localhost:8000` | Public URL, used for OAuth callbacks |
| `OPENAI_API_KEY` | — | Instance-wide AI fallback (users' own keys win) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Instance-wide default model |
| `ADMIN_SECRET` | — | Enables `/admin`; leave empty in single-user setups |

## 🏠 Self-hosting

Everything is designed for small, single-box deployments — see [DEPLOY.md](DEPLOY.md) for
the full guide (Docker Compose, PostgreSQL, migrations, upgrades). In short:

- **SQLite** is fine for personal use; **PostgreSQL** for anything longer-lived
- One uvicorn worker — the scheduler runs inside the app process
- Migrations are idempotent scripts in `app/migrations/`, run manually before deploys

## 🤝 Contributing

PRs are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and conventions.
The test suite (150+ tests, no network needed) is the contract — keep it green.

## 📄 License

[MIT](LICENSE) — do whatever you like, attribution appreciated.
