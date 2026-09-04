"""
Live Recovery Rate Analytics & Category Metrics Module:
Computes real-time recovery metrics from SQLite (RecoveryAttemptModel and AuditLogModel).
Guarantees real, un-stubbed metrics computed directly from database records.
"""

from typing import Optional, Dict, Any, List
import pandas as pd
from sqlalchemy import func, case, or_
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import RecoveryAttemptModel, AuditLogModel, RecoveryScheduleModel
from app.agents.failure_classifier import FailureReason


def compute_recovery_rate_by_failure_category(
    db: Optional[Session] = None,
    include_all_categories: bool = True
) -> pd.DataFrame:
    """
    Computes live recovery rates grouped by failure reason directly from RecoveryAttemptModel in SQLite.
    
    SQL / SQLAlchemy Logic:
    - Groups by RecoveryAttemptModel.failure_reason
    - Total Attempts: COUNT(id)
    - Successful Recoveries: COUNT(CASE WHEN recovered_at IS NOT NULL OR status = 'recovered' THEN 1 END)
    - Pending Recoveries: COUNT(CASE WHEN status IN ('pending', 'created', 'paid_verification_pending') THEN 1 END)
    - Failed Recoveries: COUNT(CASE WHEN status IN ('failed', 'verification_failed', 'expired') THEN 1 END)
    - Revenue at Risk: SUM(amount)
    - Revenue Recovered: SUM(CASE WHEN recovered_at IS NOT NULL OR status = 'recovered' THEN amount ELSE 0 END)
    - Recovery Rate (%): (successful / total * 100.0)
    - Revenue Recovery Rate (%): (amount_recovered / amount_at_risk * 100.0)
    """
    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        # Grouped query from RecoveryAttemptModel
        results = db.query(
            RecoveryAttemptModel.failure_reason,
            func.count(RecoveryAttemptModel.id).label("total_attempts"),
            func.sum(
                case(
                    (
                        or_(
                            RecoveryAttemptModel.recovered_at.isnot(None),
                            RecoveryAttemptModel.status == "recovered"
                        ),
                        1
                    ),
                    else_=0
                )
            ).label("successful_recoveries"),
            func.sum(
                case(
                    (
                        RecoveryAttemptModel.status.in_(["pending", "created", "paid_verification_pending"]),
                        1
                    ),
                    else_=0
                )
            ).label("pending_recoveries"),
            func.sum(
                case(
                    (
                        RecoveryAttemptModel.status.in_(["failed", "verification_failed", "expired"]),
                        1
                    ),
                    else_=0
                )
            ).label("failed_recoveries"),
            func.sum(RecoveryAttemptModel.amount).label("total_amount_at_risk"),
            func.sum(
                case(
                    (
                        or_(
                            RecoveryAttemptModel.recovered_at.isnot(None),
                            RecoveryAttemptModel.status == "recovered"
                        ),
                        RecoveryAttemptModel.amount
                    ),
                    else_=0.0
                )
            ).label("total_amount_recovered")
        ).filter(
            RecoveryAttemptModel.failure_reason.isnot(None)
        ).group_by(
            RecoveryAttemptModel.failure_reason
        ).all()

        category_data: Dict[str, Dict[str, Any]] = {}
        for r in results:
            reason_str = str(r.failure_reason)
            total = int(r.total_attempts or 0)
            success = int(r.successful_recoveries or 0)
            pending = int(r.pending_recoveries or 0)
            failed = int(r.failed_recoveries or 0)
            at_risk = float(r.total_amount_at_risk or 0.0)
            recovered = float(r.total_amount_recovered or 0.0)
            rate_pct = round((success / total * 100.0), 1) if total > 0 else 0.0
            rev_rate_pct = round((recovered / at_risk * 100.0), 1) if at_risk > 0 else 0.0

            category_data[reason_str] = {
                "failure_reason": reason_str,
                "total_attempts": total,
                "successful_recoveries": success,
                "pending_recoveries": pending,
                "failed_recoveries": failed,
                "total_amount_at_risk": round(at_risk, 2),
                "total_amount_recovered": round(recovered, 2),
                "recovery_rate_pct": rate_pct,
                "revenue_recovery_rate_pct": rev_rate_pct,
                "guardrail_status": "Automated Recovery"
            }

        # Ensure all official FailureReason categories appear (including RISK_BLOCKED with 0 attempts)
        if include_all_categories:
            for reason in FailureReason:
                r_val = reason.value
                if r_val not in category_data:
                    guardrail_note = "100% Quarantined (Risk Isolation)" if reason in [FailureReason.RISK_BLOCKED, FailureReason.UNKNOWN] else "No Live Attempts"
                    category_data[r_val] = {
                        "failure_reason": r_val,
                        "total_attempts": 0,
                        "successful_recoveries": 0,
                        "pending_recoveries": 0,
                        "failed_recoveries": 0,
                        "total_amount_at_risk": 0.0,
                        "total_amount_recovered": 0.0,
                        "recovery_rate_pct": 0.0,
                        "revenue_recovery_rate_pct": 0.0,
                        "guardrail_status": guardrail_note
                    }

        rows = list(category_data.values())
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values(by=["total_attempts", "recovery_rate_pct"], ascending=False).reset_index(drop=True)
        return df
    finally:
        if close_session:
            db.close()


def compute_audit_log_recovery_by_root_cause(db: Optional[Session] = None) -> pd.DataFrame:
    """
    Computes live recovery breakdown across all transactions from AuditLogModel in SQLite.
    """
    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        results = db.query(
            AuditLogModel.root_cause,
            func.count(AuditLogModel.audit_id).label("total_transactions"),
            func.sum(
                case(
                    (
                        or_(
                            AuditLogModel.recovered == True,
                            AuditLogModel.execution_status == "recovered"
                        ),
                        1
                    ),
                    else_=0
                )
            ).label("recovered_count"),
            func.sum(
                case(
                    (
                        AuditLogModel.execution_status == "recovery_pending",
                        1
                    ),
                    else_=0
                )
            ).label("pending_count"),
            func.sum(
                case(
                    (
                        AuditLogModel.execution_status == "escalated",
                        1
                    ),
                    else_=0
                )
            ).label("escalated_count"),
            func.sum(AuditLogModel.amount).label("total_amount_at_risk"),
            func.sum(AuditLogModel.recovered_amount).label("total_amount_recovered")
        ).filter(
            AuditLogModel.root_cause.isnot(None)
        ).group_by(
            AuditLogModel.root_cause
        ).all()

        rows = []
        for r in results:
            total = int(r.total_transactions or 0)
            recovered_cnt = int(r.recovered_count or 0)
            pending_cnt = int(r.pending_count or 0)
            esc_cnt = int(r.escalated_count or 0)
            at_risk = float(r.total_amount_at_risk or 0.0)
            recovered_amt = float(r.total_amount_recovered or 0.0)
            rate_pct = round((recovered_cnt / total * 100.0), 1) if total > 0 else 0.0
            rev_pct = round((recovered_amt / at_risk * 100.0), 1) if at_risk > 0 else 0.0

            rows.append({
                "root_cause": str(r.root_cause),
                "total_transactions": total,
                "recovered_count": recovered_cnt,
                "pending_count": pending_cnt,
                "escalated_count": esc_cnt,
                "unrecovered_count": total - recovered_cnt - pending_cnt,
                "total_amount_at_risk": round(at_risk, 2),
                "total_amount_recovered": round(recovered_amt, 2),
                "recovery_rate_pct": rate_pct,
                "revenue_recovery_rate_pct": rev_pct
            })

        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values(by="total_amount_at_risk", ascending=False).reset_index(drop=True)
        return df
    finally:
        if close_session:
            db.close()


def load_recovery_schedules(db: Optional[Session] = None) -> pd.DataFrame:
    """
    Loads all scheduled cooldown retries from RecoveryScheduleModel in SQLite.
    """
    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        scheds = db.query(RecoveryScheduleModel).order_by(RecoveryScheduleModel.created_at.desc()).all()
        data = []
        for s in scheds:
            data.append({
                "id": s.id,
                "transaction_id": s.transaction_id,
                "failure_reason": s.failure_reason,
                "execute_at": s.execute_at,
                "status": s.status,
                "action_payload": s.action_payload,
                "created_at": s.created_at,
                "updated_at": s.updated_at
            })
        return pd.DataFrame(data) if data else pd.DataFrame()
    finally:
        if close_session:
            db.close()


def compute_escalation_guardrail_metrics(
    db: Optional[Session] = None,
    batch_run_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Directly queries AuditLogModel in SQLite for transactions escalated to human operations
    (quarantined by guardrails, e.g. RISK_BLOCKED or UNKNOWN).

    SQL / SQLAlchemy Logic:
    - Filters AuditLogModel where recommended_action = 'human_escalation'
    - Groups by root_cause
    - total_escalated_count: COUNT(audit_id)
    - total_amount_quarantined: SUM(amount)
    
    Returns structured real metrics:
    - total_escalated_count: total count of escalated records
    - total_amount_quarantined: sum of amounts for all escalated records
    - breakdown: list of dicts grouped by root_cause with exact counts and amounts
    - breakdown_by_cause: dict mapping root_cause -> {count, amount}
    - df: pandas DataFrame of the breakdown
    """
    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        query = db.query(
            AuditLogModel.root_cause,
            func.count(AuditLogModel.audit_id).label("escalated_count"),
            func.sum(AuditLogModel.amount).label("amount_quarantined")
        ).filter(
            AuditLogModel.recommended_action == "human_escalation"
        )

        if batch_run_id:
            query = query.filter(AuditLogModel.batch_run_id == batch_run_id)

        results = query.group_by(
            AuditLogModel.root_cause
        ).all()

        total_count = 0
        total_amount = 0.0
        breakdown = []
        breakdown_by_cause = {}

        for r in results:
            cause = str(r.root_cause) if r.root_cause else "unspecified"
            cnt = int(r.escalated_count or 0)
            amt = float(r.amount_quarantined or 0.0)
            total_count += cnt
            total_amount += amt
            item = {
                "root_cause": cause,
                "escalated_count": cnt,
                "amount_quarantined": round(amt, 2),
                "guardrail_action": "Quarantined for Human Ops (0% Retry Policy)"
            }
            breakdown.append(item)
            breakdown_by_cause[cause] = {"count": cnt, "amount": round(amt, 2)}

        df_breakdown = pd.DataFrame(breakdown) if breakdown else pd.DataFrame(columns=[
            "root_cause", "escalated_count", "amount_quarantined", "guardrail_action"
        ])

        return {
            "total_escalated_count": total_count,
            "total_amount_quarantined": round(total_amount, 2),
            "breakdown": breakdown,
            "breakdown_by_cause": breakdown_by_cause,
            "df": df_breakdown
        }
    finally:
        if close_session:
            db.close()
