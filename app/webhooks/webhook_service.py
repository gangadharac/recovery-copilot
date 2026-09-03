import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db.database import SessionLocal, init_db
from app.db.models import WebhookEventModel, TransactionModel, AuditLogModel, RecoveryAttemptModel
from app.webhooks.webhook_security import verify_razorpay_signature, compute_payload_hash
from app.webhooks.recovery_trigger import recovery_trigger_service

logger = logging.getLogger(__name__)

class WebhookService:
    """
    Service layer for ingesting, validating, deduplicating, and correlating
    Razorpay Test Mode webhook events, and triggering the autonomous RecoveryAgent.
    """
    def __init__(self):
        init_db()

    def process_webhook_event(
        self,
        raw_body: bytes,
        signature: Optional[str],
        event_id_header: Optional[str] = None,
        db: Optional[Session] = None,
        webhook_secret_override: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Processes a raw webhook request:
        1. Validates HMAC-SHA256 signature against raw request bytes.
        2. Computes payload SHA-256 hash.
        3. Parses and validates JSON.
        4. Enforces idempotency (duplicate event detection).
        5. Persists webhook event audit record.
        6. Normalizes payment/order state (payment.failed, payment.captured, order.paid).
        7. On payment.failed, triggers the autonomous RecoveryAgent loop.
        """
        # Determine secret to validate against
        secret = webhook_secret_override or settings.RAZORPAY_WEBHOOK_SECRET
        if not secret:
            logger.warning("RAZORPAY_WEBHOOK_SECRET not configured. Rejecting unauthenticated webhook.")
            raise HTTPException(status_code=401, detail="Webhook secret not configured on server")

        # 1. Signature Verification
        if not signature:
            logger.warning("Rejected webhook request: Missing X-Razorpay-Signature header.")
            raise HTTPException(status_code=401, detail="Missing X-Razorpay-Signature header")

        if not verify_razorpay_signature(raw_body, signature, secret):
            logger.warning("Rejected webhook request: Invalid X-Razorpay-Signature.")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        logger.info("[WEBHOOK] Signature validated successfully.")

        # 2. Compute Payload Hash
        payload_hash = compute_payload_hash(raw_body)

        # 3. Parse JSON Body
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to parse webhook JSON body: {e}")
            raise HTTPException(status_code=400, detail="Malformed JSON payload")

        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid payload: expected JSON object")

        event_type = payload.get("event", "unknown")
        logger.info(f"[WEBHOOK] Event identified: {event_type}")

        # 4. Extract Event Identifiers
        event_id = (
            payload.get("event_id") or
            payload.get("id") or
            event_id_header or
            f"evt_{payload_hash[:16]}"
        )

        # Extract payment / order entities if present
        payload_data = payload.get("payload", {})
        payment_entity = payload_data.get("payment", {}).get("entity", {})
        order_entity = payload_data.get("order", {}).get("entity", {})

        payment_id = payment_entity.get("id")
        order_id = payment_entity.get("order_id") or order_entity.get("id")

        session_provided = db is not None
        db_session = db if session_provided else SessionLocal()

        try:
            # 5. Idempotency / Duplicate Check
            existing_event = db_session.query(WebhookEventModel).filter(
                (WebhookEventModel.event_id == event_id) |
                ((WebhookEventModel.payload_hash == payload_hash) & (WebhookEventModel.event_type == event_type))
            ).first()

            if existing_event:
                logger.info(f"[WEBHOOK] Duplicate event detected: event_id={event_id}. Skipping processing.")
                return {
                    "status": "duplicate_ignored",
                    "event_id": event_id,
                    "event_type": event_type,
                    "agent_triggered": False,
                    "message": "Duplicate event already processed"
                }

            # 6. Persist Webhook Event Record
            db_event = WebhookEventModel(
                id=f"wh_{uuid.uuid4().hex[:12]}",
                event_id=event_id,
                event_type=event_type,
                payment_id=payment_id,
                order_id=order_id,
                received_at=datetime.now(timezone.utc),
                processed_at=datetime.now(timezone.utc),
                status="processed",
                payload_json=payload,
                payload_hash=payload_hash,
                error_message=None
            )
            db_session.add(db_event)

            # 7. Event-Specific Processing & Autonomous Agent Trigger
            agent_result = None
            if event_type == "payment.failed":
                agent_result = self._handle_payment_failed(payment_entity, order_id, event_id, db_session)
            elif event_type in ["payment.captured", "order.paid"]:
                self._handle_payment_success(event_type, payment_entity, order_entity, db_session)
            elif event_type == "payment.authorized":
                self._handle_payment_authorized(payment_entity, db_session)
            else:
                logger.info(f"Acknowledged unhandled Razorpay event type: '{event_type}'.")
                db_event.status = "ignored"

            db_session.commit()

            response_data = {
                "status": "success",
                "event_id": event_id,
                "event_type": event_type,
                "payment_id": payment_id,
                "order_id": order_id,
                "message": f"Successfully processed {event_type} event"
            }

            if agent_result:
                response_data["agent_triggered"] = True
                response_data["agent_status"] = agent_result.final_status
                response_data["agent_action"] = agent_result.final_action
                response_data["iterations_used"] = agent_result.iterations_used
            else:
                response_data["agent_triggered"] = False

            return response_data

        except HTTPException:
            db_session.rollback()
            raise
        except Exception as e:
            db_session.rollback()
            logger.error(f"Error processing webhook event {event_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Internal webhook processing error: {str(e)}")
        finally:
            if not session_provided:
                db_session.close()

    def _handle_payment_failed(
        self,
        payment_entity: Dict[str, Any],
        order_id: Optional[str],
        event_id: str,
        db: Session
    ) -> Any:
        """
        Normalizes payment.failed event and triggers the autonomous RecoveryAgent.
        """
        payment_id = payment_entity.get("id", f"pay_fail_{uuid.uuid4().hex[:8]}")
        raw_amount = payment_entity.get("amount", 0)
        amount_inr = float(raw_amount) / 100.0 if raw_amount else 100.0

        currency = payment_entity.get("currency", "INR")
        method = payment_entity.get("method", "card")
        error_code = payment_entity.get("error_code") or payment_entity.get("error_reason") or "GATEWAY_ERROR"
        error_desc = payment_entity.get("error_description") or "Payment failed during transaction processing"
        error_source = payment_entity.get("error_source", "bank_switch")
        customer_email = payment_entity.get("email")
        customer_phone = payment_entity.get("contact")

        # Upsert TransactionModel
        txn = db.query(TransactionModel).filter(TransactionModel.transaction_id == payment_id).first()
        if txn and txn.lifecycle_status in ["captured", "paid"]:
            logger.info(
                f"[WEBHOOK] Late payment.failed event received for already {txn.lifecycle_status} payment {payment_id}. Skipping recovery."
            )
            return None

        if not txn:
            txn = TransactionModel(
                transaction_id=payment_id,
                payment_id=payment_id,
                order_id=order_id,
                customer_id=f"cust_{payment_id[:10]}",
                customer_name=payment_entity.get("notes", {}).get("customer_name") or "Razorpay Customer",
                customer_phone=customer_phone,
                customer_email=customer_email,
                amount=amount_inr,
                currency=currency,
                payment_method=method,
                error_code=error_code,
                error_description=error_desc,
                error_source=error_source,
                retry_count=0,
                auto_charge_consent=False,
                raw_payload=payment_entity,
                lifecycle_status="payment_failed",
                created_at=datetime.now(timezone.utc)
            )
            db.add(txn)
        else:
            txn.lifecycle_status = "payment_failed"
            txn.error_code = error_code
            txn.error_description = error_desc

        db.flush()
        logger.info(f"[WEBHOOK] Payment failure stored for payment_id: {payment_id} (INR {amount_inr:,.2f})")

        # Trigger Autonomous RecoveryAgent
        agent_res = recovery_trigger_service.trigger_recovery_from_webhook(
            payment_entity=payment_entity,
            order_id=order_id,
            event_id=event_id,
            db=db
        )
        return agent_res

    def _handle_payment_success(
        self,
        event_type: str,
        payment_entity: Dict[str, Any],
        order_entity: Dict[str, Any],
        db: Session
    ) -> None:
        """
        Correlates payment.captured or order.paid with previous payment.failed records,
        and marks recovery as verified in the audit trail.
        """
        payment_id = payment_entity.get("id")
        order_id = payment_entity.get("order_id") or order_entity.get("id")

        # Correlate by payment_id, order_id, or payment_link_id
        matched_txn = None
        if payment_id:
            matched_txn = db.query(TransactionModel).filter(
                (TransactionModel.payment_id == payment_id) | (TransactionModel.transaction_id == payment_id)
            ).first()

        if not matched_txn and order_id:
            matched_txn = db.query(TransactionModel).filter(TransactionModel.order_id == order_id).first()

        plink_id = payment_entity.get("payment_link_id") or payment_entity.get("notes", {}).get("payment_link_id")
        if not matched_txn and plink_id:
            attempt = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.payment_link_id == plink_id).first()
            if attempt:
                matched_txn = db.query(TransactionModel).filter(TransactionModel.transaction_id == attempt.transaction_id).first()

        if matched_txn:
            matched_txn.lifecycle_status = "captured" if event_type == "payment.captured" else "paid"
            logger.info(
                f"[VERIFY] Event {event_type} successfully correlated with Transaction {matched_txn.transaction_id} (Order: {order_id})"
            )

            # Phase 6: Correlate and verify in RecoveryAttemptModel
            recovery_attempts = db.query(RecoveryAttemptModel).filter(
                (RecoveryAttemptModel.transaction_id == matched_txn.transaction_id) |
                (RecoveryAttemptModel.payment_link_id == plink_id)
            ).all()

            for attempt in recovery_attempts:
                if attempt.status != "recovered":
                    attempt.status = "recovered"
                    attempt.recovered_at = datetime.now(timezone.utc)
                    logger.info(
                        f"[RECOVERY] Payment Link {attempt.payment_link_id} verified as RECOVERED "
                        f"for transaction {matched_txn.transaction_id}."
                    )

            # Correlate and verify in AuditLogModel
            audit_logs = db.query(AuditLogModel).filter(
                (AuditLogModel.transaction_id == matched_txn.transaction_id)
            ).all()

            for audit in audit_logs:
                audit.execution_status = "recovered"
                audit.recovered = True
                audit.recovered_amount = matched_txn.amount
                audit.execution_notes = f"{audit.execution_notes or ''} | [RECOVERY VERIFIED via {event_type} webhook]".strip(" |")
                logger.info(f"[RECOVERY] Recovery verified in audit log for transaction: {matched_txn.transaction_id}")
        else:
            logger.info(
                f"[VERIFY] Event {event_type} recorded (Payment: {payment_id}, Order: {order_id}) - no prior failed transaction found."
            )

    def _handle_payment_authorized(
        self,
        payment_entity: Dict[str, Any],
        db: Session
    ) -> None:
        """
        Updates status for payment.authorized event.
        """
        payment_id = payment_entity.get("id")
        if payment_id:
            txn = db.query(TransactionModel).filter(
                (TransactionModel.payment_id == payment_id) | (TransactionModel.transaction_id == payment_id)
            ).first()
            if txn:
                txn.lifecycle_status = "authorized"
                logger.info(f"[VERIFY] Payment {payment_id} status updated to authorized.")

# Global WebhookService singleton
webhook_service = WebhookService()
