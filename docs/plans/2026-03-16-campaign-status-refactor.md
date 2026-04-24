# Campaign Status Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Decouple `payment_status` from `status` by renaming `draft_unpaid` → `draft`, removing the payment webhook's status mutation, auto-transitioning `draft → active` when the first post is added, and adding an explicit `payment_status == "paid"` gate to the scheduler.

**Architecture:** Four focused changes: (1) rename the status value in the DB and model default, (2) strip the status mutation from the payment/refund webhook so payment only touches `payment_status`, (3) auto-advance campaign from `draft` to `active` when first post is added (in `posts.py` and `campaigns.py`), (4) add a `payment_status == "paid"` filter to the scheduler query so unpaid `active` campaigns never publish. No new schema columns required — only data migration.

**Tech Stack:** FastAPI, SQLModel, SQLite (dev), Jinja2. Migration via SQLite `UPDATE`.

---

## Key Decisions

- **`draft`** = campaign shell, zero posts (paid or unpaid).
- **`active`** = at least one post exists, awaiting first publish (paid or unpaid). Scheduler will only act on paid ones.
- **`running`** = scheduler has published at least one post (always paid, by gate).
- **`completed`** = all posts published (always paid).
- **`paused`** = `enabled = False` (unchanged — folding into `status` is a separate future task).
- **Refund handler** resets to `draft` + `unpaid` + `enabled=False` (same as before, just new name).
- **Admin manual-activate** (`/admin/campaigns/{id}/activate`) keeps its explicit `campaign.status = "active"` — admin override is intentionally explicit.

---

### Task 1: DB migration + model default + FREE_ACTIVE_STATUSES

**Files:**
- Modify: `app/models.py` (line 37)
- Modify: `app/routers/campaigns.py` (lines 25, 30)
- Run: SQLite migration on `tootflow.db`

**Step 1: Update model default in `app/models.py`**

Find line 37:
```python
    status: str = "draft_unpaid"
```
Replace with:
```python
    status: str = "draft"
```

**Step 2: Update FREE_ACTIVE_STATUSES in `app/routers/campaigns.py`**

Find lines 25 and 30:
```python
FREE_ACTIVE_STATUSES = ("draft_unpaid", "active", "running")
```
```python
    Only draft_unpaid/active/running + unpaid + enabled campaigns count.
```
Replace with:
```python
FREE_ACTIVE_STATUSES = ("draft", "active", "running")
```
```python
    Only draft/active/running + unpaid + enabled campaigns count.
```

**Step 3: Run SQLite data migration**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "
from app.database import engine
from sqlalchemy import text
with engine.connect() as conn:
    result = conn.execute(text(\"UPDATE campaigns SET status = 'draft' WHERE status = 'draft_unpaid'\"))
    conn.commit()
    print(f'Migrated {result.rowcount} campaign(s)')
"
```
Expected: `Migrated N campaign(s)` (0 is fine if no draft_unpaid rows exist).

**Step 4: Verify no `draft_unpaid` rows remain**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "
from app.database import engine
from sqlalchemy import text
with engine.connect() as conn:
    rows = conn.execute(text(\"SELECT count(*) FROM campaigns WHERE status = 'draft_unpaid'\")).scalar()
    print('Remaining draft_unpaid:', rows)
    assert rows == 0, 'Migration incomplete'
    print('OK')
"
```
Expected: `Remaining draft_unpaid: 0` then `OK`.

**Step 5: Verify app starts**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```
Expected: `OK`

**Step 6: Commit**

```bash
git add app/models.py app/routers/campaigns.py
git commit -m "feat(status): rename draft_unpaid -> draft in model default and FREE_ACTIVE_STATUSES"
```

---

### Task 2: Decouple payment webhook from campaign status

**Files:**
- Modify: `app/routers/payments.py` (lines 83-95 refund block, lines 114-118 order_created block)

**Step 1: Update the refund handler**

Find in `payments.py` (lines 83-94):
```python
    if event_name == "order_refunded":
        # Revoke access and stop publishing
        campaign.payment_status = "unpaid"
        campaign.status = "draft_unpaid"
        campaign.enabled = False
```
Replace with:
```python
    if event_name == "order_refunded":
        # Revoke access and stop publishing
        campaign.payment_status = "unpaid"
        campaign.status = "draft"
        campaign.enabled = False
```

**Step 2: Update the order_created handler**

Find in `payments.py` (lines 114-118):
```python
    campaign.payment_status = "paid"
    campaign.status = "active"
    campaign.start_date = datetime.utcnow()
    campaign.end_date = datetime.utcnow() + timedelta(days=campaign.duration_days)
```
Replace with:
```python
    campaign.payment_status = "paid"
    # Status is driven by post lifecycle, not payment.
    # Only advance to "active" if the campaign already has posts; otherwise
    # it stays "draft" until the user adds posts.
    if campaign.status == "draft":
        from sqlmodel import select as _select
        from app.models import Post as _Post
        has_posts = session.exec(
            _select(_Post).where(_Post.campaign_id == campaign_id)
        ).first()
        if has_posts:
            campaign.status = "active"
    campaign.start_date = datetime.utcnow()
    campaign.end_date = datetime.utcnow() + timedelta(days=campaign.duration_days)
```

**Step 3: Verify**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```
Expected: `OK`

**Step 4: Commit**

```bash
git add app/routers/payments.py
git commit -m "feat(status): decouple payment webhook from campaign status"
```

---

### Task 3: Auto-transition draft → active when first post is added

**Files:**
- Modify: `app/routers/posts.py` — `add_post` function (lines 66-90)
- Modify: `app/routers/posts.py` — `generate_campaign_posts` function (lines 125-175)

**Step 1: Update `add_post` in `posts.py`**

Find in `add_post` (after `session.add(post)`, before `session.commit()`):
```python
    post = Post(campaign_id=campaign_id, content=content[:500], scheduled_time=sched,
                poll_options=opts_json, poll_expires_in=expires_in)
    session.add(post)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/posts", status_code=302)
```
Replace with:
```python
    post = Post(campaign_id=campaign_id, content=content[:500], scheduled_time=sched,
                poll_options=opts_json, poll_expires_in=expires_in)
    session.add(post)
    # Advance campaign from draft → active when first post is added
    if campaign.status == "draft":
        campaign.status = "active"
        session.add(campaign)
    session.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}/posts", status_code=302)
```

**Step 2: Update `generate_campaign_posts` in `posts.py`**

Find in `generate_campaign_posts` the block where posts are committed (look for `session.commit()` after the posts are added). Read the function first to locate exact lines. After all posts are added to the session and before the final `session.commit()`, add:

```python
    # Advance campaign from draft → active (posts now exist)
    if campaign.status == "draft":
        campaign.status = "active"
        session.add(campaign)
```

Note: The generate function clears existing posts before adding new ones, so if it runs on a "draft" campaign it should transition to "active".

**Step 3: Verify**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```
Expected: `OK`

**Step 4: Commit**

```bash
git add app/routers/posts.py
git commit -m "feat(status): auto-transition draft -> active when first post is added"
```

---

### Task 4: Add payment gate to scheduler + update templates and CSS

**Files:**
- Modify: `app/scheduler.py` (lines 21-29)
- Modify: `app/templates/base.html` (line 374)
- Modify: `app/templates/dashboard.html` (line 103)

**Step 1: Add `payment_status == "paid"` filter to scheduler query**

Find in `scheduler.py` (lines 21-30):
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
Replace with:
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

**Step 2: Remove `badge-draft_unpaid` alias from `base.html`**

Find line 374 in `app/templates/base.html`:
```css
    .badge-draft, .badge-draft_unpaid { background: var(--surface-soft); color: var(--muted); border: 1px solid #c8bfb4; }
```
Replace with:
```css
    .badge-draft { background: var(--surface-soft); color: var(--muted); border: 1px solid #c8bfb4; }
```

**Step 3: Update dashboard legend badge in `dashboard.html`**

Find line 103 in `app/templates/dashboard.html`:
```html
          <span class="badge badge-draft_unpaid" style="font-size:10px;">draft</span>
```
Replace with:
```html
          <span class="badge badge-draft" style="font-size:10px;">draft</span>
```

**Step 4: Verify**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```
Expected: `OK`

**Step 5: Commit**

```bash
git add app/scheduler.py app/templates/base.html app/templates/dashboard.html
git commit -m "feat(status): add payment gate to scheduler, remove draft_unpaid CSS alias"
```
