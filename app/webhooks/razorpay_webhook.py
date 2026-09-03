from typing import Optional
from fastapi import APIRouter, Request, Header, Depends, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db.database import get_db
from app.webhooks.webhook_service import webhook_service

router = APIRouter(prefix="/webhooks/razorpay", tags=["Razorpay Webhooks"])

@router.post("", status_code=status.HTTP_200_OK)
@router.post("/", status_code=status.HTTP_200_OK)
async def receive_razorpay_webhook(
    request: Request,
    x_razorpay_signature: Optional[str] = Header(None, alias="X-Razorpay-Signature"),
    x_razorpay_event_id: Optional[str] = Header(None, alias="X-Razorpay-Event-Id"),
    db: Session = Depends(get_db)
):
    """
    Ingests and securely validates Razorpay Test Mode webhook events.
    Uses the raw request body bytes for HMAC-SHA256 signature verification.
    """
    raw_body = await request.body()
    result = webhook_service.process_webhook_event(
        raw_body=raw_body,
        signature=x_razorpay_signature,
        event_id_header=x_razorpay_event_id,
        db=db
    )
    return result

@router.get("/health", status_code=status.HTTP_200_OK)
def razorpay_webhook_health():
    """
    Health check endpoint for Razorpay webhook configuration.
    Zero secrets or credentials are ever exposed.
    """
    return {
        "status": "ok",
        "service": "Revenue Recovery Agent Razorpay Webhook Ingestion",
        "razorpay_webhook_configured": bool(settings.RAZORPAY_WEBHOOK_SECRET),
        "supported_events": [
            "payment.failed",
            "payment.authorized",
            "payment.captured",
            "order.paid"
        ]
    }
