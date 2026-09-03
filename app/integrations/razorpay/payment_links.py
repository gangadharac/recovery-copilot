import uuid
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import requests
from sqlalchemy.orm import Session

from app.config import settings
from app.db.database import SessionLocal
from app.db.models import RecoveryAttemptModel
from app.integrations.razorpay.client import RazorpayClient, razorpay_client

logger = logging.getLogger(__name__)

# Known mock / placeholder customer values that should NOT be sent as genuine customer info
PLACEHOLDER_CONTACTS = {"+919876543210", "+919988776655", "+919876543211", "+919876543212", ""}
PLACEHOLDER_EMAILS = {"customer@example.com", "test@example.com", "arnav.m@example.com", ""}
PLACEHOLDER_NAMES = {"Razorpay Customer", "Razorpay Customer (Demo)", "Acme Retail Customer", ""}

class RazorpayPaymentLinkAdapter:
    """
    Adapter for creating and managing Razorpay Test Mode Payment Links.
    Enforces amount conversion (INR -> paise), customer data integrity (zero fabrication),
    test-mode safety, application-level idempotency, and SQLite audit persistence.
    """

    def __init__(self, client: Optional[RazorpayClient] = None):
        self.client = client or razorpay_client

    def create_payment_link(
        self,
        amount_inr: float,
        transaction_id: str,
        currency: str = "INR",
        agent_run_id: Optional[str] = None,
        customer_name: Optional[str] = None,
        customer_email: Optional[str] = None,
        customer_contact: Optional[str] = None,
        description: Optional[str] = None,
        notes: Optional[Dict[str, Any]] = None,
        db: Optional[Session] = None
    ) -> Dict[str, Any]:
        """
        Creates a Razorpay Test Mode Payment Link for a failed transaction.
        Checks for an existing active payment link first to enforce idempotency.
        """
        # 1. Credentials & Test Mode Safety Validation
        if not self.client.is_configured:
            logger.warning("[RAZORPAY_PLINK] Cannot create payment link: Missing API key or secret.")
            return {
                "success": False,
                "status": "failed",
                "error_message": "Razorpay API credentials (KEY_ID / KEY_SECRET) not configured."
            }

        try:
            self.client.validate_safety()
        except ValueError as val_err:
            logger.error(f"[RAZORPAY_PLINK] Safety validation failed: {val_err}")
            return {
                "success": False,
                "status": "failed",
                "error_message": str(val_err)
            }

        # 2. Amount & Currency Validation
        try:
            val_amount = float(amount_inr)
            if val_amount <= 0:
                raise ValueError("Amount must be greater than zero")
            amount_paise = int(round(val_amount * 100))
        except (ValueError, TypeError) as amt_err:
            logger.error(f"[RAZORPAY_PLINK] Invalid amount {amount_inr}: {amt_err}")
            return {
                "success": False,
                "status": "failed",
                "error_message": f"Invalid amount: {str(amt_err)}"
            }

        curr = (currency or "INR").upper()

        session_provided = db is not None
        db_session = db if session_provided else SessionLocal()

        try:
            # 3. Idempotency Check: Do not create a second link if an active attempt exists
            existing_attempt = db_session.query(RecoveryAttemptModel).filter(
                RecoveryAttemptModel.transaction_id == transaction_id,
                RecoveryAttemptModel.status.in_(["pending", "created"])
            ).first()

            if existing_attempt and existing_attempt.payment_link_id:
                logger.info(
                    f"[IDEMPOTENCY] Active payment link {existing_attempt.payment_link_id} "
                    f"already exists for transaction {transaction_id}. Returning existing link."
                )
                return {
                    "success": True,
                    "status": "recovery_pending",
                    "payment_link_id": existing_attempt.payment_link_id,
                    "payment_link_url": existing_attempt.payment_link_url,
                    "amount": existing_attempt.amount,
                    "currency": existing_attempt.currency,
                    "is_idempotent_reuse": True,
                    "recovery_attempt_id": existing_attempt.id
                }

            # 4. Construct Customer Payload (No Fake/Fabricated Customer Data)
            customer_payload: Dict[str, str] = {}
            if customer_name and customer_name.strip() not in PLACEHOLDER_NAMES:
                customer_payload["name"] = customer_name.strip()
            if customer_email and customer_email.strip() not in PLACEHOLDER_EMAILS and "@" in customer_email:
                customer_payload["email"] = customer_email.strip()
            if customer_contact and customer_contact.strip() not in PLACEHOLDER_CONTACTS:
                customer_payload["contact"] = customer_contact.strip()

            # 5. Build Razorpay API Request Payload
            req_notes = {
                "transaction_id": transaction_id,
                "agent_run_id": agent_run_id or "",
                "recovery_engine": "Revenue Recovery Agent (Phase 6 Test Mode)"
            }
            if notes:
                req_notes.update(notes)

            payload = {
                "amount": amount_paise,
                "currency": curr,
                "description": description or f"Recovery Payment for Transaction {transaction_id}",
                "reference_id": f"rec_{transaction_id[:20]}_{uuid.uuid4().hex[:6]}",
                "notes": req_notes
            }

            if customer_payload:
                payload["customer"] = customer_payload

            # 6. Execute Request via HTTP POST
            endpoint = f"{self.client.BASE_URL}/payment_links"
            auth = self.client.get_auth()
            headers = self.client.get_headers()

            logger.info(
                f"[RAZORPAY_PLINK] Creating Payment Link for txn {transaction_id}: "
                f"INR {val_amount:,.2f} ({amount_paise} paise)"
            )

            response = requests.post(
                endpoint,
                json=payload,
                auth=auth,
                headers=headers,
                timeout=10.0
            )

            # 7. Process API Response
            if response.status_code in [200, 201]:
                data = response.json()
                plink_id = data.get("id")
                plink_url = data.get("short_url") or data.get("url")

                logger.info(
                    f"[RAZORPAY_PLINK] Created Test Payment Link: id={plink_id}, "
                    f"url={plink_url}, status=recovery_pending"
                )

                # Persist RecoveryAttemptModel record
                attempt_id = f"rec_attempt_{uuid.uuid4().hex[:12]}"
                attempt = RecoveryAttemptModel(
                    id=attempt_id,
                    transaction_id=transaction_id,
                    agent_run_id=agent_run_id,
                    payment_link_id=plink_id,
                    payment_link_url=plink_url,
                    status="pending",
                    amount=val_amount,
                    currency=curr,
                    customer_id=req_notes.get("customer_id"),
                    customer_contact=customer_payload.get("contact"),
                    customer_email=customer_payload.get("email"),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc)
                )
                db_session.add(attempt)
                db_session.commit()

                return {
                    "success": True,
                    "status": "recovery_pending",
                    "payment_link_id": plink_id,
                    "payment_link_url": plink_url,
                    "amount": val_amount,
                    "currency": curr,
                    "is_idempotent_reuse": False,
                    "recovery_attempt_id": attempt_id,
                    "raw_response": {
                        "id": plink_id,
                        "short_url": plink_url,
                        "status": data.get("status")
                    }
                }

            else:
                try:
                    err_json = response.json()
                    err_desc = err_json.get("error", {}).get("description") or response.text
                except Exception:
                    err_desc = response.text

                logger.error(
                    f"[RAZORPAY_PLINK] API Error ({response.status_code}) for txn {transaction_id}: {err_desc}"
                )
                return {
                    "success": False,
                    "status": "failed",
                    "error_message": f"Razorpay API error ({response.status_code}): {err_desc}"
                }

        except requests.exceptions.Timeout:
            logger.error(f"[RAZORPAY_PLINK] Request timed out while creating link for txn {transaction_id}")
            return {
                "success": False,
                "status": "failed",
                "error_message": "Razorpay API request timed out after 10.0s"
            }
        except requests.exceptions.RequestException as req_err:
            logger.error(f"[RAZORPAY_PLINK] Network error while creating link for txn {transaction_id}: {req_err}")
            return {
                "success": False,
                "status": "failed",
                "error_message": f"Network error during Razorpay API call: {str(req_err)}"
            }
        except Exception as e:
            logger.error(f"[RAZORPAY_PLINK] Unexpected error for txn {transaction_id}: {e}", exc_info=True)
            return {
                "success": False,
                "status": "failed",
                "error_message": f"Unexpected error: {str(e)}"
            }
        finally:
            if not session_provided:
                db_session.close()

# Global singleton
razorpay_payment_links = RazorpayPaymentLinkAdapter()
