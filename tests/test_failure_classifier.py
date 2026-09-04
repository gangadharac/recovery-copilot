import pytest
from app.agents.failure_classifier import FailureReason, classify_failure


def test_classify_otp_failure_via_step():
    payload = {
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_01",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Payment was not completed within the allowed time window.",
                    "error_source": "customer",
                    "error_step": "payment_authentication",
                    "error_reason": "payment_cancelled_by_user"
                }
            }
        }
    }
    assert classify_failure(payload) == FailureReason.OTP_FAILURE


def test_classify_otp_failure_via_description():
    entity = {
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "3DS verification failed: customer submitted wrong OTP",
        "error_source": "customer",
        "error_step": "payment_authentication"
    }
    assert classify_failure(entity) == FailureReason.OTP_FAILURE


def test_classify_bank_server_down_via_source_and_timeout():
    payload = {
        "payload": {
            "payment": {
                "entity": {
                    "error_code": "GATEWAY_ERROR",
                    "error_description": "Issuer switch timed out during authorization",
                    "error_source": "issuer",
                    "error_step": "payment_authorization"
                }
            }
        }
    }
    assert classify_failure(payload) == FailureReason.BANK_SERVER_DOWN


def test_classify_bank_server_down_via_code():
    entity = {
        "error_code": "GATEWAY_ERROR",
        "error_description": "Payment failed at bank switch",
        "error_source": "bank_switch"
    }
    assert classify_failure(entity) == FailureReason.BANK_SERVER_DOWN


def test_classify_insufficient_funds():
    payload = {
        "payload": {
            "payment": {
                "entity": {
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Your account has insufficient funds to complete this payment",
                    "error_source": "customer",
                    "error_reason": "insufficient_funds"
                }
            }
        }
    }
    assert classify_failure(payload) == FailureReason.INSUFFICIENT_FUNDS


def test_classify_limit_exceeded_funds():
    entity = {
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Daily velocity limit exceeded on payment instrument",
        "error_source": "issuer"
    }
    assert classify_failure(entity) == FailureReason.INSUFFICIENT_FUNDS


def test_classify_card_expired():
    payload = {
        "payload": {
            "payment": {
                "entity": {
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "The card expiry date is invalid or card has expired",
                    "error_source": "customer",
                    "error_reason": "card_expired"
                }
            }
        }
    }
    assert classify_failure(payload) == FailureReason.CARD_EXPIRED


def test_classify_risk_blocked_via_source():
    payload = {
        "payload": {
            "payment": {
                "entity": {
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Transaction blocked per merchant fraud rule settings",
                    "error_source": "business",
                    "error_step": "payment_initiation"
                }
            }
        }
    }
    assert classify_failure(payload) == FailureReason.RISK_BLOCKED


def test_classify_risk_blocked_via_description():
    entity = {
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Payment declined due to high fraud risk score",
        "error_source": "gateway"
    }
    assert classify_failure(entity) == FailureReason.RISK_BLOCKED


def test_classify_network_glitch():
    payload = {
        "payload": {
            "payment": {
                "entity": {
                    "error_code": "GATEWAY_ERROR",
                    "error_description": "Network connection dropped before issuer response was received",
                    "error_source": "gateway",
                    "error_reason": "connection_dropped"
                }
            }
        }
    }
    assert classify_failure(payload) == FailureReason.NETWORK_GLITCH


def test_classify_unknown_fallback():
    assert classify_failure({}) == FailureReason.UNKNOWN
    assert classify_failure({"error_code": "SOMETHING_RANDOM", "error_description": "Unspecified anomaly"}) == FailureReason.UNKNOWN
    assert classify_failure(None) == FailureReason.UNKNOWN
