import pytest
from datetime import datetime, timezone
from app.schemas.transaction import Transaction, PaymentMethod, PaymentMethodDetails
from app.schemas.diagnosis import DiagnosisResult, RootCause, DiagnosisSource
from app.schemas.strategy import StrategyDecision, RecoveryAction
from app.executor.recovery_executor import recovery_executor

def test_executor_smoke():
    """Verifies recovery executor produces valid ExecutionOutcome with Hinglish message for nudge."""
    txn = Transaction(
        transaction_id="txn_exec_01",
        customer_id="cust_01",
        customer_name="Rohan Mehta",
        customer_phone="+919876543212",
        customer_email="rohan@example.com",
        amount=3499.0,
        payment_method=PaymentMethod.UPI,
        error_code="otp_timeout",
        error_description="OTP expired",
        error_source="user_error",
        timestamp=datetime.now(timezone.utc)
    )
    diagnosis = DiagnosisResult(
        transaction_id=txn.transaction_id,
        root_cause=RootCause.WRONG_OTP,
        confidence=0.95,
        reasoning="OTP expired",
        source=DiagnosisSource.RULES
    )
    decision = StrategyDecision(
        transaction_id=txn.transaction_id,
        action=RecoveryAction.NUDGE_CUSTOMER,
        original_action=RecoveryAction.NUDGE_CUSTOMER,
        reasoning="Dispatch WhatsApp nudge"
    )
    outcome = recovery_executor.execute(txn, diagnosis, decision)
    assert outcome.transaction_id == txn.transaction_id
    assert outcome.nudge_message is not None
    assert "Rohan Mehta" in outcome.nudge_message
    assert "3,499.00" in outcome.nudge_message or "3499" in outcome.nudge_message
