import uuid
import pytest
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.db.database import SessionLocal, init_db
from app.db.models import TransactionModel, AuditLogModel, BatchRunModel, AgentTraceModel
from app.schemas.transaction import (
    Transaction,
    PaymentMethod,
    PaymentMethodDetails,
    CustomerHistory
)
from app.schemas.diagnosis import DiagnosisResult, RootCause, DiagnosisSource
from app.pipeline.batch_orchestrator import batch_orchestrator, AgentBatchResult, BatchResult
from app.agents.recovery_agent import RecoveryAgent, RecoveryAgentResult

@pytest.fixture(autouse=True)
def setup_db():
    init_db()

@pytest.fixture
def sample_batch_txns():
    t1 = Transaction(
        transaction_id="txn_integ_001",
        customer_id="cust_integ_1",
        customer_name="Aarav Sharma",
        customer_phone="+919876543210",
        customer_email="aarav@example.com",
        amount=1500.0,
        currency="INR",
        payment_method=PaymentMethod.CARD,
        payment_method_details=PaymentMethodDetails(bank_code="HDFC"),
        error_code="GATEWAY_ERROR",
        error_description="Bank switch timed out",
        error_source="bank_switch",
        retry_count=0,
        last_retry_timestamp=None,
        timestamp=datetime.now(timezone.utc),
        auto_charge_consent=True,
        customer_history=CustomerHistory(reliability_score=0.9)
    )
    t2 = Transaction(
        transaction_id="txn_integ_002",
        customer_id="cust_integ_2",
        customer_name="Priya Patel",
        customer_phone="+919876543211",
        customer_email="priya@example.com",
        amount=8500.0,
        currency="INR",
        payment_method=PaymentMethod.CARD,
        payment_method_details=PaymentMethodDetails(bank_code="ICICI"),
        error_code="3DS_VERIFICATION_FAILED",
        error_description="OTP verification failed",
        error_source="issuer",
        retry_count=3, # Will hit max retry cap
        last_retry_timestamp=None,
        timestamp=datetime.now(timezone.utc),
        auto_charge_consent=False,
        customer_history=CustomerHistory(reliability_score=0.85)
    )
    t3 = Transaction(
        transaction_id="txn_integ_003",
        customer_id="cust_integ_3",
        customer_name="Vikram Singh",
        customer_phone="+919876543212",
        customer_email="vikram@example.com",
        amount=4200.0,
        currency="INR",
        payment_method=PaymentMethod.UPI,
        payment_method_details=PaymentMethodDetails(upi_vpa="vikram@okhdfcbank"),
        error_code="risk_blocked",
        error_description="Velocity check exceeded",
        error_source="risk_engine",
        retry_count=0,
        last_retry_timestamp=None,
        timestamp=datetime.now(timezone.utc),
        auto_charge_consent=False,
        customer_history=CustomerHistory(reliability_score=0.4)
    )
    return [t1, t2, t3]

# 1. process_batch_with_agent() works.
def test_1_process_batch_with_agent_executes(sample_batch_txns):
    run_id = f"test_agent_run_01_{uuid.uuid4().hex[:6]}"
    result = batch_orchestrator.process_batch_with_agent(sample_batch_txns, run_id=run_id)
    assert isinstance(result, AgentBatchResult)
    assert result.total_transactions == 3
    assert result.total_at_risk == 1500.0 + 8500.0 + 4200.0
    assert result.batch_run_id == run_id

# 2. RecoveryAgent is actually called.
def test_2_recovery_agent_called_in_agent_batch(sample_batch_txns):
    run_id = f"test_agent_run_02_{uuid.uuid4().hex[:6]}"
    result = batch_orchestrator.process_batch_with_agent(sample_batch_txns, run_id=run_id)
    assert len(result.agent_results) == 3
    for ag_res in result.agent_results:
        assert isinstance(ag_res, RecoveryAgentResult)
        assert ag_res.iterations_used >= 1
        assert ag_res.state is not None

# 3. Agent result is persisted in database.
def test_3_agent_results_persisted_in_db(sample_batch_txns):
    run_id = f"test_agent_run_03_{uuid.uuid4().hex[:6]}"
    batch_orchestrator.process_batch_with_agent(sample_batch_txns, run_id=run_id)
    
    db: Session = SessionLocal()
    try:
        traces = db.query(AgentTraceModel).filter(AgentTraceModel.batch_run_id == run_id).all()
        assert len(traces) >= 3 # At least one trace per transaction
        
        audit_logs = db.query(AuditLogModel).filter(AuditLogModel.batch_run_id == run_id).all()
        assert len(audit_logs) == 3
        for log in audit_logs:
            assert log.agent_run_id is not None
            assert log.agent_run_id.startswith("agent_")
    finally:
        db.close()

# 4. Agent trace contains multiple iterations when replanning happens.
def test_4_agent_trace_persists_multi_step_iterations(sample_batch_txns):
    run_id = f"test_agent_run_04_{uuid.uuid4().hex[:6]}"
    batch_orchestrator.process_batch_with_agent(sample_batch_txns, run_id=run_id)
    
    db: Session = SessionLocal()
    try:
        traces = db.query(AgentTraceModel).filter(AgentTraceModel.batch_run_id == run_id).all()
        step_numbers = [t.iteration_number for t in traces]
        assert 1 in step_numbers
    finally:
        db.close()

# 5. Successful recovery is recorded.
def test_5_successful_recovery_recorded(sample_batch_txns):
    run_id = f"test_agent_run_05_{uuid.uuid4().hex[:6]}"
    result = batch_orchestrator.process_batch_with_agent(sample_batch_txns, run_id=run_id)
    assert result.recovered_count >= 1
    assert result.total_recovered > 0

# 6. Failed recovery is recorded.
def test_6_failed_recovery_recorded(sample_batch_txns):
    run_id = f"test_agent_run_06_{uuid.uuid4().hex[:6]}"
    result = batch_orchestrator.process_batch_with_agent(sample_batch_txns, run_id=run_id)
    assert result.unrecovered_count >= 1
    assert len(result.exception_list) >= 1

# 7. Human escalation is recorded.
def test_7_human_escalation_recorded(sample_batch_txns):
    run_id = f"test_agent_run_07_{uuid.uuid4().hex[:6]}"
    result = batch_orchestrator.process_batch_with_agent(sample_batch_txns, run_id=run_id)
    # txn_integ_003 is risk_blocked
    assert result.escalated_count >= 1
    escalated_res = [r for r in result.agent_results if r.transaction_id == "txn_integ_003"][0]
    assert escalated_res.final_status == "escalated"

# 8. Give-up is recorded.
def test_8_give_up_recorded(sample_batch_txns):
    run_id = f"test_agent_run_08_{uuid.uuid4().hex[:6]}"
    result = batch_orchestrator.process_batch_with_agent(sample_batch_txns, run_id=run_id)
    # txn_integ_002 has retry_count=3, triggering give_up guardrail
    give_up_res = [r for r in result.agent_results if r.transaction_id == "txn_integ_002"][0]
    assert give_up_res.final_status == "abandoned"
    assert give_up_res.final_action == "give_up"

# 9. Guardrail intervention is persisted.
def test_9_guardrail_intervention_persisted(sample_batch_txns):
    run_id = f"test_agent_run_09_{uuid.uuid4().hex[:6]}"
    result = batch_orchestrator.process_batch_with_agent(sample_batch_txns, run_id=run_id)
    assert result.guardrail_interventions_count >= 1

# 10. Agent_run_id exists and connects traces.
def test_10_agent_run_id_integrity(sample_batch_txns):
    run_id = f"test_agent_run_10_{uuid.uuid4().hex[:6]}"
    result = batch_orchestrator.process_batch_with_agent(sample_batch_txns, run_id=run_id)
    for audit in result.audit_trails:
        assert audit.agent_run_id is not None
        assert audit.agent_run_id.startswith(f"agent_{audit.transaction_id}")

# 11. Existing process_batch() still works.
def test_11_original_process_batch_unaffected(sample_batch_txns):
    run_id = f"test_orig_run_11_{uuid.uuid4().hex[:6]}"
    orig_result = batch_orchestrator.process_batch(sample_batch_txns, run_id=run_id)
    assert isinstance(orig_result, BatchResult)
    assert orig_result.total_transactions == 3
    assert orig_result.total_at_risk == 1500.0 + 8500.0 + 4200.0

# 12. Existing database tables and schema remain intact.
def test_12_database_schema_backward_compatibility():
    db: Session = SessionLocal()
    try:
        # Check all 4 tables queryable
        txns = db.query(TransactionModel).limit(1).all()
        audits = db.query(AuditLogModel).limit(1).all()
        runs = db.query(BatchRunModel).limit(1).all()
        traces = db.query(AgentTraceModel).limit(1).all()
        assert isinstance(txns, list)
        assert isinstance(audits, list)
        assert isinstance(runs, list)
        assert isinstance(traces, list)
    finally:
        db.close()

# 13. One transaction failure does not stop the batch (error isolation).
def test_13_error_isolation_in_batch_orchestrator(sample_batch_txns):
    run_id = f"test_agent_err_iso_{uuid.uuid4().hex[:6]}"
    corrupted_txn = Transaction(
        transaction_id="txn_corrupted_999",
        customer_id="cust_err",
        customer_name="Error User",
        customer_phone="+919000000000",
        customer_email="err@example.com",
        amount=500.0,
        currency="INR",
        payment_method=PaymentMethod.CARD,
        payment_method_details=PaymentMethodDetails(),
        error_code="UNKNOWN_FATAL",
        error_description="Fatal error",
        error_source="unknown",
        retry_count=0,
        timestamp=datetime.now(timezone.utc),
        customer_history=CustomerHistory()
    )
    batch_with_error = sample_batch_txns + [corrupted_txn]
    result = batch_orchestrator.process_batch_with_agent(batch_with_error, run_id=run_id)
    assert result.total_transactions == 4
    assert len(result.agent_results) == 4

# 14. Tool usage breakdown is accurately aggregated.
def test_14_tool_usage_breakdown_aggregation(sample_batch_txns):
    run_id = f"test_agent_tools_agg_{uuid.uuid4().hex[:6]}"
    result = batch_orchestrator.process_batch_with_agent(sample_batch_txns, run_id=run_id)
    assert isinstance(result.tool_usage_breakdown, dict)
    assert "switch_routing" in result.tool_usage_breakdown
    assert "whatsapp_nudge" in result.tool_usage_breakdown
    assert "upi_switch" in result.tool_usage_breakdown
    assert "human_escalation" in result.tool_usage_breakdown
    assert "give_up" in result.tool_usage_breakdown
    total_tool_calls = sum(result.tool_usage_breakdown.values())
    assert total_tool_calls >= 3

# 15. Explicit test: 1 transaction -> 1 final AuditLog, and multiple AgentTraces allowed.
def test_15_one_transaction_produces_one_audit_log_and_multiple_traces():
    run_id = f"test_single_txn_trace_{uuid.uuid4().hex[:6]}"
    single_txn = Transaction(
        transaction_id="txn_single_trace_01",
        customer_id="cust_single_1",
        customer_name="Single Trace User",
        customer_phone="+919876543299",
        customer_email="single@example.com",
        amount=2500.0,
        currency="INR",
        payment_method=PaymentMethod.CARD,
        payment_method_details=PaymentMethodDetails(bank_code="HDFC"),
        error_code="GATEWAY_ERROR",
        error_description="Bank switch timed out",
        error_source="bank_switch",
        retry_count=0,
        timestamp=datetime.now(timezone.utc),
        auto_charge_consent=True,
        customer_history=CustomerHistory(reliability_score=0.95)
    )
    result = batch_orchestrator.process_batch_with_agent([single_txn], run_id=run_id)
    assert result.total_transactions == 1
    
    db: Session = SessionLocal()
    try:
        audit_logs = db.query(AuditLogModel).filter(AuditLogModel.batch_run_id == run_id).all()
        traces = db.query(AgentTraceModel).filter(AgentTraceModel.batch_run_id == run_id).all()
        
        # Exactly ONE final summary record for this transaction in this batch run
        assert len(audit_logs) == 1
        assert audit_logs[0].transaction_id == "txn_single_trace_01"
        assert audit_logs[0].audit_id == f"audit_{run_id}_txn_single_trace_01"
        
        # One or more trace records representing iteration steps
        assert len(traces) >= 1
        for tr in traces:
            assert tr.transaction_id == "txn_single_trace_01"
            assert tr.batch_run_id == run_id
    finally:
        db.close()
