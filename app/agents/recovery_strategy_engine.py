"""
Recovery Strategy Engine:
Declarative strategy brain mapping diagnosed failure reasons to bounded,
actionable recovery plans with reason-tailored messaging, method preferences,
cooldown schedules, and strict risk guardrails.
"""

from typing import List, Dict, Optional
from pydantic import BaseModel, Field
from app.agents.failure_classifier import FailureReason


class RecoveryPlan(BaseModel):
    """
    Structured recovery strategy plan generated for a diagnosed payment failure.
    """
    failure_reason: FailureReason
    action_type: str = Field(description="Action archetype: payment_link, delayed_nudge, quarantine, retry_gateway")
    retry_delay_seconds: int = Field(default=0, ge=0, description="Cooldown window in seconds before recovery attempt")
    suggested_methods: List[str] = Field(default_factory=list, description="Prioritized payment rails (upi, card, netbanking, wallet)")
    customer_message: str = Field(description="Reason-tailored customer notification copy")
    requires_manual_review: bool = Field(default=False, description="Whether transaction must be quarantined for human ops")
    max_attempts: int = Field(default=1, ge=0, le=3, description="Hard upper bound on automated attempts")
    use_upi_intent: bool = Field(default=False, description="Whether to prioritize instant UPI intent flow")


RECOVERY_STRATEGIES: Dict[FailureReason, RecoveryPlan] = {
    FailureReason.OTP_FAILURE: RecoveryPlan(
        failure_reason=FailureReason.OTP_FAILURE,
        action_type="payment_link",
        retry_delay_seconds=0,
        suggested_methods=["upi", "card"],
        customer_message="Your payment verification timed out. Complete it faster in 1-tap using UPI or another card.",
        requires_manual_review=False,
        max_attempts=2,
        use_upi_intent=True
    ),
    FailureReason.BANK_SERVER_DOWN: RecoveryPlan(
        failure_reason=FailureReason.BANK_SERVER_DOWN,
        action_type="delayed_nudge",
        retry_delay_seconds=900,  # 15 minutes cooldown window
        suggested_methods=["upi", "wallet", "netbanking"],
        customer_message="Your bank switch is experiencing downtime. Retry with UPI or another bank for instant confirmation.",
        requires_manual_review=False,
        max_attempts=2,
        use_upi_intent=False
    ),
    FailureReason.INSUFFICIENT_FUNDS: RecoveryPlan(
        failure_reason=FailureReason.INSUFFICIENT_FUNDS,
        action_type="delayed_nudge",
        retry_delay_seconds=7200,  # 2 hours delayed window
        suggested_methods=["upi", "card"],
        customer_message="Payment incomplete. Complete your purchase anytime using your preferred payment method.",
        requires_manual_review=False,
        max_attempts=1,
        use_upi_intent=False
    ),
    FailureReason.CARD_EXPIRED: RecoveryPlan(
        failure_reason=FailureReason.CARD_EXPIRED,
        action_type="payment_link",
        retry_delay_seconds=0,
        suggested_methods=["upi", "card"],
        customer_message="Your card appears to be expired. Please use a valid card or UPI to complete payment.",
        requires_manual_review=False,
        max_attempts=1,
        use_upi_intent=True
    ),
    FailureReason.RISK_BLOCKED: RecoveryPlan(
        failure_reason=FailureReason.RISK_BLOCKED,
        action_type="quarantine",
        retry_delay_seconds=0,
        suggested_methods=[],
        customer_message="Transaction quarantined for security verification. Automated retry blocked by risk policy.",
        requires_manual_review=True,
        max_attempts=0,  # Strict 0% retry guardrail
        use_upi_intent=False
    ),
    FailureReason.NETWORK_GLITCH: RecoveryPlan(
        failure_reason=FailureReason.NETWORK_GLITCH,
        action_type="payment_link",
        retry_delay_seconds=30,  # Quick 30-second transient buffer
        suggested_methods=["card", "upi"],
        customer_message="A transient connection hiccup occurred. Retry now — your cart and items are saved.",
        requires_manual_review=False,
        max_attempts=3,
        use_upi_intent=False
    ),
    FailureReason.UNKNOWN: RecoveryPlan(
        failure_reason=FailureReason.UNKNOWN,
        action_type="quarantine",
        retry_delay_seconds=0,
        suggested_methods=[],
        customer_message="Payment failure reason could not be verified. Quarantined for manual review.",
        requires_manual_review=True,
        max_attempts=0,
        use_upi_intent=False
    )
}


def get_recovery_plan(failure_reason: FailureReason) -> RecoveryPlan:
    """
    Returns the declarative RecoveryPlan for a given failure reason.
    Falls back to UNKNOWN strategy plan if an unrecognized value is provided.
    """
    return RECOVERY_STRATEGIES.get(failure_reason, RECOVERY_STRATEGIES[FailureReason.UNKNOWN])
