# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**TootFlow** is a minimal SaaS web application for planning and running Mastodon campaigns. It's designed as a single-service indie SaaS with a campaign-based business model (one-time payment per campaign, no subscriptions).

Core workflow: Users generate AI-powered Mastodon posts, manually edit them, pay for campaign activation via LemonSqueezy, then posts publish automatically on schedule.

## Tech Stack

- **Backend**: Python, FastAPI, SQLModel/SQLAlchemy
- **Database**: SQLite (dev), PostgreSQL (prod)
- **Frontend**: Server-rendered HTML with Jinja2, minimal CSS
- **Background Jobs**: APScheduler (post publishing, metrics updates)
- **AI**: OpenAI API (or compatible)
- **Payments**: LemonSqueezy (one-time checkout + webhooks)
- **External API**: Mastodon OAuth and API integration

## Architecture Principles

### Simplicity First
- Single deployable application (no microservices)
- Minimal feature set, no overengineering
- Avoid unnecessary abstractions
- Prioritize shipping speed and readability

### Design Philosophy (Brutalist)
The UI follows brutalist web design:
- System fonts only
- High contrast (black/white/strong colors)
- No gradients, shadows, or animations
- Visible borders and layout structure
- Function over decoration

## Core Data Model

- **User**: email, password_hash
- **MastodonAccount**: instance_url, access_token (per user)
- **Campaign**: name, description, template_type, tone, language, duration, posts_per_week, status, payment_status
- **Post**: campaign_id, content (max 500 chars), scheduled_time, status, mastodon_status_id
- **Metrics**: post_id, favourites, reblogs, replies
- **Payment**: campaign_id, lemonsqueezy_order_id, amount, status

## Campaign States

Campaign progression: `draft` (no posts) → `active` (posts added) → `running` → `completed`

`status` and `payment_status` are independent. The scheduler only publishes posts for campaigns where `payment_status == "paid"` AND `status in ("active", "running")`.

## Key Workflows

### Post Generation
AI generates full campaign content plan based on template, tone, and language. Posts must be fully editable by users (human-in-the-loop workflow). Users can modify, delete, or add posts manually.

### Payment Flow
1. User creates campaign (status: `draft_unpaid`)
2. User clicks "Activate Campaign" → redirect to LemonSqueezy Checkout
3. LemonSqueezy webhook confirms payment
4. Campaign status → `active`, start_date is set
5. Scheduler begins publishing posts

### Background Services
- **Scheduler**: Runs every minute, publishes due posts for active campaigns
- **Metrics Updater**: Runs daily, fetches engagement data from Mastodon API

## Mastodon Integration

- OAuth authentication for user accounts
- Publish via: POST /api/v1/statuses
- Store mastodon_status_id and publish timestamp
- Fetch metrics via Mastodon API

## Constraints

- Posts: max 500 characters (Mastodon-friendly)
- Default campaign duration: 30 days
- Keep implementation minimal and readable
- No subscriptions - one-time payment model only
