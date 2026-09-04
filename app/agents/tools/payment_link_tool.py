import uuid
import logging
from typing import Dict, Any, Optional
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult, RootCause, DiagnosisSource
from app.schemas.agent_state import AgentToolName, ToolResult
from app.agents.tools.base_tool import BaseTool
from app.integrations.razorpay.payment_links import (
    RazorpayPaymentLinkAdapter,
    razorpay_payment_links,
)

logger = logging.getLogger(__name__)

class PaymentLinkTool(BaseTool):
    """
    Phase 6 Real Recovery Tool:
    Creates a real Razorpay Test-Mode Payment Link via Razorpay REST API.
    Enforces strict revenue accounting:
      Link creation -> recovery_pending (NOT recovered!).
      Revenue is counted only upon verified payment.captured / order.paid webhook.
    """
    name = AgentToolName.PAYMENT_LINK
    description = (
        "Creates an authentic Razorpay Test Mode Payment Link (https://rzp.io/i/...) "
        "for the failed transaction amount, setting recovery state to recovery_pending."
    )

    def __init__(self, adapter: Optional[RazorpayPaymentLinkAdapter] = None):
        self.adapter = adapter or razorpay_payment_links

    def execute(
        self,
        txn: Transaction,
        diagnosis: Optional[DiagnosisResult] = None,
        **kwargs: Any
    ) -> ToolResult:
        call_id = kwargs.get("call_id", f"call_plink_{uuid.uuid4().hex[:8]}")
        agent_run_id = kwargs.get("agent_run_id")
        db = kwargs.get("db")
        failure_reason = kwargs.get("failure_reason") or (diagnosis.root_cause.value if diagnosis else None)
        preferred_methods = kwargs.get("preferred_methods")
        use_upi_intent = kwargs.get("use_upi_intent", False)
        custom_desc = kwargs.get("description") or f"Revenue Recovery Payment Link for Txn {txn.transaction_id}"

        logger.info(
            f"[PAYMENT_LINK_TOOL] Executing PaymentLinkTool for txn: {txn.transaction_id} "
            f"(INR {txn.amount:,.2f}) | reason={failure_reason} | methods={preferred_methods}"
        )

        res = self.adapter.create_payment_link(
            amount_inr=txn.amount,
            transaction_id=txn.transaction_id,
            currency=txn.currency,
            agent_run_id=agent_run_id,
            customer_name=txn.customer_name,
            customer_email=txn.customer_email,
            customer_contact=txn.customer_phone,
            description=custom_desc,
            failure_reason=failure_reason,
            preferred_methods=preferred_methods,
            use_upi_intent=use_upi_intent,
            db=db
        )

        if res.get("success"):
            plink_id = res.get("payment_link_id")
            plink_url = res.get("payment_link_url")
            is_reused = res.get("is_idempotent_reuse", False)

            status_msg = "Reused active" if is_reused else "Created new"
            notes = f"{status_msg} Razorpay Test Payment Link: {plink_id} ({plink_url}). Recovery pending customer payment."

            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                success=True,
                status="recovery_pending",
                result_data={
                    "payment_link_id": plink_id,
                    "payment_link_url": plink_url,
                    "status": "recovery_pending",
                    "is_idempotent_reuse": is_reused,
                    "amount": txn.amount,
                    "currency": txn.currency,
                    "failure_reason": failure_reason,
                    "preferred_methods": preferred_methods,
                    "use_upi_intent": use_upi_intent,
                    "execution_notes": notes
                },
                error_reason=None,
                recovered_amount=0.0 # Strict revenue accounting: 0.0 until payment.captured
            )
        else:
            err_msg = res.get("error_message") or "Failed to create Razorpay Payment Link."
            logger.error(f"[PAYMENT_LINK_TOOL] Link creation failed for {txn.transaction_id}: {err_msg}")

            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                success=False,
                status="failed",
                result_data={
                    "status": "failed",
                    "error_message": err_msg,
                    "amount": txn.amount
                },
                error_reason=err_msg,
                recovered_amount=0.0
            )
