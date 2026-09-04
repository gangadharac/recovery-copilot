import uuid
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.main import app
from app.db.database import SessionLocal, init_db
from app.db.models import TransactionModel, AuditLogModel, RecoveryAttemptModel, WebhookEventModel
from app.db.recovery_state import RecoveryStatus, transition_recovery_attempt, can_transition
from app.webhooks.webhook_security import generate_test_signature
from app.integrations.razorpay.payment_links import RazorpayPaymentLinkAdapter
from app.agents.tools.payment_link_tool import PaymentLinkTool
from app.schemas.transaction import Transaction, PaymentMethod
from app.webhooks.audit_logger import audit_logger, SecurityAuditLogger

from app.config import settings
from app.integrations.razorpay.client import RazorpayClient, razorpay_client

TEST_KEY_ID = "rzp_test_secKey123456"
TEST_KEY_SECRET = "test_secret_hardening_abc"
TEST_SECRET = "test_webhook_secret_phase61_sec"

@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setattr(razorpay_client, "_key_id", TEST_KEY_ID)
    monkeypatch.setattr(razorpay_client, "_key_secret", TEST_KEY_SECRET)
    init_db()

@pytest.fixture
def adapter():
    test_client = RazorpayClient(key_id=TEST_KEY_ID, key_secret=TEST_KEY_SECRET)
    return RazorpayPaymentLinkAdapter(client=test_client)

@pytest.fixture
def client():
    return TestClient(app)

def make_signed_request(client, payload_dict, secret=TEST_SECRET):
    raw_body = json.dumps(payload_dict).encode("utf-8")
    sig = generate_test_signature(raw_body, secret)
    headers = {
        "X-Razorpay-Signature": sig,
        "X-Razorpay-Event-Id": payload_dict.get("event_id", f"evt_{uuid.uuid4().hex[:8]}"),
        "Content-Type": "application/json"
    }
    return client.post("/webhooks/razorpay", content=raw_body, headers=headers)


# 1. Missing payment ID in payment.failed
def test_h1_missing_payment_id_rejected(client):
    payload = {
        "event": "payment.failed",
        "event_id": f"evt_nopayid_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "amount": 250000,
                    "currency": "INR",
                    "status": "failed"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    data = response.json()
    assert data["agent_triggered"] is False
    assert "Missing payment.id" in data["message"]


# 2. Missing amount in payment.failed
def test_h2_missing_amount_rejected(client):
    payload = {
        "event": "payment.failed",
        "event_id": f"evt_noamt_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_noamt_{uuid.uuid4().hex[:6]}",
                    "currency": "INR",
                    "status": "failed"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    data = response.json()
    assert data["agent_triggered"] is False
    assert "Missing payment.amount" in data["message"]


# 3. Missing currency in payment.failed
def test_h3_missing_currency_rejected(client):
    payload = {
        "event": "payment.failed",
        "event_id": f"evt_missing_curr_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_nocurr_{uuid.uuid4().hex[:6]}",
                    "amount": 100000,
                    "currency": "", # Explicit empty currency
                    "status": "failed"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    data = response.json()
    assert data["agent_triggered"] is False
    assert "currency" in data["message"].lower()


# 4. Missing customer information preserves None
def test_h4_missing_customer_info_preserves_none(client):
    pay_id = f"pay_nocust_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": f"evt_nocust_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": pay_id,
                    "amount": 350000,
                    "currency": "INR",
                    "status": "failed",
                    "method": "card"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db: Session = SessionLocal()
    try:
        txn = db.query(TransactionModel).filter(TransactionModel.payment_id == pay_id).first()
        assert txn is not None
        assert txn.customer_name is None
        assert txn.customer_phone is None
        assert txn.customer_email is None
        assert txn.customer_id is None
    finally:
        db.close()


# 5. No fabricated customer data
def test_h5_no_fabricated_customer_data(client):
    pay_id = f"pay_nofab_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": f"evt_nofab_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": pay_id,
                    "amount": 150000,
                    "currency": "INR",
                    "status": "failed"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db: Session = SessionLocal()
    try:
        txn = db.query(TransactionModel).filter(TransactionModel.payment_id == pay_id).first()
        assert txn is not None
        assert txn.customer_name != "Razorpay Customer"
        assert txn.customer_name != "Razorpay Customer (Demo)"
        assert txn.customer_phone != "+919876543210"
        assert txn.customer_email != "customer@example.com"
    finally:
        db.close()


# 6. No fabricated payment ID
def test_h6_no_fabricated_payment_id(client):
    payload = {
        "event": "payment.failed",
        "event_id": f"evt_nofabpay_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "amount": 400000,
                    "currency": "INR",
                    "status": "failed"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    assert response.json()["agent_triggered"] is False
    assert response.json()["payment_id"] is None


# 7. Invalid payment.failed payload
def test_h7_invalid_payment_failed_payload(client):
    payload = {
        "event": "payment.failed",
        "event_id": f"evt_inv_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_neg_123",
                    "amount": -500, # Invalid non-positive amount
                    "currency": "INR"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    data = response.json()
    assert data["agent_triggered"] is False
    assert "positive" in data["message"].lower()


# 8. payment_link.paid webhook event
def test_h8_payment_link_paid_verification(client):
    plink_id = f"plink_paid_{uuid.uuid4().hex[:6]}"
    pay_id = f"pay_h8_{uuid.uuid4().hex[:6]}"
    txn_id = f"txn_h8_{uuid.uuid4().hex[:6]}"

    db: Session = SessionLocal()
    try:
        txn = TransactionModel(
            transaction_id=txn_id,
            payment_id=txn_id,
            amount=6299.0,
            currency="INR",
            lifecycle_status="recovery_pending"
        )
        attempt = RecoveryAttemptModel(
            id=f"rec_att_{txn_id}",
            transaction_id=txn_id,
            payment_link_id=plink_id,
            status="pending",
            amount=6299.0,
            currency="INR"
        )
        audit = AuditLogModel(
            audit_id=f"audit_{txn_id}",
            batch_run_id="batch_p61",
            transaction_id=txn_id,
            amount=6299.0,
            execution_status="recovery_pending",
            recovered=False,
            recovered_amount=0.0
        )
        db.add_all([txn, attempt, audit])
        db.commit()
    finally:
        db.close()

    payload = {
        "event": "payment_link.paid",
        "event_id": f"evt_plink_paid_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": plink_id,
                    "amount": 629900,
                    "amount_paid": 629900,
                    "currency": "INR",
                    "status": "paid"
                }
            },
            "payment": {
                "entity": {
                    "id": pay_id,
                    "amount": 629900,
                    "currency": "INR",
                    "status": "captured",
                    "payment_link_id": plink_id
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db = SessionLocal()
    try:
        att = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.payment_link_id == plink_id).first()
        assert att.status == "recovered"
        assert att.verification_source == "payment_link.paid"
        assert att.verified_amount == 6299.0
        assert att.recovered_at is not None

        aud = db.query(AuditLogModel).filter(AuditLogModel.transaction_id == txn_id).first()
        assert aud.recovered is True
        assert aud.recovered_amount == 6299.0
    finally:
        db.close()


# 9. payment.captured webhook event
def test_h9_payment_captured_verification(client):
    plink_id = f"plink_cap_{uuid.uuid4().hex[:6]}"
    pay_id = f"pay_h9_{uuid.uuid4().hex[:6]}"
    txn_id = f"txn_h9_{uuid.uuid4().hex[:6]}"

    db: Session = SessionLocal()
    try:
        attempt = RecoveryAttemptModel(
            id=f"rec_att_{txn_id}",
            transaction_id=txn_id,
            payment_link_id=plink_id,
            status="pending",
            amount=1999.0,
            currency="INR"
        )
        db.add(attempt)
        db.commit()
    finally:
        db.close()

    payload = {
        "event": "payment.captured",
        "event_id": f"evt_cap_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": pay_id,
                    "payment_link_id": plink_id,
                    "amount": 199900,
                    "currency": "INR",
                    "status": "captured"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db = SessionLocal()
    try:
        att = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.payment_link_id == plink_id).first()
        assert att.status == "recovered"
        assert att.verification_source == "payment.captured"
    finally:
        db.close()


# 10. order.paid webhook event
def test_h10_order_paid_verification(client):
    order_id = f"order_h10_{uuid.uuid4().hex[:6]}"
    plink_id = f"plink_ord_{uuid.uuid4().hex[:6]}"
    txn_id = f"txn_h10_{uuid.uuid4().hex[:6]}"

    db: Session = SessionLocal()
    try:
        attempt = RecoveryAttemptModel(
            id=f"rec_att_{txn_id}",
            transaction_id=txn_id,
            payment_link_id=plink_id,
            order_id=order_id,
            status="pending",
            amount=850.0,
            currency="INR"
        )
        db.add(attempt)
        db.commit()
    finally:
        db.close()

    payload = {
        "event": "order.paid",
        "event_id": f"evt_ord_{uuid.uuid4().hex[:6]}",
        "payload": {
            "order": {
                "entity": {
                    "id": order_id,
                    "amount": 85000,
                    "amount_paid": 85000,
                    "currency": "INR",
                    "status": "paid"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db = SessionLocal()
    try:
        att = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.payment_link_id == plink_id).first()
        assert att.status == "recovered"
        assert att.verification_source == "order.paid"
    finally:
        db.close()


# 11. Payment Link correlation priority
def test_h11_payment_link_correlation_priority(client):
    plink_id = f"plink_prio_{uuid.uuid4().hex[:6]}"
    txn_id = f"txn_prio_{uuid.uuid4().hex[:6]}"

    db: Session = SessionLocal()
    try:
        attempt = RecoveryAttemptModel(
            id=f"rec_att_{txn_id}",
            transaction_id=txn_id,
            payment_link_id=plink_id,
            status="pending",
            amount=3000.0,
            currency="INR"
        )
        db.add(attempt)
        db.commit()
    finally:
        db.close()

    payload = {
        "event": "payment_link.paid",
        "event_id": f"evt_prio_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": plink_id,
                    "amount": 300000,
                    "currency": "INR",
                    "status": "paid"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db = SessionLocal()
    try:
        att = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.payment_link_id == plink_id).first()
        assert att.status == "recovered"
    finally:
        db.close()


# 12. Amount match
def test_h12_amount_match(client):
    plink_id = f"plink_match_{uuid.uuid4().hex[:6]}"
    txn_id = f"txn_match_{uuid.uuid4().hex[:6]}"

    db: Session = SessionLocal()
    try:
        attempt = RecoveryAttemptModel(
            id=f"rec_att_{txn_id}",
            transaction_id=txn_id,
            payment_link_id=plink_id,
            status="pending",
            amount=500.0,
            currency="INR"
        )
        db.add(attempt)
        db.commit()
    finally:
        db.close()

    payload = {
        "event": "payment_link.paid",
        "event_id": f"evt_match_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": plink_id,
                    "amount": 50000, # exact 500.0 INR
                    "currency": "INR"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db = SessionLocal()
    try:
        att = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.payment_link_id == plink_id).first()
        assert att.status == "recovered"
    finally:
        db.close()


# 13. Amount mismatch triggers verification_failed
def test_h13_amount_mismatch_fails_verification(client):
    plink_id = f"plink_mism_{uuid.uuid4().hex[:6]}"
    txn_id = f"txn_mism_{uuid.uuid4().hex[:6]}"

    db: Session = SessionLocal()
    try:
        attempt = RecoveryAttemptModel(
            id=f"rec_att_{txn_id}",
            transaction_id=txn_id,
            payment_link_id=plink_id,
            status="pending",
            amount=6299.0, # Attempt was for 6299
            currency="INR"
        )
        audit = AuditLogModel(
            audit_id=f"audit_{txn_id}",
            batch_run_id="batch_p61",
            transaction_id=txn_id,
            amount=6299.0,
            execution_status="recovery_pending",
            recovered=False,
            recovered_amount=0.0
        )
        db.add_all([attempt, audit])
        db.commit()
    finally:
        db.close()

    # Razorpay event arrives with only 4000.0 INR (400000 paise)
    payload = {
        "event": "payment_link.paid",
        "event_id": f"evt_mism_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": plink_id,
                    "amount": 400000, # 4000.0 INR != 6299.0 INR
                    "currency": "INR",
                    "status": "paid"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db = SessionLocal()
    try:
        att = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.payment_link_id == plink_id).first()
        assert att.status == "verification_failed"
        assert "mismatch" in att.error_message.lower()

        # Audit log MUST NOT be marked recovered!
        aud = db.query(AuditLogModel).filter(AuditLogModel.transaction_id == txn_id).first()
        assert aud.recovered is False
        assert aud.recovered_amount == 0.0
    finally:
        db.close()


# 14. Currency match
def test_h14_currency_match(client):
    plink_id = f"plink_currmatch_{uuid.uuid4().hex[:6]}"
    txn_id = f"txn_currmatch_{uuid.uuid4().hex[:6]}"

    db: Session = SessionLocal()
    try:
        attempt = RecoveryAttemptModel(
            id=f"rec_att_{txn_id}",
            transaction_id=txn_id,
            payment_link_id=plink_id,
            status="pending",
            amount=100.0,
            currency="INR"
        )
        db.add(attempt)
        db.commit()
    finally:
        db.close()

    payload = {
        "event": "payment_link.paid",
        "event_id": f"evt_currmatch_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": plink_id,
                    "amount": 10000,
                    "currency": "INR",
                    "status": "paid"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db = SessionLocal()
    try:
        att = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.payment_link_id == plink_id).first()
        assert att.status == "recovered"
    finally:
        db.close()


# 15. Currency mismatch triggers verification_failed
def test_h15_currency_mismatch_fails_verification(client):
    plink_id = f"plink_currmism_{uuid.uuid4().hex[:6]}"
    txn_id = f"txn_currmism_{uuid.uuid4().hex[:6]}"

    db: Session = SessionLocal()
    try:
        attempt = RecoveryAttemptModel(
            id=f"rec_att_{txn_id}",
            transaction_id=txn_id,
            payment_link_id=plink_id,
            status="pending",
            amount=100.0,
            currency="INR" # INR expected
        )
        db.add(attempt)
        db.commit()
    finally:
        db.close()

    payload = {
        "event": "payment_link.paid",
        "event_id": f"evt_currmism_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": plink_id,
                    "amount": 10000,
                    "currency": "USD", # USD received
                    "status": "paid"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db = SessionLocal()
    try:
        att = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.payment_link_id == plink_id).first()
        assert att.status == "verification_failed"
        assert "currency mismatch" in att.error_message.lower()
    finally:
        db.close()


# 16. Duplicate RecoveryAttempt prevention
def test_h16_duplicate_recovery_attempt_prevented(adapter):
    txn_id = f"txn_dup_prev_{uuid.uuid4().hex[:6]}"

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": f"plink_dup_{uuid.uuid4().hex[:6]}",
            "short_url": "https://rzp.io/i/dup123",
            "status": "created"
        }
        mock_post.return_value = mock_resp

        # First call creates link
        res1 = adapter.create_payment_link(amount_inr=1500.0, transaction_id=txn_id)
        assert res1["success"] is True
        assert res1["is_idempotent_reuse"] is False

        # Second call reuses existing link
        res2 = adapter.create_payment_link(amount_inr=1500.0, transaction_id=txn_id)
        assert res2["success"] is True
        assert res2["is_idempotent_reuse"] is True
        assert res2["payment_link_id"] == res1["payment_link_id"]
        assert mock_post.call_count == 1 # Only 1 API call made!


# 17. Concurrent recovery protection
def test_h17_state_machine_transition_controls():
    # Valid transitions
    assert can_transition("created", "pending") is True
    assert can_transition("pending", "paid_verification_pending") is True
    assert can_transition("paid_verification_pending", "recovered") is True
    assert can_transition("paid_verification_pending", "verification_failed") is True

    # Invalid transitions
    assert can_transition("recovered", "pending") is False
    assert can_transition("verification_failed", "recovered") is False
    assert can_transition("failed", "recovered") is False


# 18. Payment Link creation remains recovery_pending
def test_h18_link_creation_remains_pending(adapter):
    tool = PaymentLinkTool(adapter=adapter)
    txn = Transaction(
        transaction_id=f"txn_pend_{uuid.uuid4().hex[:6]}",
        amount=2500.0,
        currency="INR",
        payment_method=PaymentMethod.CARD,
        error_code="GATEWAY_ERROR",
        error_description="Gateway timeout",
        error_source="bank_switch",
        timestamp=datetime.now(timezone.utc)
    )

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "plink_pending_test",
            "short_url": "https://rzp.io/i/pend001",
            "status": "created"
        }
        mock_post.return_value = mock_resp

        tool_res = tool.execute(txn)
        assert tool_res.success is True
        assert tool_res.status == "recovery_pending"
        assert tool_res.recovered_amount == 0.0 # NOT recovered yet!


# 19. Successful verification becomes recovered
def test_h19_successful_verification_becomes_recovered(client):
    plink_id = f"plink_h19_{uuid.uuid4().hex[:6]}"
    txn_id = f"txn_h19_{uuid.uuid4().hex[:6]}"

    db: Session = SessionLocal()
    try:
        attempt = RecoveryAttemptModel(
            id=f"rec_att_{txn_id}",
            transaction_id=txn_id,
            payment_link_id=plink_id,
            status="pending",
            amount=999.0,
            currency="INR"
        )
        db.add(attempt)
        db.commit()
    finally:
        db.close()

    payload = {
        "event": "payment_link.paid",
        "event_id": f"evt_h19_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": plink_id,
                    "amount": 99900,
                    "currency": "INR",
                    "status": "paid"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db = SessionLocal()
    try:
        att = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.payment_link_id == plink_id).first()
        assert att.status == "recovered"
        assert att.verified_amount == 999.0
    finally:
        db.close()


# 20. Secrets never appear in logs
def test_h20_secrets_never_in_audit_logs():
    sanitized = SecurityAuditLogger._sanitize({
        "key_secret": "rzp_secret_top_secret_123",
        "webhook_secret": "whsec_super_secret_456",
        "authorization": "Basic dGVzdF9rZXk6dGVzdF9zZWNyZXQ=",
        "safe_transaction_id": "txn_safe_101"
    })
    assert sanitized["key_secret"] == "[REDACTED]"
    assert sanitized["webhook_secret"] == "[REDACTED]"
    assert sanitized["authorization"] == "[REDACTED]"
    assert sanitized["safe_transaction_id"] == "txn_safe_101"
