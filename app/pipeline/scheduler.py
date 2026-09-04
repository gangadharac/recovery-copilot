"""
Persistent Recovery Scheduler:
Manages delayed and scheduled recovery jobs (e.g. bank switch cooldowns,
insufficient funds reminders) backed by SQLite storage so that jobs survive
server restarts, prevent duplicate timers, and verify transaction state before firing.
"""

import uuid
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import RecoveryScheduleModel, RecoveryAttemptModel
from app.db.recovery_state import RecoveryStatus
from app.integrations.razorpay.payment_links import razorpay_payment_links

logger = logging.getLogger("app.recovery.scheduler")


class RecoveryScheduler:
    """
    Persistent recovery scheduler enforcing:
    1. Database-backed schedule storage for survivability across restarts.
    2. Duplicate schedule guard on active transaction_id jobs.
    3. Pre-execution verification: skips recovery if already recovered or cancelled.
    """

    def schedule_delayed_recovery(
        self,
        transaction_id: str,
        failure_reason: str,
        delay_seconds: int,
        action_payload: Dict[str, Any],
        db: Optional[Session] = None
    ) -> RecoveryScheduleModel:
        """
        Schedules a delayed recovery job.
        Checks for existing active jobs on the transaction to prevent duplicates.
        """
        session_provided = db is not None
        session = db if session_provided else SessionLocal()

        try:
            # 1. Duplicate Scheduling Guard (Active Job Check)
            existing_active = session.query(RecoveryScheduleModel).filter(
                RecoveryScheduleModel.transaction_id == transaction_id,
                RecoveryScheduleModel.status.in_(["pending", "executing"])
            ).first()

            if existing_active:
                logger.info(
                    f"[SCHEDULER] Active schedule {existing_active.id} already exists for "
                    f"txn {transaction_id} (status: {existing_active.status}). Skipping duplicate scheduling."
                )
                return existing_active

            # 2. Compute Execution Due Timestamp (UTC)
            now_utc = datetime.now(timezone.utc)
            due_utc = now_utc + timedelta(seconds=max(delay_seconds, 0))

            schedule_id = f"sched_{uuid.uuid4().hex[:12]}"
            schedule = RecoveryScheduleModel(
                id=schedule_id,
                transaction_id=transaction_id,
                failure_reason=failure_reason,
                execute_at=due_utc,
                status="pending",
                action_payload=action_payload,
                attempts=0,
                created_at=now_utc,
                updated_at=now_utc
            )
            session.add(schedule)
            session.commit()
            session.refresh(schedule)

            logger.info(
                f"[SCHEDULER] Queued delayed recovery job {schedule_id} for txn {transaction_id} "
                f"due at {due_utc.isoformat()} ({delay_seconds}s delay)."
            )

            # 3. If running inside an active asyncio loop, spawn asynchronous timer
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._wait_and_execute(schedule_id, delay_seconds))
            except RuntimeError:
                # Running outside an active asyncio loop (e.g. synchronous worker/script)
                pass

            return schedule
        finally:
            if not session_provided:
                session.close()

    async def _wait_and_execute(self, schedule_id: str, delay_seconds: int) -> Dict[str, Any]:
        """Asynchronously waits for the delay duration and executes the due schedule."""
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        return self.execute_due_schedule(schedule_id)

    def execute_due_schedule(self, schedule_id: str, db: Optional[Session] = None) -> Dict[str, Any]:
        """
        Executes a scheduled recovery job.
        Guarantees that:
        1. Job status is checked and locked.
        2. Transaction is checked to verify it wasn't already recovered in the interim.
        3. Creates the tailored recovery payment link and marks job completed.
        """
        session_provided = db is not None
        session = db if session_provided else SessionLocal()

        try:
            schedule = session.query(RecoveryScheduleModel).filter(
                RecoveryScheduleModel.id == schedule_id
            ).first()

            if not schedule:
                logger.error(f"[SCHEDULER] Schedule {schedule_id} not found.")
                return {"success": False, "status": "not_found"}

            if schedule.status not in ["pending", "executing"]:
                logger.info(f"[SCHEDULER] Schedule {schedule_id} status is '{schedule.status}'. Skipping execution.")
                return {"success": False, "status": schedule.status}

            # 1. State Verification: Was transaction already recovered while waiting?
            already_recovered = session.query(RecoveryAttemptModel).filter(
                RecoveryAttemptModel.transaction_id == schedule.transaction_id,
                RecoveryAttemptModel.status.in_([
                    RecoveryStatus.RECOVERED.value,
                    RecoveryStatus.PAID_VERIFICATION_PENDING.value
                ])
            ).first()

            if already_recovered:
                logger.info(
                    f"[SCHEDULER] Transaction {schedule.transaction_id} is already recovered "
                    f"(attempt {already_recovered.id}). Cancelling schedule {schedule_id}."
                )
                schedule.status = "cancelled"
                schedule.updated_at = datetime.now(timezone.utc)
                session.commit()
                return {
                    "success": True,
                    "status": "cancelled",
                    "reason": "already_recovered",
                    "transaction_id": schedule.transaction_id
                }

            # 2. Mark schedule as executing
            schedule.status = "executing"
            schedule.attempts += 1
            schedule.updated_at = datetime.now(timezone.utc)
            session.commit()

            # 3. Execute recovery via Payment Link Adapter
            payload = schedule.action_payload or {}
            amount_inr = payload.get("amount") or 0.0
            description = payload.get("description") or f"Delayed Recovery for {schedule.transaction_id}"
            preferred_methods = payload.get("preferred_methods") or []
            use_upi_intent = payload.get("use_upi_intent", False)

            res = razorpay_payment_links.create_payment_link(
                amount_inr=amount_inr,
                transaction_id=schedule.transaction_id,
                currency=payload.get("currency", "INR"),
                customer_name=payload.get("customer_name"),
                customer_email=payload.get("customer_email"),
                customer_contact=payload.get("customer_contact"),
                description=description,
                failure_reason=schedule.failure_reason,
                preferred_methods=preferred_methods,
                use_upi_intent=use_upi_intent,
                db=session
            )

            # 4. Mark completed or failed
            if res.get("success"):
                schedule.status = "completed"
                schedule.updated_at = datetime.now(timezone.utc)
                session.commit()
                logger.info(
                    f"[SCHEDULER] Schedule {schedule_id} executed successfully for {schedule.transaction_id}. "
                    f"Link: {res.get('payment_link_id')}."
                )
                return {
                    "success": True,
                    "status": "completed",
                    "payment_link_id": res.get("payment_link_id"),
                    "payment_link_url": res.get("payment_link_url"),
                    "transaction_id": schedule.transaction_id
                }
            else:
                schedule.status = "failed"
                schedule.error_message = res.get("error_message")
                schedule.updated_at = datetime.now(timezone.utc)
                session.commit()
                logger.warning(
                    f"[SCHEDULER] Schedule {schedule_id} failed for {schedule.transaction_id}: {res.get('error_message')}"
                )
                return {
                    "success": False,
                    "status": "failed",
                    "error_message": res.get("error_message"),
                    "transaction_id": schedule.transaction_id
                }
        finally:
            if not session_provided:
                session.close()

    def cancel_schedule(self, transaction_id: str, db: Optional[Session] = None) -> int:
        """
        Cancels any pending schedules for a transaction (e.g. when transaction gets paid).
        Returns the number of cancelled schedules.
        """
        session_provided = db is not None
        session = db if session_provided else SessionLocal()

        try:
            pending_jobs = session.query(RecoveryScheduleModel).filter(
                RecoveryScheduleModel.transaction_id == transaction_id,
                RecoveryScheduleModel.status == "pending"
            ).all()

            for job in pending_jobs:
                job.status = "cancelled"
                job.updated_at = datetime.now(timezone.utc)

            session.commit()
            return len(pending_jobs)
        finally:
            if not session_provided:
                session.close()

    def rehydrate_and_run(self, db: Optional[Session] = None) -> int:
        """
        Server startup hook: Queries all pending jobs from database.
        Executes any jobs that became overdue while server was down, and schedules future ones.
        Returns the number of processed overdue jobs.
        """
        session_provided = db is not None
        session = db if session_provided else SessionLocal()

        try:
            now_utc = datetime.now(timezone.utc)
            # Fetch all pending jobs
            pending_jobs = session.query(RecoveryScheduleModel).filter(
                RecoveryScheduleModel.status == "pending"
            ).all()

            overdue_count = 0
            for job in pending_jobs:
                exec_at = job.execute_at
                if exec_at.tzinfo is None:
                    exec_at = exec_at.replace(tzinfo=timezone.utc)

                if exec_at <= now_utc:
                    logger.info(f"[SCHEDULER_STARTUP] Job {job.id} for txn {job.transaction_id} is overdue. Executing now.")
                    self.execute_due_schedule(job.id, db=session)
                    overdue_count += 1
                else:
                    remaining_seconds = int((exec_at - now_utc).total_seconds())
                    logger.info(
                        f"[SCHEDULER_STARTUP] Rescheduling future job {job.id} for txn {job.transaction_id} "
                        f"in {remaining_seconds}s."
                    )
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self._wait_and_execute(job.id, remaining_seconds))
                    except RuntimeError:
                        pass

            return overdue_count
        finally:
            if not session_provided:
                session.close()


# Global recovery scheduler singleton
recovery_scheduler = RecoveryScheduler()
