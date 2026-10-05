"""X.com (Twitter) API service — OAuth 1.0a user-context auth.

Uses the X API v2 for publishing. X Free tier is write-only, so no
metrics/read endpoints are implemented here.

OAuth 1.0a signing is hand-rolled with stdlib only (no new dependencies):
HMAC-SHA1 signature over the signature base string. For requests with a
JSON body, only query params and oauth_* params are included in the
signature — body params are NOT signed (per RFC 5849).
"""
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, urlencode

import httpx

from app.config import get_settings
from app.models import XAccount

API_BASE = "https://api.x.com"


def _pct(s: str) -> str:
    """Percent-encode per RFC 3986 (OAuth 1.0a spec)."""
    return quote(str(s), safe="~")


def _oauth1_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    token: str | None = None,
    token_secret: str | None = None,
    extra_params: dict | None = None,
) -> str:
    """Build the OAuth 1.0a Authorization header for a request.

    extra_params: additional params to include in the signature (query params
    or form body params). For JSON request bodies, pass only query params.
    """
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    if token:
        oauth_params["oauth_token"] = token

    sig_params = {**oauth_params, **(extra_params or {})}
    param_string = "&".join(
        f"{_pct(k)}={_pct(sig_params[k])}" for k in sorted(sig_params)
    )
    base_string = "&".join([method.upper(), _pct(url), _pct(param_string)])
    signing_key = f"{_pct(consumer_secret)}&{_pct(token_secret or '')}"
    signature = hmac.new(
        signing_key.encode(), base_string.encode(), hashlib.sha1
    ).digest()
    import base64
    oauth_params["oauth_signature"] = base64.b64encode(signature).decode()

    return "OAuth " + ", ".join(
        f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth_params.items())
    )


async def get_request_token(callback_url: str) -> dict:
    """Step 1 of 3-legged OAuth: obtain a request token."""
    settings = get_settings()
    url = f"{API_BASE}/oauth/request_token"
    header = _oauth1_header(
        "POST", url,
        settings.x_api_key, settings.x_api_secret,
        extra_params={"oauth_callback": callback_url},
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers={"Authorization": header})
        resp.raise_for_status()
        # Response is form-encoded: oauth_token=...&oauth_token_secret=...&oauth_callback_confirmed=true
        return dict(
            pair.split("=", 1) for pair in resp.text.split("&") if "=" in pair
        )


def get_authorize_url(oauth_token: str) -> str:
    """Step 2: URL to send the user to for authorization."""
    return f"{API_BASE}/oauth/authorize?oauth_token={_pct(oauth_token)}"


async def exchange_verifier(
    oauth_token: str, oauth_token_secret: str, oauth_verifier: str
) -> dict:
    """Step 3: exchange the verifier for a long-lived access token pair.

    Returns dict with oauth_token, oauth_token_secret, user_id, screen_name.
    """
    settings = get_settings()
    url = f"{API_BASE}/oauth/access_token"
    header = _oauth1_header(
        "POST", url,
        settings.x_api_key, settings.x_api_secret,
        token=oauth_token, token_secret=oauth_token_secret,
        extra_params={"oauth_verifier": oauth_verifier},
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers={"Authorization": header})
        resp.raise_for_status()
        return dict(
            pair.split("=", 1) for pair in resp.text.split("&") if "=" in pair
        )


async def publish_post(account: XAccount, content: str) -> dict:
    """Publish a tweet via X API v2. Returns the response 'data' object (has 'id').

    account.access_token / access_token_secret must already be decrypted.
    Polls are not supported by the X API — callers must not pass them.
    """
    settings = get_settings()
    url = f"{API_BASE}/2/tweets"
    header = _oauth1_header(
        "POST", url,
        settings.x_api_key, settings.x_api_secret,
        token=account.access_token, token_secret=account.access_token_secret,
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers={"Authorization": header},
            json={"text": content},
        )
        resp.raise_for_status()
        return resp.json().get("data", {})
