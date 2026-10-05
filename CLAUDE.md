# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**TootFlow** is a minimal SaaS web application for planning and running Mastodon and X (Twitter) campaigns. It's designed as a single-service indie SaaS with a campaign-based business model (pay-what-you-want, one-time per campaign, no subscriptions).

Core workflow: Users generate AI-powered posts, manually edit them, pay for campaign activation via Polar.sh, then posts publish automatically on schedule.

## Tech Stack

- **Backend**: Python, FastAPI, SQLModel/SQLAlchemy
- **Database**: SQLite (dev), PostgreSQL (prod)
- **Frontend**: Server-rendered HTML with Jinja2, minimal CSS
- **Background Jobs**: APScheduler (post publishing, metrics updates)
- **AI**: OpenAI API (or compatible)
- **Payments**: Polar.sh (pay-what-you-want, one-time checkout + webhooks)
- **External API**: Mastodon OAuth and API integration

## Architecture Principles

### Simplicity First
- Single deployable application (no microservices)
- Minimal feature set, no overengineering
- Avoid unnecessary abstractions
- Prioritize shipping speed and readability

### Design Philosophy (Brutalist)
The UI follows brutalist web design:
- System fonts only (Playfair Display headings, JetBrains Mono labels, Avenir Next body)
- High contrast (`--ink: #0d1822`, `--teal: #47b8b1`, `--orange: #d95f2a`)
- No gradients, shadows, or animations
- Visible borders and layout structure
- Function over decoration

## Core Data Model

- **User**: created_at
- **MastodonAccount**: instance_url, access_token (Fernet-encrypted), username
- **XAccount**: user_id, username, access_token, access_token_secret
- **AccountLink**: user_id, linked_user_id — joins two TootFlow users so their campaigns appear combined in the dashboard
- **Campaign**: name, description (JSON), template_type, tone, language, duration_days, posts_per_week, status, payment_status, enabled (pause/resume), mastodon_account_id, x_account_id
- **Post**: campaign_id, content (max 500 chars Mastodon / 280 chars X), scheduled_time, status, mastodon_status_id, x_status_id, poll_options (Mastodon only), poll_expires_in
- **Metrics**: post_id, favourites, reblogs, replies, poll_voters
- **Payment**: campaign_id, lemonsqueezy_order_id (used as generic order_id field), amount, currency, status
- **LoginEvent**: user_id, created_at — recorded on every successful Mastodon OAuth login
- **PageView**: visitor_hash, path, created_at — privacy-safe (hashed IP+date)
- **SiteConfig**: key/value — stores banner_text, registration_open
- **FollowerSnapshot**: user_id, count, recorded_at

## Account Linking

Users can link multiple TootFlow accounts (each backed by a different Mastodon account) via `AccountLink`. Linked accounts share dashboard visibility and full campaign management access.

**Links are direct-only — not transitive.** If A links to B and B links to C, A does NOT see C. This is deliberate: links are explicit actions, and transitivity would silently grant visibility to accounts the user never consciously connected. Do not implement graph traversal in `_get_linked_user_ids`. If A needs to see C, the user must link A→C directly.

- Linking flow: `POST /mastodon/link` → OAuth → callback creates `AccountLink`; new TootFlow user created if the Mastodon identity doesn't exist yet
- Unlinking: `POST /mastodon/unlink/{linked_user_id}`
- Migration: `python -m app.migrations.add_account_links [--prod]`

## Campaign States

Campaign progression: `draft` → `active` (posts added) → `running` (first post published) → `completed`

- `status` and `payment_status` are independent
- `enabled: bool` controls pause/resume independently of status
- Scheduler publishes for ALL `status in ("active","running") AND enabled=True` campaigns regardless of payment_status
- `payment_status == "paid"` gates: AI generation, metrics page, Export Studio only

## Key Workflows

### Payment Flow (Polar.sh)
1. User clicks "Activate" → `POST /campaigns/{id}/activate` → redirect to Polar checkout
2. Polar webhook `POST /webhooks/polar` → `order.paid` event → activate campaign
3. Fallback: campaign detail page detects `?customer_session_token=` query param + unpaid status → queries Polar orders API directly to activate
4. Event type detection uses `type(event).__name__` string check (not `isinstance`) for SDK version resilience

### Scheduler (publish_due_posts)
- Runs every 1 minute, `max_instances=1` (no overlapping runs)
- Posts ordered by `scheduled_time ASC` to prioritise oldest-due first
- Platform per campaign: `x_account_id` set → X publish path (`_publish_to_x`); else Mastodon path
- Decrypted tokens are used only on transient (non-session) account copies — never persisted
- Retry logic: `httpx.HTTPStatusError` 4xx → `status="failed"` (permanent); 5xx/429/network → stays `pending` (retried next run)

### Campaign Resume / Stale Post Rescheduling
When a paused campaign is resumed:
- All pending posts with `scheduled_time < now` shift forward by `ceil(days since earliest stale post)` whole days
- Time-of-day preserved (HH:MM:SS unchanged)
- Relative spacing between posts maintained
- Future/published/failed posts untouched

### Campaign Delete
Cascade order: Metrics → Posts → Payments → Campaign (explicit, no ORM cascade)

### Admin Dashboard
- `/admin` — tabbed: Overview / Business / Controls
- Overview: unique visitors, 30-day visitor trend (teal), 30-day login trend (orange), user signups
- Business: KPI strip + campaigns by status + revenue + posts breakdown + manual payment override
- Controls: banner, registration gate, runtime settings, manual metric collection trigger

## Background Services
- **Scheduler**: Every 1 min, `max_instances=1`, ordered by scheduled_time ASC
- **Metrics Updater**: Daily, paid campaigns only, filtered by `metrics_max_days` and `metrics_max_posts_per_user`

## Mastodon Integration

- OAuth authentication for user accounts (callback at `/mastodon/callback` in `routers/auth.py`)
- Publish via: POST /api/v1/statuses
- Store mastodon_status_id and published_at timestamp
- Fetch metrics and follower counts via Mastodon API

## X (Twitter) Integration

- OAuth 1.0a (3-legged) — tokens never expire; consumer credentials in `X_API_KEY`/`X_API_SECRET` (empty = X UI hidden)
- Connect-only: X accounts attach to the logged-in user (`POST /x/connect`, callback `/x/callback`); login stays Mastodon-only
- One platform per campaign: `x_account_id` XOR `mastodon_account_id` (enforced in campaign create/edit)
- Hand-rolled HMAC-SHA1 signer in `services/x_service.py` (stdlib only, no new deps); JSON bodies are NOT signed (only query + oauth_* params)
- Publish via: POST /2/tweets; stores `post.x_status_id`
- **Free tier is write-only** — no X metrics, no follower snapshots, no replies; metrics page shows a notice for X campaigns
- Polls not supported by X API — rejected server-side and hidden in UI for X campaigns
- Char limit 280 for X posts (AI prompts, UI counters, server truncation all platform-aware)
- Migration: `python -m app.migrations.add_x_support [--prod]`

## Constraints

- Posts: max 500 characters (Mastodon) / 280 characters (X)
- Default campaign duration: 30 days
- Keep implementation minimal and readable
- No subscriptions — pay-what-you-want, one-time per campaign
- Single uvicorn worker only — APScheduler runs inside the app process
- X Free tier: 1,500 tweets/month app-wide, write-only
- **bcrypt must be pinned to 3.2.2** — passlib 1.7.4 incompatible with bcrypt 4+
- **httpx must be >=0.28.1** — required by polar-sdk 0.31.3

## Testing

- `pytest==9.0.3` in requirements
- Run: `python -m pytest tests/ -v`
- `tests/test_campaign_reschedule.py` — 8 unit tests for stale post rescheduling logic (in-memory SQLite)
- `tests/test_x_support.py` — X OAuth flow, scheduler X branch, XOR account validation, 280-char/poll constraints, token-at-rest regression
