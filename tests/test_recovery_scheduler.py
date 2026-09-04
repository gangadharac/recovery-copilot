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


def test_rehydrate_and_run_executes_past_overdue_job(scheduler):
    """
    Verifies that jobs scheduled in the past execute immediately upon server restart
    rather than waiting for a fresh sleep timer or being lost.
    """
    txn_id = f"txn_overdue_{uuid.uuid4().hex[:6]}"
    sched_id = f"sched_past_{uuid.uuid4().hex[:6]}"
    past_due = datetime.now(timezone.utc) - timedelta(minutes=15)
    db: Session = SessionLocal()

    # Pre-seed a schedule directly in the database with a past execute_at timestamp
    past_schedule = RecoveryScheduleModel(
        id=sched_id,
        transaction_id=txn_id,
        failure_reason="bank_server_down",
        execute_at=past_due,
        status="pending",
        action_payload={"amount": 2999.0, "description": "Overdue recovery test"},
        attempts=0,
        created_at=past_due - timedelta(minutes=15)
    )
    db.add(past_schedule)
    db.commit()

    with patch("app.integrations.razorpay.payment_links.razorpay_payment_links.create_payment_link") as mock_create:
        mock_create.return_value = {
            "success": True,
            "payment_link_id": f"plink_overdue_{uuid.uuid4().hex[:6]}",
            "payment_link_url": "https://rzp.io/i/overdue"
        }

        # Simulate server reboot rehydration
        overdue_count = scheduler.rehydrate_and_run(db=db)
        assert overdue_count >= 1

        db.refresh(past_schedule)
        assert past_schedule.status == "completed"
        assert past_schedule.attempts >= 1
        assert mock_create.called
    db.close()


def test_execute_due_schedule_handles_exception_safely(scheduler):
    """
    Verifies that if the Razorpay API or payment link creation raises an unhandled exception,
    the schedule status is set to 'failed', error details are persisted, and the exception
    does not propagate or crash background tasks.
    """
    txn_id = f"txn_fail_exc_{uuid.uuid4().hex[:6]}"
    db: Session = SessionLocal()

    sched = scheduler.schedule_delayed_recovery(
        transaction_id=txn_id,
        failure_reason="network_glitch",
        delay_seconds=0,
        action_payload={"amount": 999.0},
        db=db
    )

    with patch("app.integrations.razorpay.payment_links.razorpay_payment_links.create_payment_link") as mock_create:
        mock_create.side_effect = RuntimeError("Fatal connection timeout to api.razorpay.com:443")

        res = scheduler.execute_due_schedule(sched.id, db=db)
        assert res["success"] is False
        assert res["status"] == "failed"
        assert "Fatal connection timeout" in res["error_message"]

        db.refresh(sched)
        assert sched.status == "failed"
        assert "Fatal connection timeout" in sched.error_message
    db.close()

