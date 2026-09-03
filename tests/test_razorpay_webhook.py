import json
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.config import settings
from app.db.database import SessionLocal, init_db
from app.db.models import WebhookEventModel, TransactionModel
from app.webhooks.webhook_security import generate_test_signature, verify_razorpay_signature

TEST_SECRET = "test_webhook_secret_key_12345"

@pytest.fixture(autouse=True)
def setup_webhook_env(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", TEST_SECRET)
    init_db()

@pytest.fixture
def client():
    return TestClient(app)

def make_signed_request(client, payload_dict, secret=TEST_SECRET, custom_signature=None, headers=None):
    raw_body = json.dumps(payload_dict).encode("utf-8")
    sig = custom_signature if custom_signature is not None else generate_test_signature(raw_body, secret)
    req_headers = {"X-Razorpay-Signature": sig}
    if headers:
        req_headers.update(headers)
    return client.post("/webhooks/razorpay", content=raw_body, headers=req_headers)

# 1. Valid webhook signature
def test_1_valid_webhook_signature(client):
    payload = {
        "event": "payment.authorized",
        "event_id": f"evt_test_valid_sig_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_001",
                    "amount": 250000,
                    "currency": "INR",
                    "status": "authorized"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["event_type"] == "payment.authorized"

# 2. Invalid webhook signature
def test_2_invalid_webhook_signature(client):
    payload = {
        "event": "payment.failed",
        "event_id": f"evt_test_invalid_sig_{uuid.uuid4().hex[:6]}",
        "payload": {"payment": {"entity": {"id": "pay_test_002", "amount": 10000}}}
    }
    response = make_signed_request(client, payload, custom_signature="invalid_signature_hex_123")
    assert response.status_code == 401
    assert "Invalid webhook signature" in response.json()["detail"]

# 3. Missing signature
def test_3_missing_webhook_signature(client):
    raw_body = json.dumps({"event": "payment.failed"}).encode("utf-8")
    response = client.post("/webhooks/razorpay", content=raw_body)
    assert response.status_code == 401
    assert "Missing X-Razorpay-Signature" in response.json()["detail"]

# 4. payment.failed ingestion
def test_4_payment_failed_ingestion(client):
    event_id = f"evt_pay_failed_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_fail_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 499900, # INR 4,999.00
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Bank switch timeout while negotiating 3DS",
                    "error_source": "bank_switch",
                    "email": "rohan.sharma@example.com",
                    "contact": "+919876543210"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["payment_id"] == payment_id

    # Verify stored in DB
    db: Session = SessionLocal()
    try:
        wh_event = db.query(WebhookEventModel).filter(WebhookEventModel.event_id == event_id).first()
        assert wh_event is not None
        assert wh_event.event_type == "payment.failed"
        assert wh_event.payment_id == payment_id

        txn = db.query(TransactionModel).filter(TransactionModel.payment_id == payment_id).first()
        assert txn is not None
        assert txn.amount == 4999.00
        assert txn.lifecycle_status in ["payment_failed", "recovery_attempted"]
        assert txn.error_code == "BAD_REQUEST_ERROR"
    finally:
        db.close()

# 5. payment.captured ingestion
def test_5_payment_captured_ingestion(client):
    event_id = f"evt_captured_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_cap_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.captured",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 150000,
                    "currency": "INR",
                    "status": "captured",
                    "method": "upi",
                    "vpa": "user@okhdfcbank"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["event_type"] == "payment.captured"

# 6. order.paid ingestion
def test_6_order_paid_ingestion(client):
    event_id = f"evt_order_paid_{uuid.uuid4().hex[:6]}"
    order_id = f"order_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "order.paid",
        "event_id": event_id,
        "payload": {
            "order": {
                "entity": {
                    "id": order_id,
                    "amount": 349900,
                    "status": "paid"
                }
            },
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:6]}",
                    "order_id": order_id,
                    "status": "captured"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["order_id"] == order_id

# 7. payment.authorized ingestion
def test_7_payment_authorized_ingestion(client):
    event_id = f"evt_auth_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_auth_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.authorized",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 89900,
                    "status": "authorized"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    assert response.json()["event_type"] == "payment.authorized"

# 8. Duplicate webhook handling (idempotency)
def test_8_duplicate_webhook_handling(client):
    event_id = f"evt_duplicate_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_dup_{uuid.uuid4().hex[:6]}",
                    "amount": 210000,
                    "currency": "INR",
                    "status": "failed"
                }
            }
        }
    }
    # First delivery
    res1 = make_signed_request(client, payload)
    assert res1.status_code == 200
    assert res1.json()["status"] == "success"

    # Second (duplicate) delivery
    res2 = make_signed_request(client, payload)
    assert res2.status_code == 200
    assert res2.json()["status"] == "duplicate_ignored"

# 9. Duplicate event does not create duplicate records
def test_9_duplicate_event_does_not_create_duplicate_records(client):
    event_id = f"evt_dup_check_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_dup_rec_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 120000,
                    "status": "failed"
                }
            }
        }
    }
    make_signed_request(client, payload)
    make_signed_request(client, payload) # Duplicate delivery

    db: Session = SessionLocal()
    try:
        event_count = db.query(WebhookEventModel).filter(WebhookEventModel.event_id == event_id).count()
        assert event_count == 1
        txn_count = db.query(TransactionModel).filter(TransactionModel.payment_id == payment_id).count()
        assert txn_count == 1
    finally:
        db.close()

# 10. Malformed JSON handling
def test_10_malformed_json_handling(client):
    raw_bad_body = b"NOT_VALID_JSON{abc:123"
    sig = generate_test_signature(raw_bad_body, TEST_SECRET)
    response = client.post("/webhooks/razorpay", content=raw_bad_body, headers={"X-Razorpay-Signature": sig})
    assert response.status_code == 400
    assert "Malformed JSON" in response.json()["detail"]

# 11. Missing important fields handling
def test_11_missing_fields_handling(client):
    event_id = f"evt_missing_{uuid.uuid4().hex[:6]}"
    payload = {"event": "payment.failed", "event_id": event_id, "payload": {}} # missing payment entity
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "success"

# 12. Unknown event type handling
def test_12_unknown_event_type_handled_safely(client):
    event_id = f"evt_unknown_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "subscription.halted",
        "event_id": event_id,
        "payload": {"subscription": {"id": "sub_123"}}
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["event_type"] == "subscription.halted"

# 13. Secrets are never returned in health or responses
def test_13_secrets_are_never_returned(client):
    res_health = client.get("/webhooks/razorpay/health")
    assert res_health.status_code == 200
    health_data = res_health.json()
    assert health_data["razorpay_webhook_configured"] is True
    # Verify no secrets in keys or values
    for k, v in health_data.items():
        assert TEST_SECRET not in str(k)
        assert TEST_SECRET not in str(v)

# 14. payment.failed followed by payment.captured correlation
def test_14_payment_failed_then_captured_correlation(client):
    payment_id = f"pay_corr_{uuid.uuid4().hex[:6]}"
    order_id = f"order_corr_{uuid.uuid4().hex[:6]}"

    # Step A: payment.failed arrives
    fail_payload = {
        "event": "payment.failed",
        "event_id": f"evt_f_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": 750000,
                    "currency": "INR",
                    "status": "failed",
                    "error_code": "GATEWAY_ERROR"
                }
            }
        }
    }
    res_fail = make_signed_request(client, fail_payload)
    assert res_fail.status_code == 200

    db: Session = SessionLocal()
    try:
        txn_after_fail = db.query(TransactionModel).filter(TransactionModel.payment_id == payment_id).first()
        assert txn_after_fail is not None
        assert txn_after_fail.lifecycle_status in ["payment_failed", "recovery_attempted"]

        # Step B: payment.captured arrives later for the same payment/order
        capture_payload = {
            "event": "payment.captured",
            "event_id": f"evt_c_{uuid.uuid4().hex[:6]}",
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": order_id,
                        "amount": 750000,
                        "currency": "INR",
                        "status": "captured",
                        "method": "upi"
                    }
                }
            }
        }
        res_cap = make_signed_request(client, capture_payload)
        assert res_cap.status_code == 200

        # Step C: Verify Transaction lifecycle_status is now correlated and updated to 'captured'
        db.refresh(txn_after_fail)
        assert txn_after_fail.lifecycle_status == "captured"
    finally:
        db.close()

# 15. Webhook processing failure handling
def test_15_webhook_signature_verification_helper():
    raw_bytes = b'{"event":"test"}'
    valid_sig = generate_test_signature(raw_bytes, "secret123")
    assert verify_razorpay_signature(raw_bytes, valid_sig, "secret123") is True
    assert verify_razorpay_signature(raw_bytes, "tampered_sig", "secret123") is False
    assert verify_razorpay_signature(raw_bytes, valid_sig, "wrong_secret") is False
    assert verify_razorpay_signature(raw_bytes, None, "secret123") is False
