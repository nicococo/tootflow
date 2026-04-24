import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlmodel import Session, select

from app.database import get_session
from app.models import Campaign, Payment, Post, User
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


@router.post("/webhooks/polar")
async def polar_webhook(request: Request, session: Session = Depends(get_session)):
    body = await request.body()
    logger.debug(f"Polar webhook received, body length: {len(body)}")

    try:
        event = verify_webhook_signature(body, dict(request.headers))
    except ValueError as e:
        logger.warning(f"Polar webhook verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_class = type(event).__name__
    logger.info(f"Polar webhook event: {event_class}")

    if event_class == "WebhookOrderPaidPayload":
        data = event.data
        metadata = getattr(data, "metadata", {}) or {}
        campaign_id = metadata.get("campaign_id")
        user_id = metadata.get("user_id")

        if not campaign_id or not user_id:
            logger.warning("Polar webhook missing campaign_id/user_id in metadata")
            raise HTTPException(status_code=400, detail="Missing metadata")

        campaign_id = int(campaign_id)
        user_id = int(user_id)

        campaign = session.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        if campaign.user_id != user_id:
            logger.warning(f"Polar webhook user_id mismatch for campaign {campaign_id}")
            raise HTTPException(status_code=400, detail="Invalid webhook data")

        payment = session.exec(select(Payment).where(Payment.campaign_id == campaign_id)).first()
        order_id = str(getattr(data, "id", ""))
        amount = float(getattr(data, "total_amount", 0)) / 100
        currency = (getattr(data, "currency", "EUR") or "EUR").upper()

        if not payment:
            payment = Payment(
                user_id=user_id,
                campaign_id=campaign_id,
                lemonsqueezy_order_id=order_id,
                amount=amount,
                currency=currency,
                status="paid",
            )
            session.add(payment)
        else:
            payment.status = "paid"
            payment.lemonsqueezy_order_id = order_id
            session.add(payment)

        campaign.payment_status = "paid"
        if campaign.status == "draft":
            has_posts = session.exec(
                select(Post).where(Post.campaign_id == campaign_id)
            ).first()
            if has_posts:
                campaign.status = "active"
        campaign.start_date = datetime.utcnow()
        campaign.end_date = datetime.utcnow() + timedelta(days=campaign.duration_days)
        session.add(campaign)
        session.commit()
        logger.info(f"Campaign {campaign_id} activated via Polar order {order_id}")

    elif event_class == "WebhookRefundCreatedPayload":
        data = event.data
        order_id = str(getattr(data, "order_id", ""))
        payment = session.exec(
            select(Payment).where(Payment.lemonsqueezy_order_id == order_id)
        ).first()
        if payment:
            campaign = session.get(Campaign, payment.campaign_id)
            if campaign:
                campaign.payment_status = "unpaid"
                campaign.status = "draft"
                campaign.enabled = False
                session.add(campaign)
            payment.status = "refunded"
            session.add(payment)
            session.commit()
            logger.info(f"Campaign {payment.campaign_id} revoked due to Polar refund")
    else:
        return JSONResponse({"status": "ignored"})

    return JSONResponse({"status": "ok"})
