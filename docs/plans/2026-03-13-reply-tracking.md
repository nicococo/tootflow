# Reply Tracking Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Show Mastodon replies to published posts on the metrics page, fetched on demand when the user clicks a reply count button.

**Architecture:** A new `fetch_replies()` function in `mastodon_service.py` calls the Mastodon context API and filters to direct replies only. A new endpoint `GET /posts/{post_id}/replies` in the metrics router fetches replies and returns a rendered HTML fragment. The metrics template adds a clickable reply button per post row and swaps in the fragment via a plain `fetch()` call.

**Tech Stack:** FastAPI, Jinja2, httpx, vanilla JS (no new dependencies)

---

### Task 1: Add `fetch_replies` to mastodon_service.py

**Files:**
- Modify: `app/services/mastodon_service.py`

**Step 1: Add the function at the bottom of the file**

```python
async def fetch_replies(account: MastodonAccount, status_id: str) -> list[dict]:
    """Fetch direct replies to a published post from the Mastodon context API."""
    base = account.instance_url.rstrip("/")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base}/api/v1/statuses/{status_id}/context",
            headers={"Authorization": f"Bearer {account.access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()

    replies = []
    for s in data.get("descendants", []):
        if s.get("in_reply_to_id") == status_id:
            acct = s.get("account", {})
            replies.append({
                "account_name": acct.get("acct", "unknown"),
                "content": s.get("content", ""),
                "created_at": s.get("created_at", ""),
            })
    return replies
```

**Step 2: Verify the file is valid Python**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.services.mastodon_service import fetch_replies; print('OK')"
```

Expected: `OK`

**Step 3: Commit**

```bash
git add app/services/mastodon_service.py
git commit -m "feat(mastodon): add fetch_replies to mastodon_service"
```

---

### Task 2: Add replies endpoint to metrics router

**Files:**
- Modify: `app/routers/metrics.py`

**Step 1: Add imports at the top of the file**

Add to the existing imports block:
```python
import asyncio
from fastapi.responses import HTMLResponse
from app.crypto import decrypt_token
from app.services import mastodon_service
```

Note: `HTMLResponse` is already imported — skip if so. Check existing imports first.

**Step 2: Add a second router and the endpoint at the bottom of the file**

```python
# Separate router for post-level actions (no /campaigns prefix)
post_router = APIRouter(prefix="/posts")
post_router_templates = templates  # reuse same Jinja2Templates instance


@post_router.get("/{post_id}/replies", response_class=HTMLResponse)
async def post_replies(
    post_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404)

    campaign = session.get(Campaign, post.campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)

    if not post.mastodon_status_id:
        return HTMLResponse("<p>No published status found for this post.</p>")

    account = session.exec(
        select(MastodonAccount).where(MastodonAccount.user_id == user.id)
    ).first()
    if not account:
        return HTMLResponse("<p>No Mastodon account connected.</p>")

    account.access_token = decrypt_token(account.access_token)

    try:
        replies = await mastodon_service.fetch_replies(account, post.mastodon_status_id)
    except Exception:
        return HTMLResponse("<p>Could not load replies right now. Try again later.</p>")

    return templates.TemplateResponse(
        "replies_fragment.html",
        {"request": request, "replies": replies},
    )
```

**Step 3: Register `post_router` in `app/main.py`**

Find the section where routers are included (look for `app.include_router`) and add:

```python
from app.routers.metrics import post_router as metrics_post_router
app.include_router(metrics_post_router)
```

**Step 4: Verify the app starts cleanly**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "from app.main import app; print('OK')"
```

Expected: `OK`

**Step 5: Commit**

```bash
git add app/routers/metrics.py app/main.py
git commit -m "feat(metrics): add GET /posts/{id}/replies endpoint"
```

---

### Task 3: Create replies_fragment.html

**Files:**
- Create: `app/templates/replies_fragment.html`

**Step 1: Create the template**

```html
{% if replies %}
<div class="replies-list">
  {% for r in replies %}
  <div class="reply-item reply-item-{{ loop.index }}" {% if loop.index > 3 %}style="display:none;"{% endif %}>
    <div class="reply-meta">
      <span class="reply-author">@{{ r.account_name }}</span>
      <span class="reply-time">{{ r.created_at[:10] if r.created_at else "" }}</span>
    </div>
    <div class="reply-content">{{ r.content | striptags }}</div>
  </div>
  {% endfor %}
  {% if replies | length > 3 %}
  <button class="reply-show-more" onclick="
    this.closest('.replies-list').querySelectorAll('.reply-item').forEach(el => el.style.display = '');
    this.remove();
  ">+ {{ replies | length - 3 }} more repl{{ 'y' if replies | length - 3 == 1 else 'ies' }}</button>
  {% endif %}
</div>
{% else %}
<div class="reply-empty">No direct replies found.</div>
{% endif %}
```

**Step 2: Verify Jinja2 renders it**

```bash
cd /Users/nicococo/Projects/tootflow && python -c "
from fastapi.templating import Jinja2Templates
t = Jinja2Templates(directory='app/templates')
print('OK' if 'replies_fragment.html' in str(t.get_template('replies_fragment.html')) else 'FAIL')
"
```

Expected: `OK`

**Step 3: Commit**

```bash
git add app/templates/replies_fragment.html
git commit -m "feat(templates): add replies_fragment partial"
```

---

### Task 4: Update metrics.html — reply button and placeholder row

**Files:**
- Modify: `app/templates/metrics.html`

**Step 1: Add CSS for replies at the bottom of the `<style>` block (before closing `</style>`)**

```css
/* ── Replies ── */
.reply-toggle {
  background: none;
  border: none;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--teal);
  cursor: pointer;
  padding: 0;
  letter-spacing: 0.5px;
  text-decoration: underline;
}
.reply-toggle:hover { color: var(--ink); }
.replies-row td {
  padding: 0 !important;
  border-bottom: 2px solid var(--ink);
}
.replies-container {
  padding: 12px 16px;
  background: var(--canvas);
  border-top: 1px solid var(--border-soft);
}
.replies-list { display: flex; flex-direction: column; gap: 8px; }
.reply-item {
  border-left: 3px solid var(--teal);
  padding: 6px 10px;
  background: var(--surface);
}
.reply-meta {
  display: flex;
  gap: 12px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--muted);
  margin-bottom: 3px;
}
.reply-author { color: var(--ink); font-weight: bold; }
.reply-content { font-size: 12px; color: var(--ink); line-height: 1.5; }
.reply-empty { font-size: 12px; color: var(--muted); font-style: italic; padding: 4px 0; }
.reply-show-more {
  background: none;
  border: 1px solid var(--border-soft);
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--muted);
  cursor: pointer;
  padding: 4px 8px;
  margin-top: 4px;
  letter-spacing: 0.5px;
}
.reply-show-more:hover { border-color: var(--ink); color: var(--ink); }
```

**Step 2: Replace the existing per-post table row with a version that includes the reply button and a hidden replies row**

Find this block in `metrics.html` (around line 277):
```html
        {% for pm in post_metrics %}
        {% set p = pm.post %}
        {% set m = pm.metrics %}
        <tr>
          <td style="color:var(--muted);">{{ loop.index }}</td>
          <td style="white-space:nowrap; font-size:13px; color:var(--muted);">{{ p.scheduled_time.strftime("%Y-%m-%d") }}</td>
          <td><span class="badge badge-{{ p.status }}">{{ p.status }}</span></td>
          <td style="font-size:12px; color:var(--muted);">{{ p.content[:80] }}{% if p.content|length > 80 %}&hellip;{% endif %}</td>
          <td style="text-align:center; font-weight:bold; color:var(--orange);">{{ m.favourites if m else "—" }}</td>
          <td style="text-align:center; font-weight:bold; color:var(--purple);">{{ m.reblogs if m else "—" }}</td>
          <td style="text-align:center; font-weight:bold; color:var(--teal);">{{ m.replies if m else "—" }}</td>
          <td style="text-align:center; font-weight:bold; color:var(--muted);">{{ m.poll_voters if (m and p.poll_options) else "—" }}</td>
        </tr>
        {% endfor %}
```

Replace with:
```html
        {% for pm in post_metrics %}
        {% set p = pm.post %}
        {% set m = pm.metrics %}
        <tr>
          <td style="color:var(--muted);">{{ loop.index }}</td>
          <td style="white-space:nowrap; font-size:13px; color:var(--muted);">{{ p.scheduled_time.strftime("%Y-%m-%d") }}</td>
          <td><span class="badge badge-{{ p.status }}">{{ p.status }}</span></td>
          <td style="font-size:12px; color:var(--muted);">{{ p.content[:80] }}{% if p.content|length > 80 %}&hellip;{% endif %}</td>
          <td style="text-align:center; font-weight:bold; color:var(--orange);">{{ m.favourites if m else "—" }}</td>
          <td style="text-align:center; font-weight:bold; color:var(--purple);">{{ m.reblogs if m else "—" }}</td>
          <td style="text-align:center; font-weight:bold; color:var(--teal);">
            {% if m and m.replies > 0 and p.mastodon_status_id %}
              <button class="reply-toggle" onclick="toggleReplies({{ p.id }}, this)">{{ m.replies }}</button>
            {% else %}
              {{ m.replies if m else "—" }}
            {% endif %}
          </td>
          <td style="text-align:center; font-weight:bold; color:var(--muted);">{{ m.poll_voters if (m and p.poll_options) else "—" }}</td>
        </tr>
        <tr class="replies-row" id="replies-row-{{ p.id }}" style="display:none;">
          <td colspan="8">
            <div class="replies-container" id="replies-{{ p.id }}">
              <span style="font-size:11px; color:var(--muted); font-family:'JetBrains Mono',monospace;">Loading…</span>
            </div>
          </td>
        </tr>
        {% endfor %}
```

**Step 3: Add JS before `{% endblock %}` at the bottom of metrics.html**

```html
<script>
function toggleReplies(postId, btn) {
  const row = document.getElementById('replies-row-' + postId);
  const container = document.getElementById('replies-' + postId);
  if (row.style.display !== 'none') {
    row.style.display = 'none';
    return;
  }
  row.style.display = '';
  if (container.dataset.loaded) return;
  fetch('/posts/' + postId + '/replies')
    .then(r => r.text())
    .then(html => {
      container.innerHTML = html;
      container.dataset.loaded = '1';
    })
    .catch(() => {
      container.innerHTML = '<span style="font-size:11px;color:var(--muted);">Could not load replies.</span>';
    });
}
</script>
```

**Step 4: Verify the template renders without errors by starting the app**

```bash
cd /Users/nicococo/Projects/tootflow && python run.py &
sleep 2 && curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ && kill %1
```

Expected: `200`

**Step 5: Commit**

```bash
git add app/templates/metrics.html
git commit -m "feat(metrics): add reply thread viewer to per-post table"
```
