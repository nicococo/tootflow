# TootFlow – Project Specification (MVP)

## 1. Overview

**TootFlow** is a minimal SaaS web application for planning and running Mastodon campaigns.

It enables users to:

- Generate Mastodon posts (toots) using AI
- **Edit all generated content manually**
- Schedule posts over a defined campaign period
- Publish automatically via the Mastodon API
- Track basic engagement metrics

The product uses a **campaign-based business model**:

- One-time payment per campaign (default: 30 days)
- No subscriptions
- Simple, indie-friendly pricing

Focus is on simplicity, speed, and clarity.  
Designed as a small, single-service indie SaaS.

---

## 2. Design Philosophy

### Functional Philosophy

- Minimal feature set
- No overengineering
- Fast workflow: idea → campaign → running

### Visual Philosophy (Brutalist Web Design)

The UI should follow a **brutalist design style**:

Characteristics:

- System fonts only
- High contrast (black / white / strong primary colors)
- No gradients, shadows, or animations
- Visible borders and layout structure
- Minimal styling, maximum clarity
- Large buttons and simple forms
- Function over decoration

Goal:
> Fast, raw, utilitarian interface that feels honest and efficient.

---

## 3. Core Features (MVP)

### 3.1 Campaign Management

Create a campaign with:

- Name
- Description / topic
- Language
- Tone (professional, casual, technical, etc.)
- Duration (default: 30 days)
- Posts per week
- Campaign template

Templates:

- Product Launch  
- Open Source Release  
- Event Promotion  

---

### 3.2 AI Post Generation

Generate a full campaign content plan based on:

- Campaign description
- Template
- Tone and language

Constraints:

- Max 500 characters per post
- Mastodon-friendly style
- Optional hashtags

---

### 3.3 Manual Editing (Required)

All generated posts must be:

- Fully editable by the user
- Editable inline or via edit page

User can:

- Modify text
- Delete posts
- Add new posts manually

This is a **human-in-the-loop workflow**.

---

### 3.4 Scheduling

- Posts are distributed across campaign duration
- User can modify scheduled times
- Simple list view is sufficient
- Background worker publishes posts at scheduled time

---

### 3.5 Mastodon Integration

Features:

- OAuth authentication
- Secure token storage
- Publish via: POST /api/v1/statuses
Store:
- Mastodon status ID
- Publish timestamp

---

### 3.6 Metrics

For each published post:

- Favourites
- Reblogs
- Replies

Background job:

- Runs daily
- Updates metrics via Mastodon API

Dashboard shows engagement per post.

---

## 4. Payment (LemonSqueezy – One-Time Campaign Purchase)

### Business Model

- One-time payment per campaign
- Example price: **29€ per campaign**
- Campaign must be paid before activation
- No subscriptions

---

### LemonSqueezy Integration

Use **LemonSqueezy Checkout** for payment handling.

Flow:

1. User creates a campaign
2. Campaign status = `draft`
3. User clicks **Activate Campaign**
4. Redirect to LemonSqueezy Checkout
5. After successful payment:

- LemonSqueezy sends **webhook**
- Campaign status → `active`
- Campaign start date is set

---

### Required Webhooks

Handle:

- `order_created`
- `payment_success`

Webhook processing:

- Verify signature
- Match order to:
- user_id
- campaign_id
- Mark campaign as paid/active

---

### Campaign States

- `draft`
- `active`
- `running`
- `completed`
- `cancelled` (optional)

Only **active/running** campaigns:

- Can publish posts
- Trigger scheduler

---

### Data Model Additions

#### Payment

- id
- user_id
- campaign_id
- lemonsqueezy_order_id
- amount
- currency
- status
- created_at

#### Campaign (additional fields)

- payment_status
- payment_id (optional)

---

## 5. User Flow

1. User signs up / logs in
2. Connect Mastodon account
3. Create campaign
4. Generate posts
5. **Review and edit posts**
6. Click **Activate Campaign**
7. Complete LemonSqueezy payment
8. Campaign becomes active
9. Posts publish automatically
10. User monitors metrics

---

## 6. Tech Stack

Backend:

- Python
- FastAPI
- SQLModel / SQLAlchemy
- SQLite (dev)
- PostgreSQL (prod)

Frontend:

- Server-rendered HTML (Jinja2)
- HTMX (optional)
- Minimal brutalist CSS

Background Jobs:

- APScheduler

AI:

- OpenAI API (or compatible)

Payments:

- LemonSqueezy Checkout + Webhooks

---

## 7. Data Model (Minimal)

### User

- id
- email
- password_hash
- created_at

### MastodonAccount

- id
- user_id
- instance_url
- access_token

### Campaign

- id
- user_id
- name
- description
- template_type
- tone
- language
- start_date
- end_date
- posts_per_week
- status
- payment_status

### Post

- id
- campaign_id
- content
- scheduled_time
- status
- mastodon_status_id

### Metrics

- post_id
- favourites
- reblogs
- replies
- last_updated

### Payment

- id
- user_id
- campaign_id
- lemonsqueezy_order_id
- amount
- currency
- status
- created_at

---

## 8. Background Services

### Scheduler

- Runs every minute
- Publishes due posts for active campaigns

### Metrics Updater

- Runs daily
- Updates engagement data

---

## 9. Deliverables

- Project structure
- Database models
- Auth system
- Campaign CRUD
- Post CRUD + editing
- AI generation endpoint
- Mastodon service
- LemonSqueezy integration
- Webhook endpoint
- Scheduler
- UI pages:
- Dashboard
- Create campaign
- Edit posts
- Activate/payment
- Metrics

---

## 10. Constraints

- Single deployable application
- No microservices
- Keep implementation minimal and readable
- Avoid unnecessary abstractions
- Prioritize shipping speed

---

## 11. Success Criteria (MVP)

A user can:

1. Connect Mastodon  
2. Create a campaign  
3. Generate posts  
4. Edit posts manually  
5. Pay once via LemonSqueezy  
6. Activate campaign  
7. Have posts published automatically  
8. View engagement metrics
