import uuid
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from sqlalchemy.orm import Session

from app.db.database import SessionLocal, init_db
from app.db.models import RecoveryAttemptModel
from app.integrations.razorpay.client import RazorpayClient
from app.integrations.razorpay.payment_links import RazorpayPaymentLinkAdapter
from app.agents.tools.payment_link_tool import PaymentLinkTool
from app.schemas.transaction import Transaction, PaymentMethod
from app.schemas.diagnosis import DiagnosisResult, RootCause, DiagnosisSource


TEST_KEY_ID = "rzp_test_secSmartKey123"
TEST_KEY_SECRET = "secSecretKeySafe999"


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    init_db()


@pytest.fixture
def adapter():
    test_client = RazorpayClient(key_id=TEST_KEY_ID, key_secret=TEST_KEY_SECRET)
    return RazorpayPaymentLinkAdapter(client=test_client)


def test_smart_link_sets_failure_reason_and_preferred_methods(adapter):
    txn_id = f"txn_smart_{uuid.uuid4().hex[:6]}"
    custom_desc = "Card verification timed out. Complete in 1-tap via UPI."
    db: Session = SessionLocal()

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": f"plink_smart_{uuid.uuid4().hex[:6]}",
            "short_url": "https://rzp.io/i/smart01",
            "status": "created"
        }
        mock_post.return_value = mock_resp

        res = adapter.create_payment_link(
            amount_inr=1250.0,
            transaction_id=txn_id,
            description=custom_desc,
            failure_reason="otp_failure",
            preferred_methods=["upi", "card"],
            use_upi_intent=True,
            db=db
        )

        assert res["success"] is True
        assert mock_post.called

        # Verify payload sent to Razorpay
        call_kwargs = mock_post.call_args[1]
        sent_payload = call_kwargs["json"]
        assert sent_payload["description"] == custom_desc
        assert sent_payload["upi_link"] is True
        assert sent_payload["notes"]["failure_reason"] == "otp_failure"
        assert sent_payload["notes"]["preferred_methods"] == "upi,card"

        # Verify database persistence on RecoveryAttemptModel
        attempt = db.query(RecoveryAttemptModel).filter(
            RecoveryAttemptModel.transaction_id == txn_id
        ).first()

        assert attempt is not None
        assert attempt.failure_reason == "otp_failure"
        assert attempt.preferred_methods == "upi,card"
        assert attempt.status == "pending"
        assert attempt.amount == 1250.0
    db.close()


def test_payment_link_tool_forwards_diagnosis_and_custom_options(adapter):
    tool = PaymentLinkTool(adapter=adapter)
    txn_id = f"txn_tool_smart_{uuid.uuid4().hex[:6]}"
    txn = Transaction(
        transaction_id=txn_id,
        amount=3499.0,
        currency="INR",
        payment_method=PaymentMethod.CARD,
        error_code="3DS_VERIFICATION_FAILED",
        error_description="OTP validation expired",
        error_source="issuer",
        timestamp=datetime.now(timezone.utc)
    )
    diag = DiagnosisResult(
        transaction_id=txn_id,
        root_cause=RootCause.WRONG_OTP,
        confidence=0.98,
        reasoning="Customer did not submit OTP in time",
        source=DiagnosisSource.RULES
    )

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": f"plink_tool_{uuid.uuid4().hex[:6]}",
            "short_url": "https://rzp.io/i/tool02",
            "status": "created"
        }
        mock_post.return_value = mock_resp

        result = tool.execute(
            txn=txn,
            diagnosis=diag,
            preferred_methods=["upi"],
            use_upi_intent=True,
            description="1-tap UPI recovery"
        )

        assert result.success is True
        assert result.status == "recovery_pending"
        assert result.recovered_amount == 0.0
        assert result.result_data["failure_reason"] == "wrong_otp"
        assert result.result_data["preferred_methods"] == ["upi"]
        assert result.result_data["use_upi_intent"] is True
