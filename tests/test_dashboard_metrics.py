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
