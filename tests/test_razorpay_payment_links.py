import json
import uuid
import logging
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
import requests
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.db.database import SessionLocal, init_db
from app.db.models import TransactionModel, AuditLogModel, RecoveryAttemptModel, WebhookEventModel
from app.integrations.razorpay.client import RazorpayClient
from app.integrations.razorpay.payment_links import RazorpayPaymentLinkAdapter
from app.agents.tools.payment_link_tool import PaymentLinkTool
from app.schemas.transaction import Transaction, PaymentMethod, PaymentMethodDetails, CustomerHistory
from app.schemas.agent_state import AgentToolName
from app.webhooks.webhook_security import generate_test_signature
from app.reporting.dashboard import load_db_data

TEST_KEY_ID = "rzp_test_sampleKeyId12345"
TEST_KEY_SECRET = "sampleSecretKeySafeSecret999"
TEST_WEBHOOK_SECRET = "test_webhook_secret_phase6"

@pytest.fixture(autouse=True)
def setup_env(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
    init_db()

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def adapter():
    custom_client = RazorpayClient(key_id=TEST_KEY_ID, key_secret=TEST_KEY_SECRET)
    return RazorpayPaymentLinkAdapter(client=custom_client)

@pytest.fixture
def sample_txn():
    return Transaction(
        transaction_id=f"txn_test_{uuid.uuid4().hex[:8]}",
        customer_id="cust_test_404",
        customer_name="Aarav Sharma",
        customer_phone="+919123456780",
        customer_email="aarav.sharma@domain.in",
        amount=6299.0,
        currency="INR",
        payment_method=PaymentMethod.CARD,
        payment_method_details=PaymentMethodDetails(bank_code="HDFC"),
        error_code="3DS_VERIFICATION_FAILED",
        error_description="OTP expired",
        error_source="issuer",
        retry_count=0,
        last_retry_timestamp=None,
        timestamp=datetime.now(timezone.utc),
        auto_charge_consent=False,
        customer_history=CustomerHistory()
    )

def make_signed_request(client, payload_dict, secret=TEST_WEBHOOK_SECRET):
    raw_body = json.dumps(payload_dict).encode("utf-8")
    sig = generate_test_signature(raw_body, secret)
    return client.post(
        "/webhooks/razorpay",
        content=raw_body,
        headers={"X-Razorpay-Signature": sig}
    )

# 1. Razorpay configuration loads correctly and enforces test mode
def test_1_razorpay_config_loads_correctly():
    assert settings.RAZORPAY_KEY_ID == TEST_KEY_ID
    assert settings.is_test_mode is True
    assert settings.is_razorpay_configured is True
    settings.validate_test_mode_safety()

    # Verify live credentials trigger safety guard
    client_live = RazorpayClient(key_id="rzp_live_abc123", key_secret="secret")
    with pytest.raises(ValueError, match="SECURITY GUARD TRIGGERED"):
        client_live.validate_safety()

# 2. Missing API key fails safely
def test_2_missing_api_key_fails_safely():
    client_no_key = RazorpayClient(key_id="", key_secret="secret")
    adapter_no_key = RazorpayPaymentLinkAdapter(client=client_no_key)
    res = adapter_no_key.create_payment_link(amount_inr=500.0, transaction_id="txn_no_key_test")
    assert res["success"] is False
    assert res["status"] == "failed"
    assert "not configured" in res["error_message"].lower()

# 3. Missing API secret fails safely
def test_3_missing_api_secret_fails_safely():
    client_no_sec = RazorpayClient(key_id="rzp_test_123", key_secret="")
    adapter_no_sec = RazorpayPaymentLinkAdapter(client=client_no_sec)
    res = adapter_no_sec.create_payment_link(amount_inr=500.0, transaction_id="txn_no_sec_test")
    assert res["success"] is False
    assert res["status"] == "failed"
    assert "not configured" in res["error_message"].lower()

# 4. Payment Link request amount conversion (rupees -> paise)
def test_4_payment_link_amount_conversion(adapter):
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "plink_amt_test_123",
            "short_url": "https://rzp.io/i/amt123",
            "status": "created"
        }
        mock_post.return_value = mock_resp

        res = adapter.create_payment_link(
            amount_inr=62.99,
            transaction_id=f"txn_amt_conv_{uuid.uuid4().hex[:6]}"
        )
        assert res["success"] is True
        assert mock_post.called
        call_kwargs = mock_post.call_args[1]
        sent_payload = call_kwargs["json"]
        assert sent_payload["amount"] == 6299 # 62.99 * 100 paise

# 5. Correct currency handling
def test_5_correct_currency_handling(adapter):
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "plink_curr_1", "short_url": "https://rzp.io/i/curr1"}
        mock_post.return_value = mock_resp

        adapter.create_payment_link(
            amount_inr=100.0,
            currency="inr",
            transaction_id=f"txn_curr_{uuid.uuid4().hex[:6]}"
        )
        sent_payload = mock_post.call_args[1]["json"]
        assert sent_payload["currency"] == "INR"

# 6. Successful Payment Link creation returns recovery_pending
def test_6_successful_payment_link_creation(adapter):
    txn_id = f"txn_succ_{uuid.uuid4().hex[:6]}"
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "plink_succ_999",
            "short_url": "https://rzp.io/i/succ999",
            "status": "created"
        }
        mock_post.return_value = mock_resp

        res = adapter.create_payment_link(amount_inr=2499.0, transaction_id=txn_id)
        assert res["success"] is True
        assert res["status"] == "recovery_pending"
        assert res["payment_link_id"] == "plink_succ_999"
        assert res["payment_link_url"] == "https://rzp.io/i/succ999"

# 7. Razorpay API failure returns safe error
def test_7_razorpay_api_failure(adapter):
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {
            "error": {"code": "BAD_REQUEST_ERROR", "description": "Invalid customer phone number format"}
        }
        mock_post.return_value = mock_resp

        res = adapter.create_payment_link(
            amount_inr=1500.0,
            transaction_id=f"txn_api_fail_{uuid.uuid4().hex[:6]}"
        )
        assert res["success"] is False
        assert res["status"] == "failed"
        assert "Invalid customer phone number format" in res["error_message"]

# 8. Razorpay timeout handling
def test_8_razorpay_timeout(adapter):
    with patch("requests.post", side_effect=requests.exceptions.Timeout("Connection timed out")):
        res = adapter.create_payment_link(
            amount_inr=1000.0,
            transaction_id=f"txn_timeout_{uuid.uuid4().hex[:6]}"
        )
        assert res["success"] is False
        assert res["status"] == "failed"
        assert "timed out" in res["error_message"].lower()

# 9. Invalid / malformed API response handling
def test_9_invalid_response_handling(adapter):
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_resp.text = "Bad Gateway"
        mock_resp.json.side_effect = ValueError("No JSON")
        mock_post.return_value = mock_resp

        res = adapter.create_payment_link(
            amount_inr=1000.0,
            transaction_id=f"txn_bad_gw_{uuid.uuid4().hex[:6]}"
        )
        assert res["success"] is False
        assert res["status"] == "failed"
        assert "502" in res["error_message"]

# 10. Payment Link ID persistence in database
def test_10_payment_link_id_persistence(adapter):
    txn_id = f"txn_persist_id_{uuid.uuid4().hex[:6]}"
    plink_id = f"plink_p_id_{uuid.uuid4().hex[:6]}"
    plink_url = f"https://rzp.io/i/{plink_id}"

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": plink_id, "short_url": plink_url, "status": "created"}
        mock_post.return_value = mock_resp

        res = adapter.create_payment_link(amount_inr=3500.0, transaction_id=txn_id)
        assert res["success"] is True

    db: Session = SessionLocal()
    try:
        attempt = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.transaction_id == txn_id).first()
        assert attempt is not None
        assert attempt.payment_link_id == plink_id
        assert attempt.status == "pending"
    finally:
        db.close()

# 11. Payment Link URL persistence in database
def test_11_payment_link_url_persistence(adapter):
    txn_id = f"txn_persist_url_{uuid.uuid4().hex[:6]}"
    plink_id = f"plink_p_url_{uuid.uuid4().hex[:6]}"
    plink_url = f"https://rzp.io/i/{plink_id}"

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": plink_id, "short_url": plink_url, "status": "created"}
        mock_post.return_value = mock_resp

        res = adapter.create_payment_link(amount_inr=4200.0, transaction_id=txn_id)
        assert res["success"] is True

    db: Session = SessionLocal()
    try:
        attempt = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.transaction_id == txn_id).first()
        assert attempt is not None
        assert attempt.payment_link_url == plink_url
        assert attempt.amount == 4200.0
    finally:
        db.close()

# 12. Idempotency: Duplicate recovery attempt does not create another link
def test_12_duplicate_recovery_attempt_idempotency(adapter):
    txn_id = f"txn_idemp_{uuid.uuid4().hex[:6]}"
    plink_id = f"plink_idemp_{uuid.uuid4().hex[:6]}"

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": plink_id, "short_url": f"https://rzp.io/i/{plink_id}"}
        mock_post.return_value = mock_resp

        # First call: calls Razorpay API
        res1 = adapter.create_payment_link(amount_inr=1200.0, transaction_id=txn_id)
        assert res1["success"] is True
        assert res1["is_idempotent_reuse"] is False
        assert mock_post.call_count == 1

        # Second call: detects existing pending link, does NOT call Razorpay API
        res2 = adapter.create_payment_link(amount_inr=1200.0, transaction_id=txn_id)
        assert res2["success"] is True
        assert res2["is_idempotent_reuse"] is True
        assert res2["payment_link_id"] == plink_id
        assert mock_post.call_count == 1 # Still 1 call!

# 13. Payment Link creation results in recovery_pending (recovered_amount = 0.0)
def test_13_payment_link_creation_results_in_recovery_pending(adapter, sample_txn):
    tool = PaymentLinkTool(adapter=adapter)
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "plink_tool_01", "short_url": "https://rzp.io/i/tool01"}
        mock_post.return_value = mock_resp

        result = tool.execute(sample_txn)
        assert result.success is True
        assert result.status == "recovery_pending"
        assert result.recovered_amount == 0.0 # Revenue is NOT yet recovered!
        assert result.result_data["payment_link_id"] == "plink_tool_01"

# 14. payment.captured changes pending -> recovered in RecoveryAttemptModel
def test_14_payment_captured_changes_pending_to_recovered(client, adapter):
    payment_id = f"pay_cap_{uuid.uuid4().hex[:6]}"
    plink_id = f"plink_cap_{uuid.uuid4().hex[:6]}"

    # Seed transaction & recovery attempt
    db: Session = SessionLocal()
    try:
        txn = TransactionModel(
            transaction_id=payment_id,
            payment_id=payment_id,
            amount=4500.0,
            currency="INR",
            lifecycle_status="recovery_pending"
        )
        attempt = RecoveryAttemptModel(
            id=f"rec_att_{payment_id}",
            transaction_id=payment_id,
            payment_link_id=plink_id,
            payment_link_url=f"https://rzp.io/i/{plink_id}",
            status="pending",
            amount=4500.0
        )
        audit = AuditLogModel(
            audit_id=f"audit_cap_{payment_id}",
            batch_run_id="batch_test_p6",
            transaction_id=payment_id,
            amount=4500.0,
            execution_status="recovery_pending",
            recovered=False,
            recovered_amount=0.0
        )
        db.add_all([txn, attempt, audit])
        db.commit()
    finally:
        db.close()

    # payment.captured arrives
    payload = {
        "event": "payment.captured",
        "event_id": f"evt_cap_{uuid.uuid4().hex[:6]}",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 450000,
                    "status": "captured"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db = SessionLocal()
    try:
        updated_attempt = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.payment_link_id == plink_id).first()
        assert updated_attempt is not None
        assert updated_attempt.status == "recovered"
        assert updated_attempt.recovered_at is not None

        updated_audit = db.query(AuditLogModel).filter(AuditLogModel.transaction_id == payment_id).first()
        assert updated_audit.recovered is True
        assert updated_audit.execution_status == "recovered"
        assert updated_audit.recovered_amount == 4500.0
    finally:
        db.close()

# 15. order.paid changes pending -> recovered
def test_15_order_paid_changes_pending_to_recovered(client):
    order_id = f"order_p6_{uuid.uuid4().hex[:6]}"
    txn_id = f"pay_order_{uuid.uuid4().hex[:6]}"

    db: Session = SessionLocal()
    try:
        txn = TransactionModel(
            transaction_id=txn_id,
            payment_id=txn_id,
            order_id=order_id,
            amount=5000.0,
            currency="INR",
            lifecycle_status="recovery_pending"
        )
        attempt = RecoveryAttemptModel(
            id=f"att_ord_{order_id}",
            transaction_id=txn_id,
            payment_link_id=f"plink_ord_{order_id}",
            status="pending",
            amount=5000.0
        )
        audit = AuditLogModel(
            audit_id=f"audit_ord_{order_id}",
            batch_run_id="batch_test_ord",
            transaction_id=txn_id,
            amount=5000.0,
            execution_status="recovery_pending",
            recovered=False,
            recovered_amount=0.0
        )
        db.add_all([txn, attempt, audit])
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
                    "amount": 500000,
                    "status": "paid"
                }
            }
        }
    }
    response = make_signed_request(client, payload)
    assert response.status_code == 200

    db = SessionLocal()
    try:
        att = db.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.transaction_id == txn_id).first()
        assert att.status == "recovered"
    finally:
        db.close()

# 16. Failed payment without customer contact does NOT invent data
def test_16_missing_customer_contact_does_not_invent_data(adapter):
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "plink_no_cust", "short_url": "https://rzp.io/i/nocust"}
        mock_post.return_value = mock_resp

        adapter.create_payment_link(
            amount_inr=1999.0,
            transaction_id=f"txn_no_contact_{uuid.uuid4().hex[:6]}",
            customer_name=None,
            customer_email=None,
            customer_contact=None
        )
        sent_payload = mock_post.call_args[1]["json"]
        assert "customer" not in sent_payload

# 17. Secrets never appear in logs
def test_17_secrets_never_appear_in_logs(adapter, caplog):
    with caplog.at_level(logging.DEBUG):
        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_resp.json.return_value = {"error": {"description": "Unauthorized"}}
            mock_post.return_value = mock_resp

            adapter.create_payment_link(
                amount_inr=100.0,
                transaction_id=f"txn_sec_leak_{uuid.uuid4().hex[:6]}"
            )

        log_text = caplog.text
        assert TEST_KEY_SECRET not in log_text

# 18. Existing webhook health check continues passing
def test_18_existing_webhook_endpoints_work(client):
    res = client.get("/webhooks/razorpay/health")
    assert res.status_code == 200
    assert "Webhook Ingestion" in res.json()["service"]

# 19. Existing agent recovery works
def test_19_existing_agent_recovery_works(sample_txn):
    from app.agents.recovery_agent import recovery_agent
    result = recovery_agent.recover(sample_txn)
    assert result.transaction_id == sample_txn.transaction_id
    assert result.iterations_used >= 1
    assert result.final_status in ["recovered", "recovery_pending", "escalated", "abandoned"]

# 20. Existing dashboard data loading returns 5 tables including df_attempts
def test_20_dashboard_loads_all_data():
    df_audit, df_runs, df_traces, df_webhooks, df_attempts = load_db_data()
    assert df_attempts is not None
