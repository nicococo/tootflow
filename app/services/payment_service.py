import hashlib
import hmac
import httpx
from typing import Optional
from app.config import get_settings

settings = get_settings()

LEMONSQUEEZY_API_BASE = "https://api.lemonsqueezy.com/v1"


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Verify LemonSqueezy webhook HMAC-SHA256 signature."""
    secret = settings.lemonsqueezy_webhook_secret.encode()
    expected = hmac.new(secret, payload, digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def create_checkout_url(campaign_id: int, user_id: int, user_email: Optional[str] = None) -> str:
    """Create a LemonSqueezy checkout URL for a campaign."""
    headers = {
        "Authorization": f"Bearer {settings.lemonsqueezy_api_key}",
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }

    checkout_options: dict = {
        "custom": {
            "campaign_id": str(campaign_id),
            "user_id": str(user_id),
        },
    }
    if user_email:
        checkout_options["email"] = user_email

    checkout_data = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": checkout_options,
                "product_options": {
                    "redirect_url": f"{settings.app_base_url}/campaigns/{campaign_id}",
                },
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": settings.lemonsqueezy_store_id}},
                "variant": {"data": {"type": "variants", "id": settings.lemonsqueezy_product_variant_id}},
            },
        }
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{LEMONSQUEEZY_API_BASE}/checkouts",
            headers=headers,
            json=checkout_data,
        )
        resp.raise_for_status()
        return resp.json()["data"]["attributes"]["url"]
