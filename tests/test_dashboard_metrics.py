"""
Tests for Step 6 Dashboard Live Recovery Rate Metrics:
Verifies live calculation of category-level recovery metrics from SQLite
(RecoveryAttemptModel, AuditLogModel, RecoveryScheduleModel).
Guarantees real, un-stubbed mathematical aggregations without hardcoded numbers.
"""

import uuid
from datetime import datetime, timezone
import pytest
from sqlalchemy.orm import Session

from app.db.database import SessionLocal, init_db
from app.db.models import RecoveryAttemptModel, AuditLogModel, RecoveryScheduleModel
from app.reporting.metrics import (
    compute_recovery_rate_by_failure_category,
    compute_audit_log_recovery_by_root_cause,
    compute_escalation_guardrail_metrics,
    load_recovery_schedules
)
from app.reporting.dashboard import load_db_data
from app.agents.failure_classifier import FailureReason


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


def test_compute_recovery_rate_empty_db():
    """When no attempts exist, all categories report 0.0% without ZeroDivisionError."""
    db: Session = SessionLocal()
    try:
        # Clean any existing test attempts
        db.query(RecoveryAttemptModel).delete()
        db.commit()

        df = compute_recovery_rate_by_failure_category(db=db)
        assert df is not None
        assert not df.empty
        # All failure reasons should be present
        for reason in FailureReason:
            match = df[df["failure_reason"] == reason.value]
            assert len(match) == 1
            row = match.iloc[0]
            assert row["total_attempts"] == 0
            assert row["successful_recoveries"] == 0
            assert row["recovery_rate_pct"] == 0.0
            assert row["total_amount_at_risk"] == 0.0
            assert row["total_amount_recovered"] == 0.0
    finally:
        db.close()


def test_compute_recovery_rate_live_calculation():
    """Real SQLite grouping query calculates exact counts, amounts, and percentages."""
    db: Session = SessionLocal()
    try:
        db.query(RecoveryAttemptModel).delete()
        db.commit()

        # Seed 3 OTP attempts: 2 recovered, 1 pending
        # Total amount: 3000.0, Recovered: 2000.0 -> Recovery rate: 66.7%
        att1 = RecoveryAttemptModel(
            id=f"att_{uuid.uuid4().hex[:8]}",
            transaction_id="txn_otp_1",
            payment_link_id="plink_otp_1",
            status="recovered",
            amount=1000.0,
            failure_reason=FailureReason.OTP_FAILURE.value,
            recovered_at=datetime.now(timezone.utc)
        )
        att2 = RecoveryAttemptModel(
            id=f"att_{uuid.uuid4().hex[:8]}",
            transaction_id="txn_otp_2",
            payment_link_id="plink_otp_2",
            status="recovered",
            amount=1000.0,
            failure_reason=FailureReason.OTP_FAILURE.value,
            recovered_at=datetime.now(timezone.utc)
        )
        att3 = RecoveryAttemptModel(
            id=f"att_{uuid.uuid4().hex[:8]}",
            transaction_id="txn_otp_3",
            payment_link_id="plink_otp_3",
            status="pending",
            amount=1000.0,
            failure_reason=FailureReason.OTP_FAILURE.value
        )

        # Seed 1 Card Expired: 1 recovered -> 100.0%
        att4 = RecoveryAttemptModel(
            id=f"att_{uuid.uuid4().hex[:8]}",
            transaction_id="txn_exp_1",
            payment_link_id="plink_exp_1",
            status="recovered",
            amount=2500.0,
            failure_reason=FailureReason.CARD_EXPIRED.value,
            recovered_at=datetime.now(timezone.utc)
        )

        # Seed 2 Bank Server Down: 0 recovered, 2 pending -> 0.0%
        att5 = RecoveryAttemptModel(
            id=f"att_{uuid.uuid4().hex[:8]}",
            transaction_id="txn_bank_1",
            payment_link_id="plink_bank_1",
            status="pending",
            amount=4000.0,
            failure_reason=FailureReason.BANK_SERVER_DOWN.value
        )
        att6 = RecoveryAttemptModel(
            id=f"att_{uuid.uuid4().hex[:8]}",
            transaction_id="txn_bank_2",
            payment_link_id="plink_bank_2",
            status="pending",
            amount=4000.0,
            failure_reason=FailureReason.BANK_SERVER_DOWN.value
        )

        db.add_all([att1, att2, att3, att4, att5, att6])
        db.commit()

        # Run live metrics computation
        df = compute_recovery_rate_by_failure_category(db=db)

        # 1. Assert OTP Failure row
        otp_row = df[df["failure_reason"] == FailureReason.OTP_FAILURE.value].iloc[0]
        assert otp_row["total_attempts"] == 3
        assert otp_row["successful_recoveries"] == 2
        assert otp_row["pending_recoveries"] == 1
        assert otp_row["total_amount_at_risk"] == 3000.0
        assert otp_row["total_amount_recovered"] == 2000.0
        assert otp_row["recovery_rate_pct"] == 66.7
        assert otp_row["revenue_recovery_rate_pct"] == 66.7

        # 2. Assert Card Expired row
        exp_row = df[df["failure_reason"] == FailureReason.CARD_EXPIRED.value].iloc[0]
        assert exp_row["total_attempts"] == 1
        assert exp_row["successful_recoveries"] == 1
        assert exp_row["total_amount_at_risk"] == 2500.0
        assert exp_row["total_amount_recovered"] == 2500.0
        assert exp_row["recovery_rate_pct"] == 100.0

        # 3. Assert Bank Server Down row
        bank_row = df[df["failure_reason"] == FailureReason.BANK_SERVER_DOWN.value].iloc[0]
        assert bank_row["total_attempts"] == 2
        assert bank_row["successful_recoveries"] == 0
        assert bank_row["pending_recoveries"] == 2
        assert bank_row["total_amount_at_risk"] == 8000.0
        assert bank_row["total_amount_recovered"] == 0.0
        assert bank_row["recovery_rate_pct"] == 0.0

        # 4. Assert Risk Blocked guardrail row (0 attempts)
        risk_row = df[df["failure_reason"] == FailureReason.RISK_BLOCKED.value].iloc[0]
        assert risk_row["total_attempts"] == 0
        assert risk_row["successful_recoveries"] == 0
        assert risk_row["recovery_rate_pct"] == 0.0
        assert "Quarantined" in risk_row["guardrail_status"]
    finally:
        db.close()


def test_load_db_data_contains_intelligence_columns():
    """Verify load_db_data returns df_attempts with failure_reason and preferred_methods."""
    db: Session = SessionLocal()
    try:
        att = RecoveryAttemptModel(
            id=f"att_intel_{uuid.uuid4().hex[:8]}",
            transaction_id="txn_intel_check",
            payment_link_id="plink_intel_check",
            status="pending",
            amount=1999.0,
            failure_reason="otp_failure",
            preferred_methods="upi,card"
        )
        db.merge(att)
        db.commit()

        load_db_data.clear()
        df_audit, df_runs, df_traces, df_webhooks, df_attempts = load_db_data()
        assert df_attempts is not None
        assert "failure_reason" in df_attempts.columns
        assert "preferred_methods" in df_attempts.columns

        matched = df_attempts[df_attempts["transaction_id"] == "txn_intel_check"]
        assert len(matched) >= 1
        row = matched.iloc[0]
        assert row["failure_reason"] == "otp_failure"
        assert row["preferred_methods"] == "upi,card"
    finally:
        db.close()


def test_load_recovery_schedules_returns_durably_stored_jobs():
    """Verify load_recovery_schedules fetches pending and active retry jobs."""
    db: Session = SessionLocal()
    try:
        sched = RecoveryScheduleModel(
            id=f"sched_test_{uuid.uuid4().hex[:8]}",
            transaction_id="txn_sched_monitor",
            failure_reason="bank_server_down",
            execute_at=datetime.now(timezone.utc),
            status="pending",
            action_payload={"amount": 4999.0, "suggested_methods": ["upi", "netbanking"]}
        )
        db.merge(sched)
        db.commit()

        df_scheds = load_recovery_schedules(db=db)
        assert df_scheds is not None
        assert not df_scheds.empty
        matched = df_scheds[df_scheds["transaction_id"] == "txn_sched_monitor"]
        assert len(matched) >= 1
        row = matched.iloc[0]
        assert row["failure_reason"] == "bank_server_down"
        assert row["status"] == "pending"
    finally:
        db.close()


def test_compute_escalation_guardrail_metrics_with_seeded_data():
    """Verify compute_escalation_guardrail_metrics queries AuditLogModel for real quarantined counts & amounts."""
    db: Session = SessionLocal()
    try:
        test_batch_id = f"test_run_esc_{uuid.uuid4().hex[:8]}"

        # Seed 2 RISK_BLOCKED audit records: each INR 18,500
        audit_risk1 = AuditLogModel(
            audit_id=f"audit_risk1_{uuid.uuid4().hex[:8]}",
            batch_run_id=test_batch_id,
            transaction_id=f"txn_risk_01_{uuid.uuid4().hex[:6]}",
            amount=18500.0,
            root_cause="risk_blocked",
            recommended_action="human_escalation",
            execution_status="escalated",
            recovered=False,
            recovered_amount=0.0
        )
        audit_risk2 = AuditLogModel(
            audit_id=f"audit_risk2_{uuid.uuid4().hex[:8]}",
            batch_run_id=test_batch_id,
            transaction_id=f"txn_risk_02_{uuid.uuid4().hex[:6]}",
            amount=18500.0,
            root_cause="risk_blocked",
            recommended_action="human_escalation",
            execution_status="escalated",
            recovered=False,
            recovered_amount=0.0
        )

        # Seed 1 UNKNOWN audit record: INR 8,999
        audit_unk = AuditLogModel(
            audit_id=f"audit_unk_{uuid.uuid4().hex[:8]}",
            batch_run_id=test_batch_id,
            transaction_id=f"txn_unk_01_{uuid.uuid4().hex[:6]}",
            amount=8999.0,
            root_cause="unknown",
            recommended_action="human_escalation",
            execution_status="escalated",
            recovered=False,
            recovered_amount=0.0
        )

        # Seed 1 non-escalated audit record (OTP Failure, payment link): INR 2,499
        audit_otp = AuditLogModel(
            audit_id=f"audit_otp_{uuid.uuid4().hex[:8]}",
            batch_run_id=test_batch_id,
            transaction_id=f"txn_otp_01_{uuid.uuid4().hex[:6]}",
            amount=2499.0,
            root_cause="wrong_otp",
            recommended_action="payment_link",
            execution_status="recovery_pending",
            recovered=False,
            recovered_amount=0.0
        )

        db.add_all([audit_risk1, audit_risk2, audit_unk, audit_otp])
        db.commit()

        # Run query scoped to seeded batch
        metrics = compute_escalation_guardrail_metrics(db=db, batch_run_id=test_batch_id)

        # Confirm exact non-zero counts
        assert metrics["total_escalated_count"] == 3
        assert metrics["total_amount_quarantined"] == 45999.0  # 18500 + 18500 + 8999

        # Confirm breakdown by cause
        assert "risk_blocked" in metrics["breakdown_by_cause"]
        assert metrics["breakdown_by_cause"]["risk_blocked"]["count"] == 2
        assert metrics["breakdown_by_cause"]["risk_blocked"]["amount"] == 37000.0

        assert "unknown" in metrics["breakdown_by_cause"]
        assert metrics["breakdown_by_cause"]["unknown"]["count"] == 1
        assert metrics["breakdown_by_cause"]["unknown"]["amount"] == 8999.0

        # Confirm wrong_otp was NOT counted as an escalation / quarantine
        assert "wrong_otp" not in metrics["breakdown_by_cause"]

        # Confirm DataFrame format
        df = metrics["df"]
        assert not df.empty
        assert "root_cause" in df.columns
        assert "escalated_count" in df.columns
        assert "amount_quarantined" in df.columns
    finally:
        db.close()
