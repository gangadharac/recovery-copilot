import uuid
import pytest
import asyncio
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
    db = SessionLocal()
    db.query(RecoveryScheduleModel).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(RecoveryScheduleModel).delete()
    db.commit()
    db.close()


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


def test_calculate_remaining_delay_helper(scheduler):
    """
    Verifies that calculate_remaining_delay computes exact seconds remaining
    for future timestamps and clamps past timestamps to 0.
    """
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)

    # 10 minutes in the future
    future_at = now + timedelta(minutes=10)
    delay = scheduler.calculate_remaining_delay(future_at, now_utc=now)
    assert delay == 600

    # 5 minutes in the past
    past_at = now - timedelta(minutes=5)
    delay_past = scheduler.calculate_remaining_delay(past_at, now_utc=now)
    assert delay_past == 0


@pytest.mark.asyncio
async def test_rehydrate_and_run_rearms_future_schedule_in_active_event_loop(scheduler):
    """
    Verifies that a schedule whose execute_at is in the FUTURE at server restart:
    1. Runs inside an active asyncio event loop.
    2. Successfully re-arms a background timer (future_rearmed == 1, rearm_failed == 0).
    3. Is NOT executed immediately (mock link creation not called, remains 'pending').
    4. Computes correct remaining delay (~600s).
    5. Confirms active background task is registered in scheduler.
    """
    txn_id = f"txn_future_{uuid.uuid4().hex[:6]}"
    sched_id = f"sched_future_{uuid.uuid4().hex[:6]}"
    # Set to 10 minutes in the future (remaining delay ~600s)
    future_due = datetime.now(timezone.utc) + timedelta(minutes=10)
    db: Session = SessionLocal()

    future_schedule = RecoveryScheduleModel(
        id=sched_id,
        transaction_id=txn_id,
        failure_reason="bank_server_down",
        execute_at=future_due,
        status="pending",
        action_payload={"amount": 4500.0, "description": "Future cooldown recovery"},
        attempts=0,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    db.add(future_schedule)
    db.commit()

    with patch("app.integrations.razorpay.payment_links.razorpay_payment_links.create_payment_link") as mock_create:
        rehydrate_res = scheduler.rehydrate_and_run(db=db)

        # 1. Assert overdue count is 0, future_rearmed is 1, rearm_failed is 0
        assert rehydrate_res.overdue_executed == 0
        assert rehydrate_res.future_rearmed == 1
        assert rehydrate_res.rearm_failed == 0

        # 2. Assert no payment link was created prematurely
        assert not mock_create.called

        # 3. Assert DB record is still 'pending' with 0 attempts
        db.refresh(future_schedule)
        assert future_schedule.status == "pending"
        assert future_schedule.attempts == 0

        # 4. Assert remaining duration is computed correctly (~600s)
        remaining = scheduler.calculate_remaining_delay(future_schedule.execute_at)
        assert 570 <= remaining <= 600

        # 5. Assert active background task is tracked
        assert len(scheduler._background_tasks) >= 1
    db.close()


def test_rehydrate_and_run_marks_rearm_failed_when_no_event_loop(scheduler):
    """
    Verifies that calling rehydrate_and_run outside an active event loop:
    1. Does NOT increment future_rearmed (reports 0).
    2. Increments rearm_failed (reports 1).
    3. Sets schedule.status to 'rearm_failed' with error_message detailing no event loop.
    4. Does NOT execute the schedule or create payment link.
    """
    txn_id = f"txn_noloop_{uuid.uuid4().hex[:6]}"
    sched_id = f"sched_noloop_{uuid.uuid4().hex[:6]}"
    future_due = datetime.now(timezone.utc) + timedelta(minutes=10)
    db: Session = SessionLocal()

    future_schedule = RecoveryScheduleModel(
        id=sched_id,
        transaction_id=txn_id,
        failure_reason="bank_server_down",
        execute_at=future_due,
        status="pending",
        action_payload={"amount": 3500.0},
        attempts=0,
        created_at=datetime.now(timezone.utc)
    )
    db.add(future_schedule)
    db.commit()

    with patch("app.integrations.razorpay.payment_links.razorpay_payment_links.create_payment_link") as mock_create:
        rehydrate_res = scheduler.rehydrate_and_run(db=db)

        # 1. Assert future_rearmed is 0, rearm_failed is 1
        assert rehydrate_res.future_rearmed == 0
        assert rehydrate_res.rearm_failed == 1
        assert rehydrate_res.overdue_executed == 0

        # 2. Assert no payment link was created
        assert not mock_create.called

        # 3. Assert DB record transitions to 'rearm_failed' with descriptive error
        db.refresh(future_schedule)
        assert future_schedule.status == "rearm_failed"
        assert "no running event loop" in (future_schedule.error_message or "")
    db.close()


@pytest.mark.asyncio
async def test_rehydrate_and_run_timer_actually_fires_and_executes_due_schedule(scheduler):
    """
    Verifies the full round-trip of a re-armed timer:
    1. Seeds a schedule due with a short delay (1 second).
    2. Calls rehydrate_and_run() inside an active asyncio event loop.
    3. Confirms it is NOT executed immediately (status stays 'pending', link not called).
    4. Waits for the timer duration (await asyncio.sleep(1.2)).
    5. Confirms execute_due_schedule was actually fired by the timer, status transitions
       to 'completed', attempts incremented to 1, and payment link was created.
    """
    txn_id = f"txn_fire_{uuid.uuid4().hex[:6]}"
    sched_id = f"sched_fire_{uuid.uuid4().hex[:6]}"
    future_due = datetime.now(timezone.utc) + timedelta(seconds=1)
    db: Session = SessionLocal()

    future_schedule = RecoveryScheduleModel(
        id=sched_id,
        transaction_id=txn_id,
        failure_reason="network_glitch",
        execute_at=future_due,
        status="pending",
        action_payload={"amount": 1299.0, "description": "Short timer roundtrip test"},
        attempts=0,
        created_at=datetime.now(timezone.utc)
    )
    db.add(future_schedule)
    db.commit()

    with patch("app.integrations.razorpay.payment_links.razorpay_payment_links.create_payment_link") as mock_create:
        mock_create.return_value = {
            "success": True,
            "payment_link_id": f"plink_fire_{uuid.uuid4().hex[:6]}",
            "payment_link_url": "https://rzp.io/i/roundtrip_test"
        }

        # Step 1: Rehydrate inside active event loop
        rehydrate_res = scheduler.rehydrate_and_run(db=db)
        assert rehydrate_res.future_rearmed == 1
        assert rehydrate_res.rearm_failed == 0

        # Step 2: Confirm NOT executed immediately
        assert not mock_create.called
        db.refresh(future_schedule)
        assert future_schedule.status == "pending"
        assert future_schedule.attempts == 0

        # Step 3: Wait for timer duration to elapse
        await asyncio.sleep(1.2)

        # Step 4: Confirm timer fired and executed due schedule in background
        assert mock_create.called
        db.refresh(future_schedule)
        assert future_schedule.status == "completed"
        assert future_schedule.attempts == 1
    db.close()


