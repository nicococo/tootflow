# Reply Tracking Design

**Date:** 2026-03-13
**Status:** Approved

## Summary

Add the ability to view Mastodon replies to published posts directly within the TootFlow metrics page. Replies are fetched on demand (user-triggered) to avoid hammering Mastodon instance APIs.

## Approach

Option A: server round-trip on demand. Clicking "show replies" fires a `fetch()` to a new FastAPI endpoint, which calls the Mastodon context API and returns a rendered HTML fragment. No new dependencies.

## Components

### 1. `mastodon_service.py` — new function
`fetch_replies(account, status_id) -> list[dict]`
- Calls `GET /api/v1/statuses/{status_id}/context`
- Filters to direct replies only: `in_reply_to_id == status_id`
- Returns list of `{account_name, content, created_at}`
- Uses existing `httpx.AsyncClient` pattern

### 2. `app/routers/metrics.py` — new endpoint
`GET /posts/{post_id}/replies`
- Auth-guarded (current user must own the post's campaign)
- Fetches post + associated MastodonAccount
- Decrypts access token via `app/crypto.py`
- Calls `fetch_replies`
- Returns rendered HTML fragment via `TemplateResponse("replies_fragment.html")`

### 3. `app/templates/metrics.html` — changes
- Post rows where `m.replies > 0`: reply count becomes a clickable `<button>`
- Each row gets a hidden `<div id="replies-{post_id}">` placeholder beneath it
- ~10 lines of vanilla JS handle: fetch on click, innerHTML swap, loading state, toggle visibility
- First 3 replies visible by default; remaining hidden behind "show N more" button (pure JS, no second request)

### 4. `app/templates/replies_fragment.html` — new partial
- Renders reply list: username (`@account@instance`), content, relative timestamp
- Uses existing brutalist CSS variables (`--ink`, `--muted`, `--border`, `--surface` etc.)
- No new CSS classes needed beyond minimal inline styles

## Data Flow

```
User clicks "💬 3 replies"
  → fetch("/posts/{id}/replies")
    → metrics router: auth check → get post → decrypt token
      → mastodon_service.fetch_replies()
        → GET /api/v1/statuses/{id}/context (Mastodon API)
      → filter direct replies
      → render replies_fragment.html
  → swap innerHTML of placeholder div
  → JS shows first 3, hides rest
  → "show N more" toggles remainder
```

## Constraints

- Only shown for posts with `m.replies > 0` — no button rendered if no replies
- Only direct replies (`in_reply_to_id == mastodon_status_id`) shown — not replies-to-replies
- No reply content stored in DB — always fetched live
- Rate limit friendly: one API call per user click, never on page load
