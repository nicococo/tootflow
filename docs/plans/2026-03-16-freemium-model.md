# Freemium Model Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Introduce a free tier (1 active unpaid campaign, manual posts only) and reduce the activation price to 5€, gating AI generation, full metrics page, and export behind payment.

**Architecture:** Four focused changes: (1) enforce the 1-active-unpaid-campaign limit at campaign creation, (2) gate the metrics page behind payment, (3) add overview metrics (total favs/reblogs/replies) to the campaign detail sidebar for paid campaigns, (4) update pricing and copy throughout. No schema changes required — `payment_status` already distinguishes free vs paid.

**Tech Stack:** FastAPI, SQLModel, Jinja2, existing `payment_status` field on Campaign model.

---

## Key Decisions

- **"Active" for the free limit** = campaigns where `payment_status == "unpaid"` AND `status in ("draft_unpaid", "active", "running")`. Completed campaigns do NOT count.
- **Free tier value**: Draft campaigns, add/edit/schedule posts manually, see post breakdown. Publishing requires activation (5€).
- **Overview metrics in sidebar**: shown only for paid campaigns (free campaigns have no published posts since publishing requires activation). For free campaigns, sidebar shows an "Activate to publish" nudge instead.
- **Metrics page**: fully gated — redirects to campaign detail with a flash message for unpaid campaigns.
- **AI generation**: already gated behind `payment_status == "paid"` in `posts.py` — no change needed.
- **Export**: already gated behind `payment_status == "paid"` in `campaigns.py` — no change needed.

---

### Task 1: Enforce 1-active-unpaid-campaign limit at campaign creation

**Files:**
- Modify: `app/routers/campaigns.py` (lines 57-108)
- Modify: `app/templates/campaign_create.html`

**Step 1: Add a helper function at the top of `campaigns.py` (after imports)**

Add after line 18 (`settings = get_settings()`):

```python
FREE_CAMPAIGN_LIMIT = 1
FREE_ACTIVE_STATUSES = ("draft_unpaid", "active", "running")


def count_active_unpaid_campaigns(session: Session, user_id: int) -> int:
    """Count campaigns that occupy the user's free slot."""
    campaigns = session.exec(
        select(Campaign).where(Campaign.user_id == user_id)
    ).all()
    return sum(
        1 for c in campaigns
        if c.payment_status == "unpaid" and c.status in FREE_ACTIVE_STATUSES
    )
```

**Step 2: Add the limit check to `create_campaign` in `campaigns.py`**

Find the `create_campaign` function (line 67). After the input validation block (after line 92, before `campaign = Campaign(...)`), add:

```python
    # Free tier: max 1 active unpaid campaign
    active_unpaid = count_active_unpaid_campaigns(session, user.id)
    if active_unpaid >= FREE_CAMPAIGN_LIMIT:
        existing = session.exec(
            select(Campaign).where(Campaign.user_id == user.id)
            .order_by(Campaign.created_at.desc())
        ).all()
        return templates.TemplateResponse(
            "campaign_create.html",
            {
                "request": request,
                "user": user,
                "templates": TEMPLATES,
                "tones": TONES,
                "languages": LANGUAGES,
                "existing_campaigns": existing,
                "free_limit_hit": True,
            },
            status_code=400,
        )
```

**Step 3: Add the limit check to `new_campaign_page` in `campaigns.py`**

Find `new_campaign_page` (line 57). Update it to pass the limit status:

```python
@router.get("/new", response_class=HTMLResponse)
def new_campaign_page(request: Request, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    existing = session.exec(select(Campaign).where(Campaign.user_id == user.id).order_by(Campaign.created_at.desc())).all()
    active_unpaid = count_active_unpaid_campaigns(session, user.id)
    return templates.TemplateResponse(
        "campaign_create.html",
        {
            "request": request,
            "user": user,
            "templates": TEMPLATES,
            "tones": TONES,
            "languages": LANGUAGES,
            "existing_campaigns": existing,
            "free_limit_hit": active_unpaid >= FREE_CAMPAIGN_LIMIT,
        },
    )
```

**Step 4: Add the notice to `campaign_create.html`**

Read `app/templates/campaign_create.html` first. Find the opening of the main form content and add a notice block near the top of the form (after `{% block content %}` or before the first `<form>`):

```html
{% if free_limit_hit %}
<div class="notice" style="border-color:var(--orange); background:#fffaf3; margin-bottom:24px;">
  <strong>Free campaign slot in use.</strong>
  You already have an active unpaid campaign. Activate it for {{ settings.campaign_price }}€ or complete it before creating another free campaign.
  <a href="/dashboard" style="margin-left:8px;">← Back to Dashboard</a>
</div>
{% endif %}
```

Also disable the submit button when limit is hit by wrapping the submit button:

```html
<button type="submit" class="btn btn-primary" {% if free_limit_hit %}disabled title="Free slot in use"{% endif %}>
  Create Campaign
</button>
```

**Step 5: Verify the app starts**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```
Expected: `OK`

**Step 6: Commit**

```bash
git add app/routers/campaigns.py app/templates/campaign_create.html
git commit -m "feat(freemium): enforce 1 active unpaid campaign limit"
```

---

### Task 2: Gate the metrics page behind payment

**Files:**
- Modify: `app/routers/metrics.py` (lines 18-83)
- Modify: `app/templates/campaign_detail.html` (line 303)

**Step 1: Add gate to `campaign_metrics` in `metrics.py`**

Find the `campaign_metrics` function (line 18). After the ownership check (after `raise HTTPException(status_code=404)`), add:

```python
    if campaign.payment_status != "paid":
        return RedirectResponse(
            f"/campaigns/{campaign_id}?metrics_locked=1",
            status_code=302,
        )
```

**Step 2: Show a notice in `campaign_detail.html` when redirected**

In `campaign_detail.html`, find the `{% set is_paid = ... %}` block near the top (line 90). Add after it:

```html
{% set metrics_locked = request.query_params.get("metrics_locked") == "1" %}
```

Then find the actions div (around line 300) and add above it:

```html
{% if metrics_locked %}
<div class="notice" style="border-color:var(--orange); background:#fffaf3; margin-bottom:20px;">
  <strong>Full metrics require an activated campaign.</strong>
  Activate this campaign for {{ settings.campaign_price }}€ to unlock per-post metrics, engagement charts, and reply tracking.
</div>
{% endif %}
```

**Step 3: Update the "View Metrics" button in `campaign_detail.html`**

Find line 303:
```html
      <a href="/campaigns/{{ campaign.id }}/metrics" class="btn btn-outline">View Metrics</a>
```

Replace with:
```html
      {% if is_paid %}
      <a href="/campaigns/{{ campaign.id }}/metrics" class="btn btn-outline">View Metrics</a>
      {% else %}
      <a href="/campaigns/{{ campaign.id }}/metrics" class="btn btn-outline" style="opacity:0.5;" title="Activate campaign to unlock full metrics">View Metrics 🔒</a>
      {% endif %}
```

**Step 4: Verify**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```
Expected: `OK`

**Step 5: Commit**

```bash
git add app/routers/metrics.py app/templates/campaign_detail.html
git commit -m "feat(freemium): gate metrics page behind payment, show lock on detail page"
```

---

### Task 3: Add overview metrics to campaign detail sidebar

**Files:**
- Modify: `app/routers/campaigns.py` (campaign_detail function, lines 111-129)
- Modify: `app/templates/campaign_detail.html`

**Step 1: Add metrics query to `campaign_detail` in `campaigns.py`**

Find the `campaign_detail` function (line 111). After the `stale_pending_count` line (line 124), add:

```python
    # Overview metrics for paid campaigns
    overview_favs = overview_reblogs = overview_replies = 0
    if campaign.payment_status == "paid":
        from app.models import Metrics
        published_posts = [p for p in posts if p.status == "published"]
        for p in published_posts:
            m = session.exec(select(Metrics).where(Metrics.post_id == p.id)).first()
            if m:
                overview_favs    += m.favourites
                overview_reblogs += m.reblogs
                overview_replies += m.replies
```

Update the `TemplateResponse` call to include the new variables:

```python
    return templates.TemplateResponse(
        "campaign_detail.html",
        {
            "request": request,
            "user": user,
            "campaign": campaign,
            "posts": posts,
            "mastodon": mastodon,
            "ai_max": settings.openai_max_posts_per_campaign,
            "stale_pending_count": stale_pending_count,
            "overview_favs": overview_favs,
            "overview_reblogs": overview_reblogs,
            "overview_replies": overview_replies,
        },
    )
```

**Step 2: Add engagement panel to sidebar in `campaign_detail.html`**

Find the closing `</aside>` tag (line 217). Insert before it:

```html
    <!-- Engagement overview (paid campaigns only) -->
    {% if is_paid %}
    <div class="info-panel">
      <div class="info-panel-head">
        <div class="eyebrow">Engagement</div>
      </div>
      <div class="info-panel-body">
        <div class="stat-row">
          <span>Favourites</span>
          <strong style="color:var(--orange);">{{ overview_favs }}</strong>
        </div>
        <div class="stat-row">
          <span>Reblogs</span>
          <strong style="color:var(--purple);">{{ overview_reblogs }}</strong>
        </div>
        <div class="stat-row">
          <span>Replies</span>
          <strong style="color:var(--teal);">{{ overview_replies }}</strong>
        </div>
        <div class="stat-row">
          <a href="/campaigns/{{ campaign.id }}/metrics" style="font-size:11px; letter-spacing:0.5px;">View full metrics →</a>
        </div>
      </div>
    </div>
    {% else %}
    <div class="info-panel">
      <div class="info-panel-head">
        <div class="eyebrow">Engagement</div>
      </div>
      <div class="info-panel-body">
        <div class="info-row" style="color:var(--muted); font-size:12px;">
          Activate this campaign to publish posts and track engagement.
        </div>
      </div>
    </div>
    {% endif %}
```

**Step 3: Verify**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```
Expected: `OK`

**Step 4: Commit**

```bash
git add app/routers/campaigns.py app/templates/campaign_detail.html
git commit -m "feat(freemium): add overview engagement metrics to campaign detail sidebar"
```

---

### Task 4: Update pricing to 5€ and update copy

**Files:**
- Modify: `app/config.py`
- Modify: `app/templates/campaign_detail.html`
- Modify: `app/templates/index.html`

**Step 1: Update default price in `app/config.py`**

Read the file first. Find the `campaign_price` field in the `Settings` class. Change its default from `"29"` to `"5"`:

```python
campaign_price: str = "5"
```

**Step 2: Update activation step hint in `campaign_detail.html`**

Find line 132 in `campaign_detail.html`:
```html
            <div class="step-hint">Pay {{ settings.campaign_price }}€ to unlock AI generation.</div>
```

Replace with:
```html
            <div class="step-hint">Pay {{ settings.campaign_price }}€ to unlock AI generation, full metrics, and export.</div>
```

**Step 3: Update the landing page hero band copy in `index.html`**

Find the band section (around line 583-585):
```html
<div class="lp-band">
  <p>AI-assisted drafting &nbsp;·&nbsp; Full editorial control &nbsp;·&nbsp; Poll support &nbsp;·&nbsp; Automated publishing &nbsp;·&nbsp; Engagement insights</p>
</div>
```

Replace the `<p>` content with:
```html
  <p>Free to plan &nbsp;·&nbsp; {{ settings.campaign_price }}€ to activate &nbsp;·&nbsp; AI drafting &nbsp;·&nbsp; Full metrics &nbsp;·&nbsp; No subscription</p>
```

**Step 4: Update the "How it works" step 3 in `index.html`**

Find the step that says "Pay once to activate" (around line 604):
```html
        <p>One-time {{ settings.campaign_price }}€ unlocks AI generation and automated publishing for this campaign.</p>
```

This already uses `{{ settings.campaign_price }}` so it will update automatically. No change needed here.

**Step 5: Update the workflow step 02 in `index.html`**

Find around line 669:
```html
        <p>Pay once ({{ settings.campaign_price }}€) to activate, then generate a full AI post schedule with one click.</p>
```

Already uses the template variable — no change needed.

**Step 6: Add a free tier mention to the hero trust line in `index.html`**

Find the `lp-hero-trust` div (around line 544):
```html
      <div class="lp-hero-trust">
        <span>No email required</span>
        <span>No card data stored</span>
        <span>No tracking</span>
        <span>No ads</span>
      </div>
```

Replace with:
```html
      <div class="lp-hero-trust">
        <span>Free to start</span>
        <span>No email required</span>
        <span>No card data stored</span>
        <span>No tracking</span>
      </div>
```

**Step 7: Update the "How it works" step 1 in `index.html` to reflect free tier**

Find the first how-it-works step (around line 592):
```html
      <div class="lp-how-step">
        <h3 class="lp-step-title">Sign in with Mastodon</h3>
        <p>OAuth login — no email, no password. Your Mastodon account is your identity.</p>
        <span class="lp-step-num">1</span>
      </div>
```

Replace the `<p>` with:
```html
        <p>OAuth login — no email, no password. Free to start: create a campaign and plan posts without paying.</p>
```

**Step 8: Verify**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```
Expected: `OK`

**Step 9: Commit**

```bash
git add app/config.py app/templates/campaign_detail.html app/templates/index.html
git commit -m "feat(freemium): update pricing to 5€ and update landing page copy"
```
