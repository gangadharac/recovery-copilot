"""
Step 5 Revenue Recovery Intelligence Verification Tests:
Tests the autonomous loop connecting real webhook events to:
1. Deterministic Failure Classification (classify_failure)
2. Strategy Plan Generation (get_recovery_plan)
3. Strict Safety Isolation (Risk Blocked -> Quarantine to Human Ops, 0 retries)
4. Cooldown Scheduling (Bank Server Down & Insufficient Funds -> RecoveryScheduler persistence)
5. Immediate Smart Link Recovery (OTP Failure & Card Expired -> Reason-tailored Payment Links)
6. Automatic Cancellation of Pending Retries upon Verified Payment Success
"""

import json
import uuid
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.config import settings
from app.db.database import SessionLocal, init_db
from app.db.models import (
    TransactionModel,
    AuditLogModel,
    AgentTraceModel,
    RecoveryAttemptModel,
    RecoveryScheduleModel
)
from app.webhooks.webhook_security import generate_test_signature

TEST_SECRET = "test_intelligence_secret_key"


@pytest.fixture(autouse=True)
def setup_intelligence_test_env(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_intel_key_123")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "intel_secret_key_456")
    init_db()


@pytest.fixture
def client():
    return TestClient(app)


def make_signed_request(client, payload_dict, secret=TEST_SECRET):
    raw_body = json.dumps(payload_dict).encode("utf-8")
    sig = generate_test_signature(raw_body, secret)
    return client.post(
        "/webhooks/razorpay",
        content=raw_body,
        headers={"X-Razorpay-Signature": sig}
    )


# 1. RISK BLOCKED: Strict Quarantine, 0 retries, no delayed schedule, no payment link
def test_risk_blocked_strictly_quarantined_without_retries(client):
    payment_id = f"pay_risk_{uuid.uuid4().hex[:6]}"
    event_id = f"evt_risk_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 1850000, # INR 18,500
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Transaction blocked by Razorpay Shield risk engine",
                    "error_source": "business",
                    "error_reason": "fraud_detected"
                }
            }
        }
    }

    with patch("requests.post") as mock_post:
        res = make_signed_request(client, payload)
        assert res.status_code == 200
        data = res.json()
        assert data["agent_triggered"] is True
        assert data["agent_status"] == "escalated"

        # Ensure NO payment link was created via HTTP POST
        assert not mock_post.called

        db: Session = SessionLocal()
        try:
            # Confirm NO schedule was created in recovery_schedules
            schedules = db.query(RecoveryScheduleModel).filter(
                RecoveryScheduleModel.transaction_id == payment_id
            ).all()
            assert len(schedules) == 0

            # Confirm NO recovery attempt link was created
            attempts = db.query(RecoveryAttemptModel).filter(
                RecoveryAttemptModel.transaction_id == payment_id
            ).all()
            assert len(attempts) == 0

            # Confirm Audit log records human escalation quarantine
            audit = db.query(AuditLogModel).filter(
                AuditLogModel.transaction_id == payment_id
            ).first()
            assert audit is not None
            assert audit.root_cause == "risk_blocked"
            assert audit.recommended_action == "human_escalation"
            assert audit.execution_status == "escalated"
            assert audit.recovered is False
        finally:
            db.close()


# 2. BANK SERVER DOWN: Schedules 900s cooldown retry via RecoveryScheduler
def test_bank_server_down_schedules_delayed_cooldown(client):
    payment_id = f"pay_bank_{uuid.uuid4().hex[:6]}"
    event_id = f"evt_bank_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 499900, # INR 4,999
                    "currency": "INR",
                    "status": "failed",
                    "method": "netbanking",
                    "error_code": "GATEWAY_ERROR",
                    "error_description": "Bank switch timed out during authorization",
                    "error_source": "bank_switch",
                    "error_reason": "bank_switch_timeout"
                }
            }
        }
    }

    res = make_signed_request(client, payload)
    assert res.status_code == 200
    assert res.json()["agent_triggered"] is True

    db: Session = SessionLocal()
    try:
        # Verify RecoveryScheduleModel has the 900s delayed job
        sched = db.query(RecoveryScheduleModel).filter(
            RecoveryScheduleModel.transaction_id == payment_id
        ).first()

        assert sched is not None
        assert sched.failure_reason == "bank_server_down"
        assert sched.status in ["pending", "executing"]
        assert sched.action_payload["amount"] == 4999.0
        assert "bank switch is experiencing downtime" in sched.action_payload["description"].lower()
        assert sched.action_payload["suggested_methods"] == ["upi", "wallet", "netbanking"]

        # Verify Audit trail
        audit = db.query(AuditLogModel).filter(
            AuditLogModel.transaction_id == payment_id
        ).first()
        assert audit is not None
        assert audit.root_cause == "bank_timeout"
        assert "downtime" in audit.nudge_message.lower()
    finally:
        db.close()


# 3. INSUFFICIENT FUNDS: Schedules 7200s delayed retry via RecoveryScheduler
def test_insufficient_funds_schedules_2hr_delayed_retry(client):
    payment_id = f"pay_funds_{uuid.uuid4().hex[:6]}"
    event_id = f"evt_funds_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 750000, # INR 7,500
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Customer account balance or limit insufficient",
                    "error_reason": "insufficient_funds"
                }
            }
        }
    }

    res = make_signed_request(client, payload)
    assert res.status_code == 200

    db: Session = SessionLocal()
    try:
        sched = db.query(RecoveryScheduleModel).filter(
            RecoveryScheduleModel.transaction_id == payment_id
        ).first()

        assert sched is not None
        assert sched.failure_reason == "insufficient_funds"
        assert sched.status in ["pending", "executing"]
        assert sched.action_payload["amount"] == 7500.0
        assert "preferred payment method" in sched.action_payload["description"].lower()
    finally:
        db.close()


# 4. OTP FAILURE: Immediate smart recovery link with tailored copy + UPI intent
def test_otp_failure_creates_immediate_smart_payment_link(client):
    payment_id = f"pay_otp_{uuid.uuid4().hex[:6]}"
    event_id = f"evt_otp_{uuid.uuid4().hex[:6]}"
    mock_plink_id = f"plink_smart_otp_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 249900, # INR 2,499
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "User did not submit OTP in time",
                    "error_step": "payment_authentication",
                    "error_reason": "otp_timeout"
                }
            }
        }
    }

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": mock_plink_id,
            "short_url": f"https://rzp.io/i/{mock_plink_id}",
            "status": "created"
        }
        mock_post.return_value = mock_resp

        res = make_signed_request(client, payload)
        assert res.status_code == 200
        assert mock_post.called

        # Verify outgoing payload sent to Razorpay API
        sent_body = mock_post.call_args[1]["json"]
        assert "1-tap using upi" in sent_body["description"].lower()
        assert sent_body["upi_link"] is True
        assert sent_body["notes"]["failure_reason"] == "otp_failure"
        assert sent_body["notes"]["preferred_methods"] == "upi,card"

        db: Session = SessionLocal()
        try:
            # Verify RecoveryAttemptModel was created with failure_reason and preferred_methods
            attempt = db.query(RecoveryAttemptModel).filter(
                RecoveryAttemptModel.transaction_id == payment_id
            ).first()
            assert attempt is not None
            assert attempt.payment_link_id == mock_plink_id
            assert attempt.failure_reason == "otp_failure"
            assert attempt.preferred_methods == "upi,card"
            assert attempt.status == "pending"
            assert attempt.amount == 2499.0
        finally:
            db.close()


# 5. CARD EXPIRED: Immediate smart recovery link prompting method switch
def test_card_expired_creates_immediate_smart_payment_link(client):
    payment_id = f"pay_exp_{uuid.uuid4().hex[:6]}"
    event_id = f"evt_exp_{uuid.uuid4().hex[:6]}"
    mock_plink_id = f"plink_smart_exp_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 199900,
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "The card has expired. Please use a valid card.",
                    "error_reason": "card_expired"
                }
            }
        }
    }

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": mock_plink_id,
            "short_url": f"https://rzp.io/i/{mock_plink_id}",
            "status": "created"
        }
        mock_post.return_value = mock_resp

        res = make_signed_request(client, payload)
        assert res.status_code == 200
        assert mock_post.called

        sent_body = mock_post.call_args[1]["json"]
        assert "valid card or upi" in sent_body["description"].lower()

        db: Session = SessionLocal()
        try:
            attempt = db.query(RecoveryAttemptModel).filter(
                RecoveryAttemptModel.transaction_id == payment_id
            ).first()
            assert attempt is not None
            assert attempt.failure_reason == "card_expired"
            assert attempt.payment_link_id == mock_plink_id
        finally:
            db.close()


# 6. PAYMENT CAPTURED CANCELS PENDING RETRY SCHEDULE
def test_payment_captured_cancels_pending_retry_schedule(client):
    payment_id = f"pay_cancelsched_{uuid.uuid4().hex[:6]}"
    event_id_fail = f"evt_f_sched_{uuid.uuid4().hex[:6]}"
    event_id_cap = f"evt_c_sched_{uuid.uuid4().hex[:6]}"

    # Step 1: payment.failed triggers bank downtime cooldown schedule
    fail_payload = {
        "event": "payment.failed",
        "event_id": event_id_fail,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 320000,
                    "currency": "INR",
                    "status": "failed",
                    "error_code": "GATEWAY_ERROR",
                    "error_description": "Bank switch timed out",
                    "error_source": "bank_switch"
                }
            }
        }
    }
    res_fail = make_signed_request(client, fail_payload)
    assert res_fail.status_code == 200

    db: Session = SessionLocal()
    try:
        sched = db.query(RecoveryScheduleModel).filter(
            RecoveryScheduleModel.transaction_id == payment_id
        ).first()
        assert sched is not None
        assert sched.status in ["pending", "executing"]
    finally:
        db.close()

    # Step 2: Customer pays on alternate route -> payment.captured webhook arrives
    cap_payload = {
        "event": "payment.captured",
        "event_id": event_id_cap,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 320000,
                    "currency": "INR",
                    "status": "captured"
                }
            }
        }
    }
    res_cap = make_signed_request(client, cap_payload)
    assert res_cap.status_code == 200

    db = SessionLocal()
    try:
        # Confirm schedule was automatically cancelled
        sched_after = db.query(RecoveryScheduleModel).filter(
            RecoveryScheduleModel.transaction_id == payment_id
        ).first()
        assert sched_after is not None
        assert sched_after.status == "cancelled"
    finally:
        db.close()
