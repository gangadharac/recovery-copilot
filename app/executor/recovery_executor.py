import logging
from typing import Optional
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult, RootCause
from app.schemas.strategy import StrategyDecision, RecoveryAction
from app.schemas.audit import ExecutionOutcome
from app.executor.razorpay_simulator import razorpay_simulator
from app.executor.nudge_generator import nudge_generator

logger = logging.getLogger(__name__)

class RecoveryExecutor:
    """
    Stage 4 Recovery Executor:
    Executes bounded recovery strategies against Razorpay test-mode simulation,
    dispatches Hinglish WhatsApp nudges, and constructs rich execution outcome logs.
    """
    def execute(self, txn: Transaction, diagnosis: DiagnosisResult, decision: StrategyDecision) -> ExecutionOutcome:
        action = decision.action
        
        # 1. Immediate Gateway Retry (via alternate switch)
        if action == RecoveryAction.RETRY_NOW:
            success, switch_used, notes = razorpay_simulator.simulate_retry(txn, diagnosis.root_cause)
            status = "recovered" if success else "failed_retry"
            unresolved = None if success else f"Alternate gateway switch ({switch_used}) also failed to capture payment."
            
            return ExecutionOutcome(
                transaction_id=txn.transaction_id,
                action_executed=action.value,
                status=status,
                recovered=success,
                recovered_amount=txn.amount if success else 0.0,
                gateway_switch_used=switch_used,
                nudge_channel=None,
                nudge_message=None,
                unresolved_reason=unresolved,
                execution_notes=notes
            )

        # 2. Contextual Hinglish WhatsApp Customer Nudge
        elif action == RecoveryAction.NUDGE_CUSTOMER:
            nudge_msg = nudge_generator.generate_message(txn, diagnosis)
            converted, notes = razorpay_simulator.simulate_nudge_conversion(txn, diagnosis.root_cause)
            status = "recovered" if converted else "nudged_unconverted"
            unresolved = None if converted else "Customer received WhatsApp nudge with payment link but did not re-attempt payment."
            
            return ExecutionOutcome(
                transaction_id=txn.transaction_id,
                action_executed=action.value,
                status=status,
                recovered=converted,
                recovered_amount=txn.amount if converted else 0.0,
                gateway_switch_used=None,
                nudge_channel="whatsapp",
                nudge_message=nudge_msg,
                unresolved_reason=unresolved,
                execution_notes=notes
            )

        # 3. Offer Alternate Payment Method (e.g. Switch from Expired Card to UPI)
        elif action == RecoveryAction.OFFER_ALT_METHOD:
            target_method = decision.target_alternate_method or "upi"
            nudge_msg = nudge_generator.generate_message(txn, diagnosis)
            converted, notes = razorpay_simulator.simulate_alt_method_conversion(txn, target_method)
            status = "recovered" if converted else "alt_method_abandoned"
            unresolved = None if converted else f"Customer presented with {target_method.upper()} payment link but dropped off."
            
            return ExecutionOutcome(
                transaction_id=txn.transaction_id,
                action_executed=action.value,
                status=status,
                recovered=converted,
                recovered_amount=txn.amount if converted else 0.0,
                gateway_switch_used=None,
                nudge_channel="whatsapp_custom_checkout",
                nudge_message=nudge_msg,
                unresolved_reason=unresolved,
                execution_notes=notes
            )

        # 4. Scheduled Retry Later (Exponential Backoff / Cooldown)
        elif action == RecoveryAction.RETRY_LATER:
            wait_time = decision.retry_scheduled_minutes or 30
            notes = f"Retry scheduled in {wait_time} minutes after compliance cooldown window."
            return ExecutionOutcome(
                transaction_id=txn.transaction_id,
                action_executed=action.value,
                status="scheduled_retry",
                recovered=False,
                recovered_amount=0.0,
                gateway_switch_used=None,
                nudge_channel=None,
                nudge_message=None,
                unresolved_reason=f"Awaiting retry cooldown window ({wait_time}m remaining).",
                execution_notes=notes
            )

        # 5. Escalate to Human Fraud / VIP Ops Team
        elif action == RecoveryAction.ESCALATE_HUMAN:
            notes = "Ticket auto-created in Razorpay Ops Dashboard with diagnostic summary & customer history for manual review."
            return ExecutionOutcome(
                transaction_id=txn.transaction_id,
                action_executed=action.value,
                status="escalated_to_ops",
                recovered=False,
                recovered_amount=0.0,
                gateway_switch_used=None,
                nudge_channel=None,
                nudge_message=None,
                unresolved_reason="Under manual review by Fraud & Risk Operations team.",
                execution_notes=notes
            )

        # 6. Give Up (Stopping rule reached)
        else: # GIVE_UP
            give_up_reason = decision.give_up_reason or "Stopping rule triggered: recovery attempts halted."
            return ExecutionOutcome(
                transaction_id=txn.transaction_id,
                action_executed=action.value,
                status="abandoned",
                recovered=False,
                recovered_amount=0.0,
                gateway_switch_used=None,
                nudge_channel=None,
                nudge_message=None,
                unresolved_reason=give_up_reason,
                execution_notes=f"Recovery stopped. Reason: {give_up_reason}"
            )

# Global Executor singleton
recovery_executor = RecoveryExecutor()
