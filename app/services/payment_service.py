from polar_sdk import Polar
from polar_sdk.webhooks import validate_event, WebhookVerificationError

from app.config import get_settings

settings = get_settings()


def _client() -> Polar:
    return Polar(
        access_token=settings.polar_access_token,
        server=settings.polar_server,
    )


def verify_webhook_signature(payload: bytes, headers: dict) -> object:
    """Verify Polar webhook signature and return the parsed event, or raise on failure."""
    try:
        return validate_event(
            body=payload,
            headers=headers,
            secret=settings.polar_webhook_secret,
        )
    except WebhookVerificationError as e:
        raise ValueError(f"Webhook verification failed: {e}") from e


async def create_checkout_url(campaign_id: int, user_id: int) -> str:
    """Create a Polar checkout URL for a campaign."""
    client = _client()
    result = client.checkouts.create(request={
        "products": [settings.polar_product_id],
        "metadata": {
            "campaign_id": str(campaign_id),
            "user_id": str(user_id),
        },
        "success_url": f"{settings.app_base_url.rstrip('/')}/campaigns/{campaign_id}",
    })
    return result.url
