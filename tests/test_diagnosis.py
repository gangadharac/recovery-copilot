import pytest
from datetime import datetime, timezone
from app.schemas.transaction import Transaction, PaymentMethod, PaymentMethodDetails
from app.schemas.diagnosis import RootCause, DiagnosisSource
from app.agents.diagnosis_agent import diagnosis_agent

def test_diagnosis_rules_and_llm_smoke():
    """Verifies rule-based and fallback diagnosis on sample transactions."""
    # Direct rule match
    txn_rule = Transaction(
        transaction_id="txn_diag_01",
        customer_id="cust_01",
        customer_name="Aarav",
        customer_phone="+919876543210",
        customer_email="aarav@example.com",
        amount=1999.0,
        payment_method=PaymentMethod.CARD,
        error_code="INSUFFICIENT_BALANCE",
        error_description="Low balance",
        error_source="user_error",
        timestamp=datetime.now(timezone.utc)
    )
    res_rule = diagnosis_agent.diagnose(txn_rule)
    assert res_rule.root_cause == RootCause.INSUFFICIENT_FUNDS
    assert res_rule.source == DiagnosisSource.RULES

    # Ambiguous error -> LLM fallback
    txn_llm = Transaction(
        transaction_id="txn_diag_02",
        customer_id="cust_02",
        customer_name="Priya",
        customer_phone="+919876543211",
        customer_email="priya@example.com",
        amount=4500.0,
        payment_method=PaymentMethod.CARD,
        error_code="BAD_REQUEST_ERROR",
        error_description="Downstream partner generic timeout on card switch",
        error_source="gateway",
        timestamp=datetime.now(timezone.utc)
    )
    res_llm = diagnosis_agent.diagnose(txn_llm)
    assert isinstance(res_llm.root_cause, RootCause)
    assert res_llm.confidence >= 0.5
