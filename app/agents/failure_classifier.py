"""
Failure Classification Module:
Pure deterministic mapping from Razorpay webhook payment.failed payloads
to structured FailureReason categories.
"""

from enum import Enum
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger("app.recovery.classifier")


class FailureReason(str, Enum):
    """
    Closed set of actionable root-cause failure categories for payment recovery.
    """
    OTP_FAILURE = "otp_failure"                 # User didn't enter OTP, wrong OTP, 3DS expired/declined
    BANK_SERVER_DOWN = "bank_server_down"       # Issuer switch unavailable, gateway timeout, bank maintenance
    INSUFFICIENT_FUNDS = "insufficient_funds"   # Account balance inadequate or card limit exceeded
    CARD_EXPIRED = "card_expired"               # Expired card month/year, invalid/expired instrument
    RISK_BLOCKED = "risk_blocked"               # Razorpay Shield / fraud score / blacklisted entity
    NETWORK_GLITCH = "network_glitch"           # Dropped connection, socket timeout, browser disconnected
    UNKNOWN = "unknown"                         # Ambiguous / unclassified error


def classify_failure(payload: Dict[str, Any]) -> FailureReason:
    """
    Pure deterministic classifier mapping Razorpay payment.failed payload metadata
    (error_code, error_description, error_source, error_step, error_reason)
    to a FailureReason category.

    Accepts both:
    1. Full Razorpay webhook payload: {"payload": {"payment": {"entity": {...}}}}
    2. Direct payment entity dictionary: {"error_code": ..., "error_description": ...}
    """
    if not isinstance(payload, dict):
        return FailureReason.UNKNOWN

    # Extract payment entity dictionary
    payment_entity = (
        payload.get("payload", {})
               .get("payment", {})
               .get("entity", {})
    )
    if not payment_entity and ("error_code" in payload or "error_description" in payload):
        # Passed direct payment entity
        payment_entity = payload

    error_code = str(payment_entity.get("error_code") or "").strip().upper()
    error_desc = str(payment_entity.get("error_description") or "").strip().lower()
    error_source = str(payment_entity.get("error_source") or "").strip().lower()
    error_step = str(payment_entity.get("error_step") or "").strip().lower()
    error_reason = str(payment_entity.get("error_reason") or "").strip().lower()

    # 1. Strict Risk / Fraud Block (Top Priority - Hard Safety Isolation)
    if (
        error_source == "business"
        or "fraud" in error_desc
        or "risk" in error_desc
        or "risk" in error_reason
        or "blacklisted" in error_desc
        or "shield" in error_desc
        or error_reason in ["fraud_detected", "risk_threshold_exceeded", "blacklisted_card"]
    ):
        return FailureReason.RISK_BLOCKED

    # 2. Authentication / OTP Failures
    if (
        error_step == "payment_authentication"
        or "otp" in error_desc
        or "3ds" in error_desc
        or "authentication failed" in error_desc
        or "auth failed" in error_desc
        or error_reason in [
            "payment_cancelled_by_user",
            "authentication_failed",
            "otp_timeout",
            "otp_expired",
            "3ds_verification_failed"
        ]
    ):
        return FailureReason.OTP_FAILURE

    # 3. Insufficient Balance / Credit Limit Exceeded
    if (
        "insufficient" in error_desc
        or "low balance" in error_desc
        or "limit exceeded" in error_desc
        or "credit limit" in error_desc
        or error_reason in ["insufficient_funds", "insufficient_balance", "limit_exceeded"]
    ):
        return FailureReason.INSUFFICIENT_FUNDS

    # 4. Expired / Invalid Card Instrument
    if (
        "expired" in error_desc
        or "invalid expiry" in error_desc
        or "card expired" in error_desc
        or error_reason in ["card_expired", "expired_card", "invalid_expiry_date"]
    ):
        return FailureReason.CARD_EXPIRED

    # 5. Transient Network Glitches (socket/transport dropped)
    if (
        "network" in error_desc
        or "connection" in error_desc
        or "socket" in error_desc
        or "disconnected" in error_desc
        or error_reason in ["network_error", "connection_dropped", "client_disconnected"]
    ):
        return FailureReason.NETWORK_GLITCH

    # 6. Bank Switch / Gateway Downtime
    if (
        error_source in ["issuer", "bank_switch", "gateway"]
        and (
            "timed out" in error_desc
            or "timeout" in error_desc
            or "unavailable" in error_desc
            or "declined by bank" in error_desc
            or "server error" in error_desc
            or "bank switch" in error_desc
        )
        or error_code in ["GATEWAY_ERROR", "SERVER_ERROR"]
        or error_reason in ["payment_failed_at_bank", "bank_timeout", "issuer_switch_unavailable"]
    ):
        return FailureReason.BANK_SERVER_DOWN

    return FailureReason.UNKNOWN
