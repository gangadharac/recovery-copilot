import pytest
from app.agents.failure_classifier import FailureReason
from app.agents.recovery_strategy_engine import RECOVERY_STRATEGIES, get_recovery_plan, RecoveryPlan


def test_recovery_strategies_completeness():
    """All FailureReason enum values must have a defined strategy plan."""
    for reason in FailureReason:
        assert reason in RECOVERY_STRATEGIES
        plan = RECOVERY_STRATEGIES[reason]
        assert isinstance(plan, RecoveryPlan)
        assert plan.failure_reason == reason
        assert plan.retry_delay_seconds >= 0
        assert plan.max_attempts in [0, 1, 2, 3]


def test_otp_failure_strategy():
    plan = get_recovery_plan(FailureReason.OTP_FAILURE)
    assert plan.retry_delay_seconds == 0  # Immediate retry
    assert "upi" in plan.suggested_methods
    assert plan.use_upi_intent is True
    assert plan.max_attempts == 2
    assert plan.requires_manual_review is False


def test_bank_server_down_cooldown():
    plan = get_recovery_plan(FailureReason.BANK_SERVER_DOWN)
    assert plan.retry_delay_seconds == 900  # 15 minutes cooldown
    assert "upi" in plan.suggested_methods
    assert plan.action_type == "delayed_nudge"
    assert plan.max_attempts == 2


def test_insufficient_funds_delayed_retry():
    plan = get_recovery_plan(FailureReason.INSUFFICIENT_FUNDS)
    assert plan.retry_delay_seconds == 7200  # 2 hours delayed window
    assert plan.max_attempts == 1
    assert plan.action_type == "delayed_nudge"


def test_card_expired_strategy():
    plan = get_recovery_plan(FailureReason.CARD_EXPIRED)
    assert plan.retry_delay_seconds == 0
    assert plan.use_upi_intent is True
    assert plan.max_attempts == 1  # Never retry expired card multiple times
    assert "upi" in plan.suggested_methods


def test_risk_blocked_strict_guardrail():
    plan = get_recovery_plan(FailureReason.RISK_BLOCKED)
    assert plan.requires_manual_review is True
    assert plan.max_attempts == 0  # Strict 0% automated retry
    assert len(plan.suggested_methods) == 0
    assert plan.action_type == "quarantine"


def test_network_glitch_quick_buffer():
    plan = get_recovery_plan(FailureReason.NETWORK_GLITCH)
    assert plan.retry_delay_seconds == 30
    assert plan.max_attempts == 3


def test_unknown_fallback():
    plan = get_recovery_plan("non_existent_reason")
    assert plan.failure_reason == FailureReason.UNKNOWN
    assert plan.retry_delay_seconds == 0
    assert plan.requires_manual_review is True
    assert plan.action_type == "quarantine"
    assert plan.max_attempts == 0
