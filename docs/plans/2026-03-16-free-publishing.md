# Free Publishing Model Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow all campaigns (paid and unpaid) to publish posts freely; restrict payment to unlocking AI generation, the full metrics page, and newsletter/LinkedIn export only.

**Architecture:** Three focused changes: (1) remove the `payment_status == "paid"` gate from the scheduler so unpaid campaigns publish too, and compute engagement overview for all campaigns in the detail router; (2) redesign the campaign detail progress steps from 4-step (with payment as step 2) to 3-step (Created → Posts Added → Publishing), and update the engagement sidebar to show for all campaigns; (3) update landing page and campaign_create copy to reflect "free to publish" messaging.

**Tech Stack:** FastAPI, SQLModel, Jinja2, APScheduler.

---

## Key Decisions

- **Scheduler gate removed:** `Campaign.payment_status == "paid"` is deleted from `publish_due_posts`. All `active`/`running` + `enabled` campaigns publish.
- **AI generation stays paid:** `posts.py` gate on `payment_status != "paid"` for AI generation remains untouched.
- **Metrics page stays paid:** `metrics.py` redirect for unpaid campaigns remains untouched.
- **Export stays paid:** `campaigns.py` export gates remain untouched.
- **Overview engagement sidebar:** shown for all campaigns (unpaid campaigns can now have published posts too). "View full metrics →" link only shown for paid campaigns.
- **Progress steps simplified:** 4 steps → 3 steps. Payment is no longer a lifecycle step; it's a feature-unlock accessible via the existing paid-features action row.
- **1-campaign free limit unchanged:** `FREE_ACTIVE_STATUSES` and the free slot counter in `campaigns.py` are unchanged.

---

### Task 1: Remove scheduler payment gate + overview metrics for all campaigns

**Files:**
- Modify: `app/scheduler.py` (lines 21-30)
- Modify: `app/routers/campaigns.py` — `campaign_detail` function (lines ~176-192)

**Step 1: Remove `Campaign.payment_status == "paid"` from scheduler**

Find in `app/scheduler.py`:
```python
        due_posts = session.exec(
            select(Post)
            .join(Campaign)
            .where(
                Post.status == "pending",
                Post.scheduled_time <= now,
                Campaign.status.in_(["active", "running"]),
                Campaign.payment_status == "paid",
                Campaign.enabled == True,
            )
        ).all()
```
Replace with:
```python
        due_posts = session.exec(
            select(Post)
            .join(Campaign)
            .where(
                Post.status == "pending",
                Post.scheduled_time <= now,
                Campaign.status.in_(["active", "running"]),
                Campaign.enabled == True,
            )
        ).all()
```

**Step 2: Compute overview metrics for ALL campaigns in `campaign_detail`**

In `app/routers/campaigns.py`, find the overview metrics block (around line 176):
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
Replace with:
```python
    # Overview metrics for all campaigns (free publishing means any campaign can have published posts)
    overview_favs = overview_reblogs = overview_replies = 0
    from app.models import Metrics
    published_posts = [p for p in posts if p.status == "published"]
    for p in published_posts:
        m = session.exec(select(Metrics).where(Metrics.post_id == p.id)).first()
        if m:
            overview_favs    += m.favourites
            overview_reblogs += m.reblogs
            overview_replies += m.replies
```

**Step 3: Verify app starts**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```
Expected: `OK`

**Step 4: Commit**

```bash
git add app/scheduler.py app/routers/campaigns.py
git commit -m "feat(freemium): allow all campaigns to publish; compute metrics for all"
```

---

### Task 2: Redesign campaign_detail.html — progress steps + engagement sidebar

**Files:**
- Modify: `app/templates/campaign_detail.html`

Read the full file first before editing. The progress steps block and engagement sidebar are in the `<aside>` section.

**Step 1: Replace the 4-step progress block with 3 steps**

Find the Campaign progress `<div class="info-panel">` block (the one with `<div class="eyebrow">Progress</div>`). It currently has 4 `progress-step` divs: Created, Activated (payment), Posts Ready, Publishing.

Replace the entire `<div class="info-panel-body">` inside that panel with this 3-step version:

```html
      <div class="info-panel-body">

        <div class="progress-step">
          <div class="step-icon done">✓</div>
          <div>
            <div class="step-label">Created</div>
          </div>
        </div>

        <div class="progress-step">
          <div class="step-icon {% if has_posts %}done{% else %}current{% endif %}">
            {% if has_posts %}✓{% else %}2{% endif %}
          </div>
          <div>
            <div class="step-label {% if not has_posts %}{% endif %}">Posts Added</div>
            {% if not has_posts %}
            <div class="step-hint">Add posts manually. <a href="/campaigns/{{ campaign.id }}/posts" style="color:var(--teal);">Go to posts →</a></div>
            {% endif %}
          </div>
        </div>

        <div class="progress-step">
          <div class="step-icon {% if campaign.status == 'completed' %}done{% elif is_running %}current{% endif %}">
            {% if campaign.status == 'completed' %}✓{% elif is_running %}3{% else %}3{% endif %}
          </div>
          <div>
            <div class="step-label {% if not is_running %}muted{% endif %}">Publishing</div>
            {% if campaign.status == 'running' %}
            <div class="step-hint">{{ published_count }} of {{ posts | length }} published.</div>
            {% elif campaign.status == 'completed' %}
            <div class="step-hint">All posts published.</div>
            {% elif has_posts %}
            <div class="step-hint">{{ pending_count }} post{{ 's' if pending_count != 1 else '' }} pending.</div>
            {% endif %}
          </div>
        </div>

      </div>
```

**Step 2: Update the engagement sidebar to show for all campaigns**

Find the engagement `{% if is_paid %}` / `{% else %}` block in the sidebar (after the Settings panel). Currently the `{% else %}` branch shows "Activate this campaign to publish posts and track engagement." — this is no longer accurate since all campaigns can publish.

Replace the entire engagement block (both `{% if is_paid %}` and `{% else %}` branches, including `{% endif %}`) with:

```html
    <!-- Engagement overview (all campaigns) -->
    <div class="info-panel">
      <div class="info-panel-head">
        <div class="eyebrow">Engagement</div>
      </div>
      <div class="info-panel-body">
        {% if published_count > 0 %}
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
        {% if is_paid %}
        <div class="stat-row">
          <a href="/campaigns/{{ campaign.id }}/metrics" style="font-size:11px; letter-spacing:0.5px;">View full metrics →</a>
        </div>
        {% else %}
        <div class="stat-row" style="font-size:11px; color:var(--muted);">
          Activate for per-post metrics &amp; charts.
        </div>
        {% endif %}
        {% else %}
        <div class="info-row" style="color:var(--muted); font-size:12px;">
          No posts published yet.
        </div>
        {% endif %}
      </div>
    </div>
```

**Step 3: Update the paid features row description for unpaid campaigns**

Find the `paid-row-locked` div (inside the action bar) where the paid features are listed for unpaid campaigns. Currently:
```html
        <div class="paid-feature-list">
          <span class="paid-feature-item">⬡ AI Generation</span>
          <span class="paid-feature-item">⬡ Full Metrics</span>
          <span class="paid-feature-item">⬡ Newsletter &amp; LinkedIn Export</span>
        </div>
```
This is already correct — no change needed here. ✓

Also find the `metrics_locked` notice (shown when redirected from metrics page). Update the text:

Find:
```html
      Activate this campaign for {{ settings.campaign_price }} {{ settings.campaign_currency }} to unlock per-post metrics, engagement charts, and reply tracking.
```
Replace with:
```html
      Activate this campaign for {{ settings.campaign_price }} {{ settings.campaign_currency }} to unlock per-post metrics, engagement charts, and reply tracking. Publishing is free.
```

**Step 4: Update the step hint in the progress step that references paying**

Find and remove any remaining reference to payment in the progress steps. The new 3-step block (from Step 1 above) already omits payment — but also check the sidebar "Progress" panel for any `{% if not is_paid %}` hints that are now stale. Replace them if found.

Also find in the campaign detail info table the AI calls row:
```html
          {% if campaign.payment_status != "paid" %}
            <span style="color:var(--muted); font-size:13px;">Activate campaign to unlock AI generation.</span>
```
This is correct and should stay — AI generation is still paid.

**Step 5: Verify**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```
Expected: `OK`

**Step 6: Commit**

```bash
git add app/templates/campaign_detail.html
git commit -m "feat(freemium): simplify progress steps, show engagement for all campaigns"
```

---

### Task 3: Update landing page and campaign_create copy

**Files:**
- Modify: `app/templates/index.html`
- Modify: `app/templates/campaign_create.html`

**Step 1: Update the lp-band copy in `index.html`**

Find (around line 594):
```html
  <p>Free to plan &nbsp;·&nbsp; {{ settings.campaign_price }} {{ settings.campaign_currency }} to activate &nbsp;·&nbsp; AI drafting &nbsp;·&nbsp; Full metrics &nbsp;·&nbsp; No subscription</p>
```
Replace with:
```html
  <p>Free to publish &nbsp;·&nbsp; {{ settings.campaign_price }} {{ settings.campaign_currency }} for AI + metrics + export &nbsp;·&nbsp; No subscription</p>
```

**Step 2: Update How-it-works step 1 in `index.html`**

Find (around line 604):
```html
        <p>OAuth login — no email, no password. Free to start: plan a campaign and add posts without paying.</p>
```
Replace with:
```html
        <p>OAuth login — no email, no password. Free to start: plan a campaign, add posts, and publish — no payment needed.</p>
```

**Step 3: Update How-it-works step 3 "Pay once to activate" in `index.html`**

Find (around line 614):
```html
        <p>One-time {{ settings.campaign_price }} {{ settings.campaign_currency }} unlocks AI generation and automated publishing for this campaign.</p>
```
Replace with:
```html
        <p>One-time {{ settings.campaign_price }} {{ settings.campaign_currency }} unlocks AI generation, full per-post metrics, and newsletter/LinkedIn export for this campaign.</p>
```

**Step 4: Update workflow step "Activate" in `index.html`**

Find (around line 680):
```html
        <p>Pay once ({{ settings.campaign_price }} {{ settings.campaign_currency }}) to activate, then generate a full AI post schedule with one click.</p>
```
Replace with:
```html
        <p>Pay once ({{ settings.campaign_price }} {{ settings.campaign_currency }}) to unlock AI generation, full metrics, and export. Publishing is always free.</p>
```

**Step 5: Update campaign_create free limit notice**

Find in `app/templates/campaign_create.html` (line 69):
```html
    You already have an active unpaid campaign. Activate it for {{ settings.campaign_price }} {{ settings.campaign_currency }} to unlock all features, or delete it to start a new free campaign.
```
Replace with:
```html
    You already have an active campaign. Activate it for {{ settings.campaign_price }} {{ settings.campaign_currency }} to unlock AI generation, metrics, and export — or delete it to start a new free campaign.
```

**Step 6: Update campaign_create hint below form**

Find in `app/templates/campaign_create.html` (line 164):
```html
      After creating the campaign you can add posts manually or use AI generation once activated.
```
Replace with:
```html
      After creating the campaign you can add posts manually and publish for free. Activate for AI generation, full metrics, and export.
```

**Step 7: Verify**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```
Expected: `OK`

**Step 8: Commit**

```bash
git add app/templates/index.html app/templates/campaign_create.html
git commit -m "feat(freemium): update copy to reflect free publishing model"
```
