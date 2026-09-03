import uuid
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session

from app.db.models import TransactionModel, AuditLogModel, AgentTraceModel
from app.schemas.transaction import (
    Transaction,
    PaymentMethod,
    PaymentMethodDetails,
    CustomerHistory
)
from app.agents.recovery_agent import recovery_agent, RecoveryAgentResult

logger = logging.getLogger(__name__)

class RecoveryTriggerService:
    """
    Bridge service connecting real Razorpay Test Mode webhook failure events
    to the autonomous RecoveryAgent.
    
    Responsibilities:
    - Pre-checks transaction lifecycle (prevents duplicate recovery on resolved payments).
    - Safely maps Razorpay webhook payload into the existing Transaction schema.
    - Triggers the existing autonomous RecoveryAgent loop (simulated execution).
    - Persists Agent traces (one per iteration) and Audit Log summary into SQLite.
    """

    def trigger_recovery_from_webhook(
        self,
        payment_entity: Dict[str, Any],
        order_id: Optional[str],
        event_id: str,
        db: Session
    ) -> Optional[RecoveryAgentResult]:
        """
        Triggers the autonomous RecoveryAgent on a payment.failed webhook event.
        """
        payment_id = payment_entity.get("id") or f"pay_wh_{uuid.uuid4().hex[:8]}"

        # 1. Pre-check transaction lifecycle
        existing_txn = db.query(TransactionModel).filter(
            (TransactionModel.payment_id == payment_id) | (TransactionModel.transaction_id == payment_id)
        ).first()

        if existing_txn and existing_txn.lifecycle_status in ["captured", "paid"]:
            logger.info(
                f"[RECOVERY] Skipping recovery for payment {payment_id}: "
                f"Lifecycle status is already '{existing_txn.lifecycle_status}'."
            )
            return None

        # 2. Convert Razorpay webhook payload to existing Transaction schema
        try:
            txn_schema = self._convert_to_transaction_schema(payment_entity, order_id, event_id, existing_txn)
        except Exception as conv_err:
            logger.error(f"[RECOVERY] Error converting webhook payload to Transaction schema: {conv_err}", exc_info=True)
            return None

        agent_run_id = f"agent_wh_{payment_id}_{uuid.uuid4().hex[:6]}"
        logger.info(f"[RECOVERY] Starting RecoveryAgent for payment_id: {payment_id} | Run ID: {agent_run_id}")

        # 3. Invoke existing autonomous RecoveryAgent
        try:
            agent_result: RecoveryAgentResult = recovery_agent.recover(txn_schema)
            logger.info(
                f"[AGENT] Run ID: {agent_run_id} | "
                f"Root cause: {agent_result.diagnosis.root_cause.value if agent_result.diagnosis else 'unknown'} | "
                f"Action: {agent_result.final_action} | "
                f"Iterations: {agent_result.iterations_used} | "
                f"Status: {agent_result.final_status}"
            )
        except Exception as agent_err:
            logger.error(f"[AGENT] Exception during RecoveryAgent execution: {agent_err}", exc_info=True)
            # Create safe fallback result so webhook persists error trace without crashing
            from app.schemas.agent_state import AutonomousAgentState
            dummy_state = AutonomousAgentState(
                transaction_id=txn_schema.transaction_id,
                current_observation="Exception encountered during recovery",
                final_status="abandoned",
                termination_reason=f"Processing exception: {str(agent_err)}"
            )
            agent_result = RecoveryAgentResult(
                transaction_id=txn_schema.transaction_id,
                final_status="abandoned",
                final_action="give_up",
                recovered_amount=0.0,
                iterations_used=1,
                concise_decision_summary=f"[{txn_schema.transaction_id}] Error: {str(agent_err)}",
                unresolved_reason=f"Processing error: {str(agent_err)}",
                state=dummy_state
            )

        # 4. Persist Multi-Step Agent Traces (one per iteration)
        batch_run_id = f"webhook_{event_id}"
        for step in agent_result.thought_history:
            step_call = agent_result.state.tool_calls[step.step_number - 1] if step.step_number <= len(agent_result.state.tool_calls) else None
            step_result = agent_result.state.tool_results[step.step_number - 1] if step.step_number <= len(agent_result.state.tool_results) else None

            db_trace = AgentTraceModel(
                trace_id=f"trace_{agent_run_id}_step{step.step_number}",
                batch_run_id=batch_run_id,
                transaction_id=txn_schema.transaction_id,
                agent_run_id=agent_run_id,
                iteration_number=step.step_number,
                observation=step.observation,
                thought_summary=step.thought_summary,
                action_tool=step.selected_action.value if hasattr(step.selected_action, "value") else str(step.selected_action),
                tool_input_summary=str(step_call.input_arguments if step_call else {}),
                tool_result_status=step_result.status if step_result else "unknown",
                verification_status=step.verification_result or "UNKNOWN",
                guardrail_decision="intervened" if "guardrail" in step.thought_summary.lower() else "passed",
                recovered_amount=step_result.recovered_amount if step_result else 0.0,
                final_status=agent_result.final_status,
                termination_reason=agent_result.unresolved_reason,
                created_at=step.timestamp
            )
            db.merge(db_trace)

        # 5. Persist Final Audit Log Summary
        is_recovered = (agent_result.final_status == "recovered")
        nudge_msg = None
        switch_used = None
        for res in agent_result.state.tool_results:
            if res.result_data:
                if res.result_data.get("nudge_message"):
                    nudge_msg = res.result_data.get("nudge_message")
                if res.result_data.get("gateway_switch_used"):
                    switch_used = res.result_data.get("gateway_switch_used")

        db_audit = AuditLogModel(
            audit_id=f"audit_wh_{event_id}_{txn_schema.transaction_id}",
            batch_run_id=batch_run_id,
            transaction_id=txn_schema.transaction_id,
            customer_id=txn_schema.customer_id,
            customer_name=txn_schema.customer_name,
            amount=txn_schema.amount,
            currency=txn_schema.currency,
            payment_method=txn_schema.payment_method.value,
            original_error_code=txn_schema.error_code,
            root_cause=agent_result.diagnosis.root_cause.value if agent_result.diagnosis else "unknown",
            diagnosis_confidence=agent_result.diagnosis.confidence if agent_result.diagnosis else 0.9,
            diagnosis_source=agent_result.diagnosis.source.value if agent_result.diagnosis else "rules",
            diagnosis_reasoning=agent_result.diagnosis.reasoning if agent_result.diagnosis else "",
            recommended_action=agent_result.final_action,
            original_action=agent_result.state.tool_calls[0].tool_name.value if agent_result.state.tool_calls else agent_result.final_action,
            guardrail_overridden=any("guardrail" in s.thought_summary.lower() for s in agent_result.thought_history),
            guardrail_checks=[],
            strategy_reasoning=agent_result.concise_decision_summary,
            give_up_reason=agent_result.unresolved_reason,
            execution_status=agent_result.final_status,
            recovered=is_recovered,
            recovered_amount=agent_result.recovered_amount,
            gateway_switch_used=switch_used,
            nudge_channel="whatsapp" if nudge_msg else None,
            nudge_message=nudge_msg,
            unresolved_reason=agent_result.unresolved_reason,
            execution_notes=f"Razorpay Webhook Autonomous Recovery completed in {agent_result.iterations_used} iterations.",
            audit_summary=agent_result.concise_decision_summary,
            agent_run_id=agent_run_id,
            iterations_used=agent_result.iterations_used,
            created_at=datetime.now(timezone.utc)
        )
        db.merge(db_audit)

        # 6. Update TransactionModel lifecycle and agent_run_id
        if existing_txn:
            existing_txn.lifecycle_status = "recovery_attempted"
            existing_txn.agent_run_id = agent_run_id

        return agent_result

    def _convert_to_transaction_schema(
        self,
        payment_entity: Dict[str, Any],
        order_id: Optional[str],
        event_id: str,
        existing_txn: Optional[TransactionModel]
    ) -> Transaction:
        """
        Maps Razorpay payment entity into the internal Transaction Pydantic schema.
        """
        payment_id = payment_entity.get("id") or f"pay_wh_{uuid.uuid4().hex[:8]}"
        
        # Convert amount from paise to INR
        raw_amt = payment_entity.get("amount", 0)
        amount_inr = float(raw_amt) / 100.0 if raw_amt else (existing_txn.amount if existing_txn else 100.0)
        if amount_inr <= 0:
            amount_inr = 100.0

        currency = payment_entity.get("currency", "INR")
        
        # Payment Method Mapping
        raw_method = str(payment_entity.get("method", "card")).lower()
        if raw_method == "upi":
            payment_method = PaymentMethod.UPI
        elif raw_method == "netbanking":
            payment_method = PaymentMethod.NETBANKING
        else:
            payment_method = PaymentMethod.CARD

        # Payment Method Details
        card_network = payment_entity.get("card", {}).get("network") if isinstance(payment_entity.get("card"), dict) else None
        if not card_network and payment_method == PaymentMethod.CARD:
            card_network = "Visa"
        bank_code = payment_entity.get("bank", "HDFC")
        upi_vpa = payment_entity.get("vpa")
        method_details = PaymentMethodDetails(card_network=card_network, bank_code=bank_code, upi_vpa=upi_vpa)

        # Error Context
        error_code = payment_entity.get("error_code") or payment_entity.get("error_reason") or "GATEWAY_ERROR"
        error_desc = payment_entity.get("error_description") or "Payment failed at bank switch or gateway"
        error_source = payment_entity.get("error_source", "bank_switch")

        # Customer Details
        notes = payment_entity.get("notes", {}) or {}
        cust_name = notes.get("customer_name") or (existing_txn.customer_name if existing_txn and existing_txn.customer_name else "Razorpay Customer (Demo)")
        cust_phone = payment_entity.get("contact") or (existing_txn.customer_phone if existing_txn and existing_txn.customer_phone else "+919876543210")
        cust_email = payment_entity.get("email") or (existing_txn.customer_email if existing_txn and existing_txn.customer_email else "customer@example.com")
        cust_id = (existing_txn.customer_id if existing_txn and existing_txn.customer_id else f"cust_{payment_id[:10]}")

        return Transaction(
            transaction_id=payment_id,
            customer_id=cust_id,
            customer_name=cust_name,
            customer_phone=cust_phone,
            customer_email=cust_email,
            amount=amount_inr,
            currency=currency,
            payment_method=payment_method,
            payment_method_details=method_details,
            error_code=error_code,
            error_description=error_desc,
            error_source=error_source,
            retry_count=existing_txn.retry_count if existing_txn else 0,
            last_retry_timestamp=None,
            timestamp=datetime.now(timezone.utc),
            auto_charge_consent=False,
            merchant_id="merch_acme_retail_in",
            customer_history=CustomerHistory(
                lifetime_successful_transactions=5,
                lifetime_failed_transactions=1,
                reliability_score=0.85
            )
        )

# Global RecoveryTriggerService singleton
recovery_trigger_service = RecoveryTriggerService()
