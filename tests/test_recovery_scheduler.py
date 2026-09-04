import uuid
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from sqlalchemy.orm import Session

from app.db.database import SessionLocal, init_db
from app.db.models import RecoveryScheduleModel, RecoveryAttemptModel
from app.db.recovery_state import RecoveryStatus
from app.pipeline.scheduler import RecoveryScheduler


@pytest.fixture(autouse=True)
def setup_test_env():
    init_db()


@pytest.fixture
def scheduler():
    return RecoveryScheduler()


def test_schedule_delayed_recovery_persists_in_db(scheduler):
    txn_id = f"txn_sched_{uuid.uuid4().hex[:6]}"
    db: Session = SessionLocal()

    schedule = scheduler.schedule_delayed_recovery(
        transaction_id=txn_id,
        failure_reason="bank_server_down",
        delay_seconds=900,
        action_payload={"amount": 2500.0, "description": "Bank downtime recovery"},
        db=db
    )

    assert schedule is not None
    assert schedule.transaction_id == txn_id
    assert schedule.status == "pending"
    assert schedule.failure_reason == "bank_server_down"

    # Verify queryable from DB
    persisted = db.query(RecoveryScheduleModel).filter(
        RecoveryScheduleModel.id == schedule.id
    ).first()
    assert persisted is not None
    assert persisted.action_payload["amount"] == 2500.0
    db.close()


def test_duplicate_scheduling_guard(scheduler):
    txn_id = f"txn_dup_sched_{uuid.uuid4().hex[:6]}"
    db: Session = SessionLocal()

    # First schedule
    sched1 = scheduler.schedule_delayed_recovery(
        transaction_id=txn_id,
        failure_reason="bank_server_down",
        delay_seconds=900,
        action_payload={"amount": 1000.0},
        db=db
    )

    # Second schedule attempt for SAME txn_id
    sched2 = scheduler.schedule_delayed_recovery(
        transaction_id=txn_id,
        failure_reason="bank_server_down",
        delay_seconds=900,
        action_payload={"amount": 1000.0},
        db=db
    )

    assert sched1.id == sched2.id

    # Verify only ONE record in DB
    count = db.query(RecoveryScheduleModel).filter(
        RecoveryScheduleModel.transaction_id == txn_id
    ).count()
    assert count == 1
    db.close()


def test_execute_due_schedule_creates_payment_link(scheduler):
    txn_id = f"txn_exec_sched_{uuid.uuid4().hex[:6]}"
    db: Session = SessionLocal()

    sched = scheduler.schedule_delayed_recovery(
        transaction_id=txn_id,
        failure_reason="bank_server_down",
        delay_seconds=0,
        action_payload={
            "amount": 1800.0,
            "description": "Custom recovery link",
            "preferred_methods": ["upi"]
        },
        db=db
    )

    with patch("app.integrations.razorpay.payment_links.razorpay_payment_links.create_payment_link") as mock_create:
        mock_create.return_value = {
            "success": True,
            "payment_link_id": "plink_sched_test_123",
            "payment_link_url": "https://rzp.io/i/sched123"
        }

        res = scheduler.execute_due_schedule(sched.id, db=db)
        assert res["success"] is True
        assert res["status"] == "completed"

        # Verify schedule updated in DB
        db.refresh(sched)
        assert sched.status == "completed"
        assert sched.attempts == 1
        assert mock_create.called
    db.close()


def test_execute_due_schedule_skips_if_already_recovered(scheduler):
    txn_id = f"txn_already_rec_{uuid.uuid4().hex[:6]}"
    db: Session = SessionLocal()

    # Pre-seed RecoveryAttempt as recovered
    att = RecoveryAttemptModel(
        id=f"rec_att_{uuid.uuid4().hex[:8]}",
        transaction_id=txn_id,
        status=RecoveryStatus.RECOVERED.value,
        amount=1500.0,
        currency="INR"
    )
    db.add(att)
    db.commit()

    # Create schedule
    sched = scheduler.schedule_delayed_recovery(
        transaction_id=txn_id,
        failure_reason="bank_server_down",
        delay_seconds=0,
        action_payload={"amount": 1500.0},
        db=db
    )

    with patch("app.integrations.razorpay.payment_links.razorpay_payment_links.create_payment_link") as mock_create:
        res = scheduler.execute_due_schedule(sched.id, db=db)
        assert res["success"] is True
        assert res["status"] == "cancelled"
        assert res["reason"] == "already_recovered"
        assert not mock_create.called

        db.refresh(sched)
        assert sched.status == "cancelled"
    db.close()


def test_cancel_schedule(scheduler):
    txn_id = f"txn_cancel_{uuid.uuid4().hex[:6]}"
    db: Session = SessionLocal()

    scheduler.schedule_delayed_recovery(
        transaction_id=txn_id,
        failure_reason="insufficient_funds",
        delay_seconds=3600,
        action_payload={"amount": 2000.0},
        db=db
    )

    cancelled_count = scheduler.cancel_schedule(txn_id, db=db)
    assert cancelled_count == 1

    cancelled_job = db.query(RecoveryScheduleModel).filter(
        RecoveryScheduleModel.transaction_id == txn_id
    ).first()
    assert cancelled_job.status == "cancelled"
    db.close()
