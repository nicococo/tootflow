import html
import re
import httpx
from typing import Optional
from app.models import MastodonAccount

# Minimal scopes: publish posts + read account info + read status metrics
OAUTH_SCOPES = "write:statuses read:accounts read:statuses"


def get_oauth_url(instance_url: str, client_id: str, redirect_uri: str, state: str) -> str:
    base = instance_url.rstrip("/")
    return (
        f"{base}/oauth/authorize"
        f"?client_id={client_id}"
        f"&scope={OAUTH_SCOPES.replace(' ', '+')}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&state={state}"
    )


async def register_app(instance_url: str, redirect_uri: str) -> dict:
    """Register TootFlow as an OAuth app on the Mastodon instance."""
    base = instance_url.rstrip("/")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base}/api/v1/apps",
            data={
                "client_name": "TootFlow",
                "redirect_uris": redirect_uri,
                "scopes": OAUTH_SCOPES,
                "website": "https://tootflow.app",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def exchange_code_for_token(
    instance_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> str:
    base = instance_url.rstrip("/")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base}/oauth/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code": code,
                "scope": OAUTH_SCOPES,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def get_account_info(instance_url: str, access_token: str) -> dict:
    base = instance_url.rstrip("/")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base}/api/v1/accounts/verify_credentials",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def publish_post(
    account: MastodonAccount,
    content: str,
    poll_options: list[str] | None = None,
    poll_expires_in: int = 86400,
) -> dict:
    base = account.instance_url.rstrip("/")
    body: dict = {"status": content}
    if poll_options and len(poll_options) >= 2:
        body["poll"] = {"options": poll_options, "expires_in": poll_expires_in}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base}/api/v1/statuses",
            headers={"Authorization": f"Bearer {account.access_token}"},
            json=body,
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_status_metrics(account: MastodonAccount, status_id: str) -> dict:
    base = account.instance_url.rstrip("/")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base}/api/v1/statuses/{status_id}",
            headers={"Authorization": f"Bearer {account.access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        poll = data.get("poll") or {}
        return {
            "favourites": data.get("favourites_count", 0),
            "reblogs": data.get("reblogs_count", 0),
            "replies": data.get("replies_count", 0),
            "poll_voters": poll.get("voters_count") or 0,
        }


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()


async def fetch_replies(account: MastodonAccount, status_id: str) -> list[dict]:
    """Fetch direct replies to a published post from the Mastodon context API.
    Returns plain-text content (HTML tags stripped)."""
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
                    "content": _strip_html(s.get("content", "")),
                    "created_at": s.get("created_at", ""),
                })
        return replies
