import json
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from unittest.mock import patch

from app.main import app
from app.config import settings
from app.db.database import SessionLocal, init_db
from app.db.models import WebhookEventModel, TransactionModel, AuditLogModel, AgentTraceModel
from app.webhooks.webhook_security import generate_test_signature
from app.agents.recovery_agent import recovery_agent

TEST_SECRET = "test_webhook_secret_key_trigger"

@pytest.fixture(autouse=True)
def setup_webhook_env(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", TEST_SECRET)
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

# TEST 1: payment.failed -> transaction created/found -> RecoveryAgent invoked
def test_1_payment_failed_invokes_recovery_agent(client):
    event_id = f"evt_t1_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_t1_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 299900,
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Bank timeout during payment session",
                    "error_source": "bank_switch"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["agent_triggered"] is True
    assert "agent_status" in data

    db: Session = SessionLocal()
    try:
        txn = db.query(TransactionModel).filter(TransactionModel.payment_id == payment_id).first()
        assert txn is not None
        assert txn.lifecycle_status == "recovery_attempted"
    finally:
        db.close()

# TEST 2: payment.failed -> RecoveryAgent produces trace
def test_2_payment_failed_produces_agent_traces(client):
    event_id = f"evt_t2_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_t2_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 150000,
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "error_code": "3DS_VERIFICATION_FAILED",
                    "error_description": "OTP verification failed",
                    "error_source": "issuer"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db: Session = SessionLocal()
    try:
        traces = db.query(AgentTraceModel).filter(AgentTraceModel.batch_run_id == f"webhook_{event_id}").all()
        assert len(traces) >= 1
        assert traces[0].transaction_id == payment_id
        assert traces[0].iteration_number == 1
    finally:
        db.close()

# TEST 3: payment.failed -> agent_run_id persisted
def test_3_payment_failed_persists_agent_run_id(client):
    event_id = f"evt_t3_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_t3_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 420000,
                    "currency": "INR",
                    "status": "failed",
                    "error_code": "GATEWAY_ERROR"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db: Session = SessionLocal()
    try:
        audit = db.query(AuditLogModel).filter(AuditLogModel.batch_run_id == f"webhook_{event_id}").first()
        assert audit is not None
        assert audit.agent_run_id is not None
        assert audit.agent_run_id.startswith(f"agent_wh_{payment_id}")
    finally:
        db.close()

# TEST 4: payment.failed duplicate -> RecoveryAgent is NOT invoked twice
def test_4_duplicate_payment_failed_does_not_reinvoke_agent(client):
    event_id = f"evt_t4_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_t4_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 180000,
                    "currency": "INR",
                    "status": "failed",
                    "error_code": "GATEWAY_ERROR"
                }
            }
        }
    }
    # Delivery 1
    res1 = make_signed_request(client, payload)
    assert res1.status_code == 200
    assert res1.json()["agent_triggered"] is True

    # Delivery 2 (Duplicate)
    res2 = make_signed_request(client, payload)
    assert res2.status_code == 200
    assert res2.json()["status"] == "duplicate_ignored"
    assert res2.json()["agent_triggered"] is False

    db: Session = SessionLocal()
    try:
        audits = db.query(AuditLogModel).filter(AuditLogModel.batch_run_id == f"webhook_{event_id}").all()
        assert len(audits) == 1
    finally:
        db.close()

# TEST 5: payment.captured -> RecoveryAgent NOT invoked
def test_5_payment_captured_does_not_invoke_agent(client):
    event_id = f"evt_t5_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_t5_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.captured",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 250000,
                    "status": "captured"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    assert response.json()["agent_triggered"] is False

# TEST 6: order.paid -> RecoveryAgent NOT invoked
def test_6_order_paid_does_not_invoke_agent(client):
    event_id = f"evt_t6_{uuid.uuid4().hex[:6]}"
    order_id = f"order_t6_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "order.paid",
        "event_id": event_id,
        "payload": {
            "order": {
                "entity": {
                    "id": order_id,
                    "amount": 500000,
                    "status": "paid"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    assert response.json()["agent_triggered"] is False

# TEST 7: payment.failed followed by payment.captured -> same transaction correlated
def test_7_failed_then_captured_correlates_same_transaction(client):
    payment_id = f"pay_t7_{uuid.uuid4().hex[:6]}"
    order_id = f"order_t7_{uuid.uuid4().hex[:6]}"

    # Step 1: payment.failed
    fail_payload = {
        "event": "payment.failed",
        "event_id": f"evt_f_t7_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": 650000,
                    "currency": "INR",
                    "status": "failed",
                    "error_code": "GATEWAY_ERROR"
                }
            }
        }
    }
    res_fail = make_signed_request(client, fail_payload)
    assert res_fail.status_code == 200

    # Step 2: payment.captured
    cap_payload = {
        "event": "payment.captured",
        "event_id": f"evt_c_t7_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": 650000,
                    "status": "captured"
                }
            }
        }
    }
    res_cap = make_signed_request(client, cap_payload)
    assert res_cap.status_code == 200

    db: Session = SessionLocal()
    try:
        txns = db.query(TransactionModel).filter(TransactionModel.payment_id == payment_id).all()
        assert len(txns) == 1
        assert txns[0].lifecycle_status == "captured"
    finally:
        db.close()

# TEST 8: payment.captured after recovery -> recovery marked verified
def test_8_payment_captured_marks_recovery_verified(client):
    event_id_fail = f"evt_f_t8_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_t8_{uuid.uuid4().hex[:6]}"

    # Step 1: payment.failed triggers agent
    fail_payload = {
        "event": "payment.failed",
        "event_id": event_id_fail,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 890000,
                    "currency": "INR",
                    "status": "failed",
                    "error_code": "GATEWAY_ERROR"
                }
            }
        }
    }
    make_signed_request(client, fail_payload)

    # Step 2: payment.captured confirms customer paid
    cap_payload = {
        "event": "payment.captured",
        "event_id": f"evt_c_t8_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 890000,
                    "status": "captured"
                }
            }
        }
    }
    make_signed_request(client, cap_payload)

    db: Session = SessionLocal()
    try:
        audit = db.query(AuditLogModel).filter(AuditLogModel.transaction_id == payment_id).first()
        assert audit is not None
        assert audit.recovered is True
        assert audit.execution_status == "recovered"
        assert "RECOVERY VERIFIED" in audit.execution_notes
    finally:
        db.close()

# TEST 9: already captured transaction receives payment.failed -> RecoveryAgent NOT invoked
def test_9_already_captured_transaction_skips_recovery(client):
    payment_id = f"pay_t9_{uuid.uuid4().hex[:6]}"
    
    # Pre-seed transaction as captured
    db: Session = SessionLocal()
    try:
        txn = TransactionModel(
            transaction_id=payment_id,
            payment_id=payment_id,
            amount=3500.0,
            currency="INR",
            lifecycle_status="captured",
            error_code="NONE"
        )
        db.add(txn)
        db.commit()
    finally:
        db.close()

    # Incoming late payment.failed event for the captured payment
    fail_payload = {
        "event": "payment.failed",
        "event_id": f"evt_late_t9_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 350000,
                    "status": "failed",
                    "error_code": "LATE_TIMEOUT"
                }
            }
        }
    }
    response = make_signed_request(client, fail_payload)
    assert response.status_code == 200
    assert response.json()["agent_triggered"] is False

# TEST 10: missing optional webhook fields -> system handles safely
def test_10_missing_optional_fields_handles_safely(client):
    event_id = f"evt_t10_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_t10_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id
                    # missing amount, error_code, customer details
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["agent_triggered"] is True

# TEST 11: agent execution failure -> webhook service does not crash unexpectedly, failure audited
def test_11_agent_execution_failure_handled_gracefully(client):
    event_id = f"evt_t11_{uuid.uuid4().hex[:6]}"
    payment_id = f"pay_t11_{uuid.uuid4().hex[:6]}"
    payload = {
        "event": "payment.failed",
        "event_id": event_id,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 100000,
                    "status": "failed"
                }
            }
        }
    }
    with patch.object(recovery_agent, "recover", side_effect=RuntimeError("Simulated agent engine crash")):
        response = make_signed_request(client, payload)
        assert response.status_code == 200
        assert response.json()["agent_triggered"] is True
        assert response.json()["agent_status"] == "abandoned"

# TEST 12: multiple different payment.failed events -> each unique payment can have its own RecoveryAgent run
def test_12_multiple_unique_payment_failures_trigger_separate_runs(client):
    p1 = f"pay_multi_1_{uuid.uuid4().hex[:6]}"
    p2 = f"pay_multi_2_{uuid.uuid4().hex[:6]}"

    payload1 = {
        "event": "payment.failed",
        "event_id": f"evt_m1_{uuid.uuid4().hex[:6]}",
        "payload": {"payment": {"entity": {"id": p1, "amount": 120000, "error_code": "GATEWAY_ERROR"}}}
    }
    payload2 = {
        "event": "payment.failed",
        "event_id": f"evt_m2_{uuid.uuid4().hex[:6]}",
        "payload": {"payment": {"entity": {"id": p2, "amount": 340000, "error_code": "insufficient_funds"}}}
    }

    res1 = make_signed_request(client, payload1)
    res2 = make_signed_request(client, payload2)

    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json()["agent_triggered"] is True
    assert res2.json()["agent_triggered"] is True

    db: Session = SessionLocal()
    try:
        a1 = db.query(AuditLogModel).filter(AuditLogModel.transaction_id == p1).first()
        a2 = db.query(AuditLogModel).filter(AuditLogModel.transaction_id == p2).first()
        assert a1 is not None
        assert a2 is not None
        assert a1.agent_run_id != a2.agent_run_id
    finally:
        db.close()
