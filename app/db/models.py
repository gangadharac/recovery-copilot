from datetime import datetime
from sqlalchemy import Column, String, Float, Integer, Boolean, DateTime, Text, JSON
from app.db.database import Base

class TransactionModel(Base):
    __tablename__ = "transactions"

    transaction_id = Column(String(64), primary_key=True, index=True)
    customer_id = Column(String(64), index=True)
    customer_name = Column(String(128))
    customer_phone = Column(String(32))
    customer_email = Column(String(128))
    amount = Column(Float, nullable=False)
    currency = Column(String(8), default="INR")
    payment_method = Column(String(32))
    error_code = Column(String(64))
    error_description = Column(Text)
    error_source = Column(String(64))
    retry_count = Column(Integer, default=0)
    auto_charge_consent = Column(Boolean, default=False)
    raw_payload = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Phase 4 & 5 Additive Fields for Razorpay Event Correlation & Agent Run
    payment_id = Column(String(64), nullable=True, index=True)
    order_id = Column(String(64), nullable=True, index=True)
    lifecycle_status = Column(String(32), default="initiated") # "payment_failed", "authorized", "captured", "paid", "already_recovered"
    agent_run_id = Column(String(64), nullable=True, index=True)

class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    audit_id = Column(String(64), primary_key=True, index=True)
    batch_run_id = Column(String(64), index=True)
    transaction_id = Column(String(64), index=True)
    customer_id = Column(String(64), index=True)
    customer_name = Column(String(128))
    amount = Column(Float, nullable=False)
    currency = Column(String(8), default="INR")
    payment_method = Column(String(32))
    original_error_code = Column(String(64))
    
    # Diagnosis Stage
    root_cause = Column(String(64))
    diagnosis_confidence = Column(Float)
    diagnosis_source = Column(String(32))
    diagnosis_reasoning = Column(Text)
    
    # Strategy Stage
    recommended_action = Column(String(64))
    original_action = Column(String(64))
    guardrail_overridden = Column(Boolean, default=False)
    guardrail_checks = Column(JSON)
    strategy_reasoning = Column(Text)
    give_up_reason = Column(Text, nullable=True) # Explicit human-readable reason for exception list
    
    # Execution Stage
    execution_status = Column(String(64))        # recovered, failed_retry, nudged_sent, escalated_to_ops, abandoned
    recovered = Column(Boolean, default=False)
    recovered_amount = Column(Float, default=0.0)
    gateway_switch_used = Column(String(64), nullable=True)
    nudge_channel = Column(String(32), nullable=True)
    nudge_message = Column(Text, nullable=True)
    unresolved_reason = Column(Text, nullable=True)
    execution_notes = Column(Text)
    
    # Agent Additions (Additive & Backward-Compatible)
    agent_run_id = Column(String(64), nullable=True, index=True)
    iterations_used = Column(Integer, default=1)
    
    # Audit summary
    audit_summary = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

class BatchRunModel(Base):
    __tablename__ = "batch_runs"

    run_id = Column(String(64), primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    total_transactions = Column(Integer, default=0)
    total_amount_at_risk = Column(Float, default=0.0)
    total_amount_recovered = Column(Float, default=0.0)
    recovery_rate_pct = Column(Float, default=0.0)
    recovered_count = Column(Integer, default=0)
    unrecovered_count = Column(Integer, default=0)
    escalated_count = Column(Integer, default=0)
    nudged_count = Column(Integer, default=0)
    root_cause_breakdown = Column(JSON)
    action_breakdown = Column(JSON)

class AgentTraceModel(Base):
    """
    Phase 3 Additive Table:
    Stores granular, multi-step execution traces for each iteration of an Autonomous Agent run.
    Contains no hidden chain-of-thought; only concise structured summaries for auditing.
    """
    __tablename__ = "agent_traces"

    trace_id = Column(String(64), primary_key=True, index=True)
    batch_run_id = Column(String(64), index=True)
    transaction_id = Column(String(64), index=True)
    agent_run_id = Column(String(64), index=True)
    iteration_number = Column(Integer, default=1)
    observation = Column(Text)
    thought_summary = Column(Text)
    action_tool = Column(String(64))
    tool_input_summary = Column(Text)
    tool_result_status = Column(String(64))
    verification_status = Column(String(64))
    guardrail_decision = Column(Text)
    recovered_amount = Column(Float, default=0.0)
    final_status = Column(String(64))
    termination_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class WebhookEventModel(Base):
    """
    Phase 4 Additive Table:
    Stores ingested Razorpay test-mode webhook events with idempotency tracking and payload hashes.
    """
    __tablename__ = "webhook_events"

    id = Column(String(64), primary_key=True, index=True)
    event_id = Column(String(64), unique=True, index=True)
    event_type = Column(String(64), index=True) # payment.failed, payment.captured, order.paid, payment.authorized
    payment_id = Column(String(64), nullable=True, index=True)
    order_id = Column(String(64), nullable=True, index=True)
    received_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)
    status = Column(String(32), default="received") # "received", "processed", "duplicate", "ignored", "failed"
    payload_json = Column(JSON)
    payload_hash = Column(String(64), index=True) # SHA-256 of raw body
    error_message = Column(Text, nullable=True)

class RecoveryAttemptModel(Base):
    """
    Phase 6 Additive Table:
    Tracks real Razorpay Test-Mode Payment Links created for transaction recovery.
    Manages payment link lifecycle from pending -> recovered.
    """
    __tablename__ = "recovery_attempts"

    id = Column(String(64), primary_key=True, index=True) # rec_attempt_...
    transaction_id = Column(String(64), index=True, nullable=False)
    agent_run_id = Column(String(64), nullable=True, index=True)
    payment_link_id = Column(String(64), nullable=True, index=True) # plink_...
    payment_link_url = Column(Text, nullable=True) # https://rzp.io/i/...
    status = Column(String(32), default="pending", index=True) # created, pending, recovered, expired, failed
    amount = Column(Float, nullable=False)
    currency = Column(String(8), default="INR")
    customer_id = Column(String(64), nullable=True)
    customer_contact = Column(String(32), nullable=True)
    customer_email = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    recovered_at = Column(DateTime, nullable=True)
