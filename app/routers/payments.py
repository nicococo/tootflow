import json
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlmodel import Session, select

from app.database import get_session
from app.models import Campaign, Payment, User
from app.auth import get_current_user
from app.csrf import verify_csrf
from app.services.payment_service import create_checkout_url, verify_webhook_signature

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/campaigns/{campaign_id}/activate")
async def activate_campaign(
    campaign_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(status_code=404)
    if campaign.payment_status == "paid":
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)

    try:
        checkout_url = await create_checkout_url(campaign_id, user.id)
    except Exception as e:
        logger.error(f"Payment service error for campaign {campaign_id}: {e}")
        raise HTTPException(status_code=500, detail="Payment service unavailable")

    return RedirectResponse(checkout_url, status_code=302)


@router.post("/webhooks/lemonsqueezy")
async def lemonsqueezy_webhook(request: Request, session: Session = Depends(get_session)):
    body = await request.body()
    signature = request.headers.get("X-Signature", "")
    logger.debug(f"Webhook received — event signature present: {bool(signature)}, body length: {len(body)}")

    if not verify_webhook_signature(body, signature):
        logger.warning("Webhook signature mismatch")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info(f"Webhook event: {payload.get('meta', {}).get('event_name', 'unknown')}")

    event_name = payload.get("meta", {}).get("event_name", "")
    if event_name not in ("order_created", "order_refunded"):
        return JSONResponse({"status": "ignored"})

    data = payload.get("data", {})
    attributes = data.get("attributes", {})
    custom_data = payload.get("meta", {}).get("custom_data", {})

    campaign_id = custom_data.get("campaign_id")
    user_id = custom_data.get("user_id")

    if not campaign_id or not user_id:
        raise HTTPException(status_code=400, detail="Missing custom data")

    campaign_id = int(campaign_id)
    user_id = int(user_id)

    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Verify campaign belongs to the claimed user
    if campaign.user_id != user_id:
        logger.warning(f"Webhook user_id mismatch for campaign {campaign_id}")
        raise HTTPException(status_code=400, detail="Invalid webhook data")

    if event_name == "order_refunded":
        # Revoke access and stop publishing
        campaign.payment_status = "unpaid"
        campaign.status = "draft_unpaid"
        campaign.enabled = False
        session.add(campaign)
        payment = session.exec(select(Payment).where(Payment.campaign_id == campaign_id)).first()
        if payment:
            payment.status = "refunded"
            session.add(payment)
        session.commit()
        logger.info(f"Campaign {campaign_id} revoked due to refund")
        return JSONResponse({"status": "ok"})

    # order_created — record payment and activate campaign
    payment = session.exec(select(Payment).where(Payment.campaign_id == campaign_id)).first()
    if not payment:
        payment = Payment(
            user_id=user_id,
            campaign_id=campaign_id,
            lemonsqueezy_order_id=str(data.get("id", "")),
            amount=float(attributes.get("total", 0)) / 100,
            currency=attributes.get("currency", "EUR").upper(),
            status="paid",
        )
        session.add(payment)
    else:
        payment.status = "paid"
        payment.lemonsqueezy_order_id = str(data.get("id", ""))
        session.add(payment)

    campaign.payment_status = "paid"
    campaign.status = "active"
    campaign.start_date = datetime.utcnow()
    campaign.end_date = datetime.utcnow() + timedelta(days=campaign.duration_days)
    session.add(campaign)
    session.commit()

    return JSONResponse({"status": "ok"})
