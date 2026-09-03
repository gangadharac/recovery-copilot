import uuid
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session

from app.config import settings
from app.db.database import SessionLocal, init_db
from app.db.models import TransactionModel, AuditLogModel, BatchRunModel, AgentTraceModel
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult, RootCause
from app.schemas.strategy import StrategyDecision, RecoveryAction, GuardrailCheck
from app.schemas.audit import ExecutionOutcome, AuditTrailRecord, AgentTraceRecord
from app.agents.diagnosis_agent import diagnosis_agent
from app.agents.strategy_agent import strategy_agent
from app.executor.recovery_executor import recovery_executor
from app.agents.recovery_agent import recovery_agent, RecoveryAgentResult

logger = logging.getLogger(__name__)

class BatchResult:
    def __init__(
        self,
        batch_run_id: str,
        total_transactions: int,
        total_at_risk: float,
        total_recovered: float,
        recovery_rate_pct: float,
        recovered_count: int,
        unrecovered_count: int,
        escalated_count: int,
        nudged_count: int,
        root_cause_breakdown: Dict[str, Any],
        action_breakdown: Dict[str, Any],
        exception_list: List[Dict[str, Any]],
        audit_trails: List[AuditTrailRecord]
    ):
        self.batch_run_id = batch_run_id
        self.total_transactions = total_transactions
        self.total_at_risk = total_at_risk
        self.total_recovered = total_recovered
        self.recovery_rate_pct = recovery_rate_pct
        self.recovered_count = recovered_count
        self.unrecovered_count = unrecovered_count
        self.escalated_count = escalated_count
        self.nudged_count = nudged_count
        self.root_cause_breakdown = root_cause_breakdown
        self.action_breakdown = action_breakdown
        self.exception_list = exception_list
        self.audit_trails = audit_trails

class AgentBatchResult(BatchResult):
    """
    Extended batch result containing autonomous agent multi-step metrics,
    re-planning stats, and tool usage breakdowns.
    """
    def __init__(
        self,
        batch_run_id: str,
        total_transactions: int,
        total_at_risk: float,
        total_recovered: float,
        recovery_rate_pct: float,
        recovered_count: int,
        unrecovered_count: int,
        escalated_count: int,
        nudged_count: int,
        root_cause_breakdown: Dict[str, Any],
        action_breakdown: Dict[str, Any],
        exception_list: List[Dict[str, Any]],
        audit_trails: List[AuditTrailRecord],
        # Autonomous Agent Specific Metrics
        recovered_first_attempt_count: int,
        recovered_after_replanning_count: int,
        avg_iterations: float,
        max_iterations_reached_count: int,
        tool_usage_breakdown: Dict[str, int],
        guardrail_interventions_count: int,
        agent_results: List[RecoveryAgentResult]
    ):
        super().__init__(
            batch_run_id=batch_run_id,
            total_transactions=total_transactions,
            total_at_risk=total_at_risk,
            total_recovered=total_recovered,
            recovery_rate_pct=recovery_rate_pct,
            recovered_count=recovered_count,
            unrecovered_count=unrecovered_count,
            escalated_count=escalated_count,
            nudged_count=nudged_count,
            root_cause_breakdown=root_cause_breakdown,
            action_breakdown=action_breakdown,
            exception_list=exception_list,
            audit_trails=audit_trails
        )
        self.recovered_first_attempt_count = recovered_first_attempt_count
        self.recovered_after_replanning_count = recovered_after_replanning_count
        self.avg_iterations = avg_iterations
        self.max_iterations_reached_count = max_iterations_reached_count
        self.tool_usage_breakdown = tool_usage_breakdown
        self.guardrail_interventions_count = guardrail_interventions_count
        self.agent_results = agent_results

class BatchOrchestrator:
    """
    End-to-end Pipeline Orchestrator:
    Supports both:
    1. process_batch(): The stable original single-pass 5-stage pipeline.
    2. process_batch_with_agent(): The autonomous multi-step agent loop with re-planning.
    """
    def __init__(self):
        init_db()

    def process_batch(self, transactions: List[Transaction], run_id: Optional[str] = None) -> BatchResult:
        """
        Original Single-Pass Linear Pipeline:
        Ingests transactions -> Diagnoses root cause -> Decides bounded strategy ->
        Executes simulated recovery / nudges -> Records SQLite audit log -> Computes metrics.
        """
        if not run_id:
            run_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        logger.info(f"Starting batch recovery run: {run_id} ({len(transactions)} transactions)")
        
        audit_records: List[AuditTrailRecord] = []
        db: Session = SessionLocal()
        
        total_at_risk = 0.0
        total_recovered = 0.0
        recovered_count = 0
        unrecovered_count = 0
        escalated_count = 0
        nudged_count = 0
        
        # Breakdown structures
        rc_breakdown: Dict[str, Dict[str, Any]] = {}
        for rc in RootCause:
            rc_breakdown[rc.value] = {
                "count": 0,
                "amount_at_risk": 0.0,
                "amount_recovered": 0.0,
                "recovered_count": 0,
                "recovery_rate_pct": 0.0
            }
            
        action_breakdown: Dict[str, Dict[str, Any]] = {}
        for act in RecoveryAction:
            action_breakdown[act.value] = {
                "count": 0,
                "amount": 0.0,
                "recovered_count": 0,
                "recovered_amount": 0.0
            }

        exception_list: List[Dict[str, Any]] = []

        try:
            for txn in transactions:
                total_at_risk += txn.amount
                
                # 1. Diagnose Root Cause
                diagnosis: DiagnosisResult = diagnosis_agent.diagnose(txn)
                
                # 2. Decide Guardrailed Strategy
                decision: StrategyDecision = strategy_agent.decide_action(txn, diagnosis)
                
                # 3. Execute Recovery Action
                outcome: ExecutionOutcome = recovery_executor.execute(txn, diagnosis, decision)
                
                # Update Counters & Breakdowns
                rc_key = diagnosis.root_cause.value
                rc_breakdown[rc_key]["count"] += 1
                rc_breakdown[rc_key]["amount_at_risk"] += txn.amount
                
                act_key = decision.action.value
                action_breakdown[act_key]["count"] += 1
                action_breakdown[act_key]["amount"] += txn.amount
                
                if outcome.recovered:
                    total_recovered += outcome.recovered_amount
                    recovered_count += 1
                    rc_breakdown[rc_key]["amount_recovered"] += outcome.recovered_amount
                    rc_breakdown[rc_key]["recovered_count"] += 1
                    action_breakdown[act_key]["recovered_count"] += 1
                    action_breakdown[act_key]["recovered_amount"] += outcome.recovered_amount
                else:
                    unrecovered_count += 1
                    
                if decision.action == RecoveryAction.ESCALATE_HUMAN:
                    escalated_count += 1
                if decision.action in [RecoveryAction.NUDGE_CUSTOMER, RecoveryAction.OFFER_ALT_METHOD]:
                    nudged_count += 1

                # Construct Executive Summary String for Audit
                audit_summary = (
                    f"[{txn.transaction_id}] INR {txn.amount:,.2f} | "
                    f"Diagnosed: {diagnosis.root_cause.value} ({diagnosis.source.value}, conf {diagnosis.confidence:.2f}) | "
                    f"Action: {decision.action.value} | "
                    f"Outcome: {outcome.status} | Recovered: ₹{outcome.recovered_amount:,.2f}"
                )
                
                audit_rec = AuditTrailRecord(
                    audit_id=f"audit_{run_id}_{txn.transaction_id}",
                    transaction_id=txn.transaction_id,
                    customer_id=txn.customer_id,
                    customer_name=txn.customer_name,
                    timestamp=txn.timestamp,
                    amount=txn.amount,
                    currency=txn.currency,
                    payment_method=txn.payment_method.value,
                    original_error_code=txn.error_code,
                    diagnosis=diagnosis,
                    strategy=decision,
                    execution=outcome,
                    audit_summary=audit_summary
                )
                audit_records.append(audit_rec)
                
                # If unrecovered or give_up or escalated, capture in honest exception list
                if not outcome.recovered:
                    exception_list.append({
                        "transaction_id": txn.transaction_id,
                        "customer_name": txn.customer_name,
                        "amount": txn.amount,
                        "payment_method": txn.payment_method.value,
                        "root_cause": diagnosis.root_cause.value,
                        "action_taken": decision.action.value,
                        "unresolved_reason": outcome.unresolved_reason or decision.give_up_reason or "Unknown exception",
                        "retry_count": txn.retry_count,
                        "timestamp": txn.timestamp.isoformat()
                    })

                # Save to DB (Transaction + Audit Log)
                db_txn = TransactionModel(
                    transaction_id=txn.transaction_id,
                    customer_id=txn.customer_id,
                    customer_name=txn.customer_name,
                    customer_phone=txn.customer_phone,
                    customer_email=txn.customer_email,
                    amount=txn.amount,
                    currency=txn.currency,
                    payment_method=txn.payment_method.value,
                    error_code=txn.error_code,
                    error_description=txn.error_description,
                    error_source=txn.error_source,
                    retry_count=txn.retry_count,
                    auto_charge_consent=txn.auto_charge_consent,
                    raw_payload=txn.model_dump(mode="json"),
                    created_at=txn.timestamp
                )
                db.merge(db_txn)

                db_audit = AuditLogModel(
                    audit_id=audit_rec.audit_id,
                    batch_run_id=run_id,
                    transaction_id=txn.transaction_id,
                    customer_id=txn.customer_id,
                    customer_name=txn.customer_name,
                    amount=txn.amount,
                    currency=txn.currency,
                    payment_method=txn.payment_method.value,
                    original_error_code=txn.error_code,
                    root_cause=diagnosis.root_cause.value,
                    diagnosis_confidence=diagnosis.confidence,
                    diagnosis_source=diagnosis.source.value,
                    diagnosis_reasoning=diagnosis.reasoning,
                    recommended_action=decision.action.value,
                    original_action=decision.original_action.value,
                    guardrail_overridden=decision.guardrail_overridden,
                    guardrail_checks=[g.model_dump() for g in decision.guardrail_checks],
                    strategy_reasoning=decision.reasoning,
                    give_up_reason=decision.give_up_reason,
                    execution_status=outcome.status,
                    recovered=outcome.recovered,
                    recovered_amount=outcome.recovered_amount,
                    gateway_switch_used=outcome.gateway_switch_used,
                    nudge_channel=outcome.nudge_channel,
                    nudge_message=outcome.nudge_message,
                    unresolved_reason=outcome.unresolved_reason,
                    execution_notes=outcome.execution_notes,
                    audit_summary=audit_summary,
                    created_at=datetime.now(timezone.utc)
                )
                db.merge(db_audit)

            # Compute percentage rates for root cause breakdown
            for k, val in rc_breakdown.items():
                if val["amount_at_risk"] > 0:
                    val["recovery_rate_pct"] = round((val["amount_recovered"] / val["amount_at_risk"]) * 100, 2)
                else:
                    val["recovery_rate_pct"] = 0.0

            overall_rate = round((total_recovered / total_at_risk) * 100, 2) if total_at_risk > 0 else 0.0

            # Record Batch Run Summary in DB
            db_run = BatchRunModel(
                run_id=run_id,
                timestamp=datetime.now(timezone.utc),
                total_transactions=len(transactions),
                total_amount_at_risk=round(total_at_risk, 2),
                total_amount_recovered=round(total_recovered, 2),
                recovery_rate_pct=overall_rate,
                recovered_count=recovered_count,
                unrecovered_count=unrecovered_count,
                escalated_count=escalated_count,
                nudged_count=nudged_count,
                root_cause_breakdown=rc_breakdown,
                action_breakdown=action_breakdown
            )
            db.merge(db_run)
            db.commit()

        except Exception as e:
            db.rollback()
            logger.error(f"Error processing batch: {e}", exc_info=True)
            raise e
        finally:
            db.close()

        logger.info(
            f"Batch {run_id} complete: ₹{total_recovered:,.2f} recovered from ₹{total_at_risk:,.2f} at risk ({overall_rate}%)"
        )

        return BatchResult(
            batch_run_id=run_id,
            total_transactions=len(transactions),
            total_at_risk=round(total_at_risk, 2),
            total_recovered=round(total_recovered, 2),
            recovery_rate_pct=overall_rate,
            recovered_count=recovered_count,
            unrecovered_count=unrecovered_count,
            escalated_count=escalated_count,
            nudged_count=nudged_count,
            root_cause_breakdown=rc_breakdown,
            action_breakdown=action_breakdown,
            exception_list=exception_list,
            audit_trails=audit_records
        )

    def process_batch_with_agent(
        self,
        transactions: List[Transaction],
        run_id: Optional[str] = None
    ) -> AgentBatchResult:
        """
        Autonomous Recovery Agent Batch Pipeline (Phase 3):
        Processes transactions through the multi-step ReAct recovery loop with adaptive re-planning.
        Persists granular AgentTrace records and computes aggregate autonomous agent metrics.
        """
        if not run_id:
            run_id = f"agent_batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        logger.info(f"Starting Autonomous Agent batch recovery run: {run_id} ({len(transactions)} transactions)")

        audit_records: List[AuditTrailRecord] = []
        agent_results: List[RecoveryAgentResult] = []
        db: Session = SessionLocal()

        total_at_risk = 0.0
        total_recovered = 0.0
        recovered_count = 0
        unrecovered_count = 0
        escalated_count = 0
        nudged_count = 0

        recovered_first_attempt = 0
        recovered_after_replanning = 0
        total_iterations_used = 0
        max_iterations_reached = 0
        guardrail_interventions = 0

        tool_usage_counts: Dict[str, int] = {
            "switch_routing": 0,
            "whatsapp_nudge": 0,
            "upi_switch": 0,
            "human_escalation": 0,
            "give_up": 0
        }

        # Breakdown structures
        rc_breakdown: Dict[str, Dict[str, Any]] = {}
        for rc in RootCause:
            rc_breakdown[rc.value] = {
                "count": 0,
                "amount_at_risk": 0.0,
                "amount_recovered": 0.0,
                "recovered_count": 0,
                "recovery_rate_pct": 0.0
            }

        action_breakdown: Dict[str, Dict[str, Any]] = {
            "switch_routing": {"count": 0, "amount": 0.0, "recovered_count": 0, "recovered_amount": 0.0},
            "whatsapp_nudge": {"count": 0, "amount": 0.0, "recovered_count": 0, "recovered_amount": 0.0},
            "upi_switch": {"count": 0, "amount": 0.0, "recovered_count": 0, "recovered_amount": 0.0},
            "human_escalation": {"count": 0, "amount": 0.0, "recovered_count": 0, "recovered_amount": 0.0},
            "give_up": {"count": 0, "amount": 0.0, "recovered_count": 0, "recovered_amount": 0.0},
        }

        exception_list: List[Dict[str, Any]] = []

        try:
            for txn in transactions:
                total_at_risk += txn.amount
                agent_run_id = f"agent_{txn.transaction_id}_{uuid.uuid4().hex[:6]}"

                # ERROR ISOLATION: Single transaction failure must not crash batch
                try:
                    agent_res: RecoveryAgentResult = recovery_agent.recover(txn)
                except Exception as txn_err:
                    logger.error(f"Error processing agent recovery for {txn.transaction_id}: {txn_err}", exc_info=True)
                    # Create safe fallback result for crashed transaction
                    from app.schemas.agent_state import AutonomousAgentState
                    dummy_state = AutonomousAgentState(
                        transaction_id=txn.transaction_id,
                        current_observation="Exception encountered during recovery",
                        final_status="abandoned",
                        termination_reason=f"Processing exception: {str(txn_err)}"
                    )
                    agent_res = RecoveryAgentResult(
                        transaction_id=txn.transaction_id,
                        final_status="abandoned",
                        final_action="give_up",
                        recovered_amount=0.0,
                        iterations_used=1,
                        concise_decision_summary=f"[{txn.transaction_id}] Error: {str(txn_err)}",
                        unresolved_reason=f"Processing error: {str(txn_err)}",
                        state=dummy_state
                    )

                agent_results.append(agent_res)
                is_recovered = (agent_res.final_status == "recovered" and agent_res.recovered_amount > 0)
                
                # Count tools executed across all iterations
                for t_call in agent_res.state.tool_calls:
                    tool_name_str = t_call.tool_name.value if hasattr(t_call.tool_name, "value") else str(t_call.tool_name)
                    if tool_name_str in tool_usage_counts:
                        tool_usage_counts[tool_name_str] += 1

                total_iterations_used += agent_res.iterations_used
                if agent_res.iterations_used >= agent_res.state.max_iterations:
                    max_iterations_reached += 1

                # Check if guardrail intervened in any thought step
                for step in agent_res.thought_history:
                    if "guardrail" in step.thought_summary.lower() and "intervened" in step.thought_summary.lower():
                        guardrail_interventions += 1

                # Update Root Cause breakdown
                rc_key = agent_res.diagnosis.root_cause.value if agent_res.diagnosis else RootCause.UNKNOWN.value
                if rc_key in rc_breakdown:
                    rc_breakdown[rc_key]["count"] += 1
                    rc_breakdown[rc_key]["amount_at_risk"] += txn.amount

                # Update Action breakdown
                act_key = agent_res.final_action
                if act_key in action_breakdown:
                    action_breakdown[act_key]["count"] += 1
                    action_breakdown[act_key]["amount"] += txn.amount

                if is_recovered:
                    total_recovered += agent_res.recovered_amount
                    recovered_count += 1
                    if rc_key in rc_breakdown:
                        rc_breakdown[rc_key]["amount_recovered"] += agent_res.recovered_amount
                        rc_breakdown[rc_key]["recovered_count"] += 1
                    if act_key in action_breakdown:
                        action_breakdown[act_key]["recovered_count"] += 1
                        action_breakdown[act_key]["recovered_amount"] += agent_res.recovered_amount

                    if agent_res.iterations_used == 1:
                        recovered_first_attempt += 1
                    else:
                        recovered_after_replanning += 1
                else:
                    unrecovered_count += 1

                if agent_res.final_status == "escalated":
                    escalated_count += 1
                if agent_res.final_action in ["whatsapp_nudge", "upi_switch"]:
                    nudged_count += 1

                # Convert to AuditTrailRecord for compatibility
                last_tool_res = agent_res.tool_history[-1] if agent_res.tool_history else None
                nudge_msg = None
                switch_used = None
                if last_tool_res and last_tool_res.result_data:
                    nudge_msg = last_tool_res.result_data.get("nudge_message")
                    switch_used = last_tool_res.result_data.get("gateway_switch_used")

                outcome = ExecutionOutcome(
                    transaction_id=txn.transaction_id,
                    action_executed=agent_res.final_action,
                    status=agent_res.final_status,
                    recovered=is_recovered,
                    recovered_amount=agent_res.recovered_amount,
                    gateway_switch_used=switch_used,
                    nudge_channel="whatsapp" if nudge_msg else None,
                    nudge_message=nudge_msg,
                    unresolved_reason=agent_res.unresolved_reason,
                    execution_notes=f"Autonomous Agent completed in {agent_res.iterations_used} iterations."
                )

                strategy_dec = StrategyDecision(
                    transaction_id=txn.transaction_id,
                    action=RecoveryAction(agent_res.final_action) if agent_res.final_action in [a.value for a in RecoveryAction] else RecoveryAction.GIVE_UP,
                    original_action=RecoveryAction.RETRY_NOW,
                    reasoning=agent_res.concise_decision_summary,
                    guardrail_checks=[]
                )

                audit_rec = AuditTrailRecord(
                    audit_id=f"audit_{run_id}_{txn.transaction_id}",
                    transaction_id=txn.transaction_id,
                    customer_id=txn.customer_id,
                    customer_name=txn.customer_name,
                    timestamp=txn.timestamp,
                    amount=txn.amount,
                    currency=txn.currency,
                    payment_method=txn.payment_method.value,
                    original_error_code=txn.error_code,
                    diagnosis=agent_res.diagnosis or diagnosis_agent.diagnose(txn),
                    strategy=strategy_dec,
                    execution=outcome,
                    audit_summary=agent_res.concise_decision_summary,
                    agent_run_id=agent_run_id,
                    iterations_used=agent_res.iterations_used
                )
                audit_records.append(audit_rec)

                # Honest Exception tracking
                if not is_recovered:
                    exception_list.append({
                        "transaction_id": txn.transaction_id,
                        "customer_name": txn.customer_name,
                        "amount": txn.amount,
                        "payment_method": txn.payment_method.value,
                        "root_cause": rc_key,
                        "action_taken": agent_res.final_action,
                        "unresolved_reason": agent_res.unresolved_reason or "Recovery uncompleted",
                        "retry_count": txn.retry_count,
                        "timestamp": txn.timestamp.isoformat()
                    })

                # Persist Transaction & Audit Log
                db_txn = TransactionModel(
                    transaction_id=txn.transaction_id,
                    customer_id=txn.customer_id,
                    customer_name=txn.customer_name,
                    customer_phone=txn.customer_phone,
                    customer_email=txn.customer_email,
                    amount=txn.amount,
                    currency=txn.currency,
                    payment_method=txn.payment_method.value,
                    error_code=txn.error_code,
                    error_description=txn.error_description,
                    error_source=txn.error_source,
                    retry_count=txn.retry_count,
                    auto_charge_consent=txn.auto_charge_consent,
                    raw_payload=txn.model_dump(mode="json"),
                    created_at=txn.timestamp
                )
                db.merge(db_txn)

                db_audit = AuditLogModel(
                    audit_id=audit_rec.audit_id,
                    batch_run_id=run_id,
                    transaction_id=txn.transaction_id,
                    customer_id=txn.customer_id,
                    customer_name=txn.customer_name,
                    amount=txn.amount,
                    currency=txn.currency,
                    payment_method=txn.payment_method.value,
                    original_error_code=txn.error_code,
                    root_cause=rc_key,
                    diagnosis_confidence=agent_res.diagnosis.confidence if agent_res.diagnosis else 0.9,
                    diagnosis_source=agent_res.diagnosis.source.value if agent_res.diagnosis else "rules",
                    diagnosis_reasoning=agent_res.diagnosis.reasoning if agent_res.diagnosis else "",
                    recommended_action=agent_res.final_action,
                    original_action=agent_res.state.tool_calls[0].tool_name.value if agent_res.state.tool_calls else agent_res.final_action,
                    guardrail_overridden=any("guardrail" in s.thought_summary.lower() for s in agent_res.thought_history),
                    guardrail_checks=[],
                    strategy_reasoning=agent_res.concise_decision_summary,
                    give_up_reason=agent_res.unresolved_reason,
                    execution_status=agent_res.final_status,
                    recovered=is_recovered,
                    recovered_amount=agent_res.recovered_amount,
                    gateway_switch_used=switch_used,
                    nudge_channel="whatsapp" if nudge_msg else None,
                    nudge_message=nudge_msg,
                    unresolved_reason=agent_res.unresolved_reason,
                    execution_notes=outcome.execution_notes,
                    audit_summary=agent_res.concise_decision_summary,
                    agent_run_id=agent_run_id,
                    iterations_used=agent_res.iterations_used,
                    created_at=datetime.now(timezone.utc)
                )
                db.merge(db_audit)

                # Persist Multi-Step Agent Traces (one per iteration)
                for step in agent_res.thought_history:
                    step_call = agent_res.state.tool_calls[step.step_number - 1] if step.step_number <= len(agent_res.state.tool_calls) else None
                    step_result = agent_res.state.tool_results[step.step_number - 1] if step.step_number <= len(agent_res.state.tool_results) else None

                    db_trace = AgentTraceModel(
                        trace_id=f"trace_{run_id}_{txn.transaction_id}_step{step.step_number}",
                        batch_run_id=run_id,
                        transaction_id=txn.transaction_id,
                        agent_run_id=agent_run_id,
                        iteration_number=step.step_number,
                        observation=step.observation,
                        thought_summary=step.thought_summary,
                        action_tool=step.selected_action.value if hasattr(step.selected_action, "value") else str(step.selected_action),
                        tool_input_summary=str(step_call.input_arguments if step_call else {}),
                        tool_result_status=step_result.status if step_result else "unknown",
                        verification_status=step.verification_result or "UNKNOWN",
                        guardrail_decision="intervened" if "guardrail" in step.thought_summary.lower() else "passed",
                        recovered_amount=step_result.recovered_amount if step_result else 0.0,
                        final_status=agent_res.final_status,
                        termination_reason=agent_res.unresolved_reason,
                        created_at=step.timestamp
                    )
                    db.merge(db_trace)

            # Compute percentage rates
            for k, val in rc_breakdown.items():
                if val["amount_at_risk"] > 0:
                    val["recovery_rate_pct"] = round((val["amount_recovered"] / val["amount_at_risk"]) * 100, 2)
                else:
                    val["recovery_rate_pct"] = 0.0

            overall_rate = round((total_recovered / total_at_risk) * 100, 2) if total_at_risk > 0 else 0.0
            avg_iters = round(total_iterations_used / len(transactions), 2) if transactions else 1.0

            # Record Batch Run Summary in DB
            db_run = BatchRunModel(
                run_id=run_id,
                timestamp=datetime.now(timezone.utc),
                total_transactions=len(transactions),
                total_amount_at_risk=round(total_at_risk, 2),
                total_amount_recovered=round(total_recovered, 2),
                recovery_rate_pct=overall_rate,
                recovered_count=recovered_count,
                unrecovered_count=unrecovered_count,
                escalated_count=escalated_count,
                nudged_count=nudged_count,
                root_cause_breakdown=rc_breakdown,
                action_breakdown=action_breakdown
            )
            db.merge(db_run)
            db.commit()

        except Exception as e:
            db.rollback()
            logger.error(f"Error processing agent batch: {e}", exc_info=True)
            raise e
        finally:
            db.close()

        logger.info(
            f"Agent Batch {run_id} complete: ₹{total_recovered:,.2f} recovered ({overall_rate}%) across {len(transactions)} txns."
        )

        return AgentBatchResult(
            batch_run_id=run_id,
            total_transactions=len(transactions),
            total_at_risk=round(total_at_risk, 2),
            total_recovered=round(total_recovered, 2),
            recovery_rate_pct=overall_rate,
            recovered_count=recovered_count,
            unrecovered_count=unrecovered_count,
            escalated_count=escalated_count,
            nudged_count=nudged_count,
            root_cause_breakdown=rc_breakdown,
            action_breakdown=action_breakdown,
            exception_list=exception_list,
            audit_trails=audit_records,
            recovered_first_attempt_count=recovered_first_attempt,
            recovered_after_replanning_count=recovered_after_replanning,
            avg_iterations=avg_iters,
            max_iterations_reached_count=max_iterations_reached,
            tool_usage_breakdown=tool_usage_counts,
            guardrail_interventions_count=guardrail_interventions,
            agent_results=agent_results
        )

# Global Orchestrator singleton
batch_orchestrator = BatchOrchestrator()
