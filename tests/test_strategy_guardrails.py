import pytest
from datetime import datetime, timedelta, timezone
from app.schemas.transaction import (
    Transaction,
    PaymentMethod,
    PaymentMethodDetails,
    CustomerHistory
)
from app.schemas.diagnosis import DiagnosisResult, RootCause, DiagnosisSource
from app.schemas.strategy import RecoveryAction, StrategyDecision
from app.agents.strategy_agent import StrategyAgent

@pytest.fixture
def strategy_agent():
    return StrategyAgent()

@pytest.fixture
def base_transaction():
    return Transaction(
        transaction_id="txn_test_guardrail_001",
        customer_id="cust_test_101",
        customer_name="Test Customer",
        customer_phone="+919876543210",
        customer_email="test@example.com",
        amount=2500.0,
        currency="INR",
        payment_method=PaymentMethod.CARD,
        payment_method_details=PaymentMethodDetails(bank_code="HDFC"),
        error_code="GATEWAY_ERROR",
        error_description="Bank switch timed out",
        error_source="bank_switch",
        retry_count=0,
        last_retry_timestamp=None,
        timestamp=datetime(2026, 8, 31, 12, 0, 0),
        auto_charge_consent=True,
        customer_history=CustomerHistory(reliability_score=0.9)
    )

def test_guardrail_1_risk_safety_isolation(strategy_agent, base_transaction):
    """
    Guardrail 1: Strict Risk Isolation
    Transactions diagnosed as risk_blocked MUST strictly route to escalate_human.
    Automated retries or customer nudges are strictly forbidden.
    """
    diagnosis = DiagnosisResult(
        transaction_id=base_transaction.transaction_id,
        root_cause=RootCause.RISK_BLOCKED,
        confidence=0.98,
        reasoning="Risk engine flagged transaction",
        source=DiagnosisSource.RULES
    )
    
    decision = strategy_agent.decide_action(base_transaction, diagnosis)
    
    assert decision.action == RecoveryAction.ESCALATE_HUMAN
    risk_check = next(g for g in decision.guardrail_checks if g.rule_name == "RISK_SAFETY_ISOLATION")
    assert risk_check.passed is True
    assert "human" in decision.reasoning.lower() or "risk" in decision.reasoning.lower()

def test_guardrail_2_max_retry_cap_give_up(strategy_agent, base_transaction):
    """
    Guardrail 2: Hard Retry Cap (Max 3 attempts)
    Transactions that have already reached or exceeded 3 retries MUST trigger stopping rule (give_up).
    """
    base_transaction.retry_count = 3
    base_transaction.amount = 1200.0  # Normal value
    
    diagnosis = DiagnosisResult(
        transaction_id=base_transaction.transaction_id,
        root_cause=RootCause.BANK_TIMEOUT,
        confidence=0.95,
        reasoning="Bank timeout",
        source=DiagnosisSource.RULES
    )
    
    decision = strategy_agent.decide_action(base_transaction, diagnosis)
    
    assert decision.action == RecoveryAction.GIVE_UP
    assert decision.guardrail_overridden is True
    assert decision.give_up_reason is not None
    assert "3/3 attempts exhausted" in decision.give_up_reason
    retry_check = next(g for g in decision.guardrail_checks if g.rule_name == "MAX_RETRY_CAP")
    assert retry_check.passed is False

def test_guardrail_2_max_retry_cap_high_value_escalation(strategy_agent, base_transaction):
    """
    Guardrail 2 (High Value): Transactions >= ₹15,000 hitting max retry cap
    are escalated to VIP human ops rather than dropped.
    """
    base_transaction.retry_count = 3
    base_transaction.amount = 25000.0  # High value
    
    diagnosis = DiagnosisResult(
        transaction_id=base_transaction.transaction_id,
        root_cause=RootCause.BANK_TIMEOUT,
        confidence=0.95,
        reasoning="Bank timeout",
        source=DiagnosisSource.RULES
    )
    
    decision = strategy_agent.decide_action(base_transaction, diagnosis)
    assert decision.action == RecoveryAction.ESCALATE_HUMAN
    assert decision.guardrail_overridden is True
    assert "VIP" in decision.reasoning or "High-value" in decision.reasoning

def test_guardrail_3_auto_charge_consent_mandate(strategy_agent, base_transaction):
    """
    Guardrail 3: Auto-Charge Consent Enforcement
    A bank timeout would normally trigger retry_now (silent auto-retry), but if
    auto_charge_consent is False, it MUST be overridden to interactive nudge_customer.
    """
    base_transaction.auto_charge_consent = False
    
    diagnosis = DiagnosisResult(
        transaction_id=base_transaction.transaction_id,
        root_cause=RootCause.BANK_TIMEOUT,
        confidence=0.95,
        reasoning="Bank timeout",
        source=DiagnosisSource.RULES
    )
    
    decision = strategy_agent.decide_action(base_transaction, diagnosis)
    
    assert decision.original_action == RecoveryAction.RETRY_NOW
    assert decision.action == RecoveryAction.NUDGE_CUSTOMER
    assert decision.guardrail_overridden is True
    consent_check = next(g for g in decision.guardrail_checks if g.rule_name == "CONSENT_MANDATE_ENFORCEMENT")
    assert consent_check.passed is False
    assert "consent is False" in consent_check.description

def test_guardrail_4_cooldown_window_enforcement(strategy_agent, base_transaction):
    """
    Guardrail 4: Cooldown Window Check (Min 30 minutes)
    If a transaction was retried 10 minutes ago, retry_now MUST be overridden
    to retry_later with the remaining cooldown scheduled.
    """
    base_transaction.retry_count = 1
    base_transaction.auto_charge_consent = True
    base_transaction.timestamp = datetime(2026, 8, 31, 12, 10, 0)
    base_transaction.last_retry_timestamp = datetime(2026, 8, 31, 12, 0, 0) # 10 mins elapsed < 30 mins
    
    diagnosis = DiagnosisResult(
        transaction_id=base_transaction.transaction_id,
        root_cause=RootCause.BANK_TIMEOUT,
        confidence=0.95,
        reasoning="Bank timeout",
        source=DiagnosisSource.RULES
    )
    
    decision = strategy_agent.decide_action(base_transaction, diagnosis)
    
    assert decision.action == RecoveryAction.RETRY_LATER
    assert decision.guardrail_overridden is True
    assert decision.retry_scheduled_minutes is not None
    assert decision.retry_scheduled_minutes >= 20  # ~21 minutes remaining
    cooldown_check = next(g for g in decision.guardrail_checks if g.rule_name == "COOLDOWN_WINDOW_ENFORCEMENT")
    assert cooldown_check.passed is False

def test_closed_action_set_integrity(strategy_agent, base_transaction):
    """
    Verifies that all decided actions strictly belong to the predefined closed action set.
    """
    for rc in RootCause:
        diagnosis = DiagnosisResult(
            transaction_id=base_transaction.transaction_id,
            root_cause=rc,
            confidence=0.90,
            reasoning=f"Testing root cause {rc.value}",
            source=DiagnosisSource.RULES
        )
        decision = strategy_agent.decide_action(base_transaction, diagnosis)
        assert isinstance(decision.action, RecoveryAction)
        assert decision.action in [
            RecoveryAction.RETRY_NOW,
            RecoveryAction.RETRY_LATER,
            RecoveryAction.NUDGE_CUSTOMER,
            RecoveryAction.OFFER_ALT_METHOD,
            RecoveryAction.ESCALATE_HUMAN,
            RecoveryAction.GIVE_UP
        ]
