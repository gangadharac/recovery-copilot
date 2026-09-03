from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from app.schemas.diagnosis import DiagnosisResult
from app.schemas.strategy import StrategyDecision
from app.schemas.agent_state import AutonomousAgentState, ThoughtStep, ToolResult

class ExecutionOutcome(BaseModel):
    transaction_id: str
    action_executed: str
    status: str                         # "recovered", "failed_retry", "nudged_sent", "escalated_to_ops", "abandoned"
    recovered: bool = False
    recovered_amount: float = 0.0
    gateway_switch_used: Optional[str] = None
    nudge_channel: Optional[str] = None # "whatsapp", "sms"
    nudge_message: Optional[str] = None
    unresolved_reason: Optional[str] = None
    execution_notes: str

class AuditTrailRecord(BaseModel):
    audit_id: str
    transaction_id: str
    customer_id: str
    customer_name: str
    timestamp: datetime
    amount: float
    currency: str = "INR"
    payment_method: str
    original_error_code: str
    diagnosis: DiagnosisResult
    strategy: StrategyDecision
    execution: ExecutionOutcome
    audit_summary: str
    agent_run_id: Optional[str] = None
    iterations_used: int = 1

class AgentTraceRecord(BaseModel):
    trace_id: str
    batch_run_id: str
    transaction_id: str
    agent_run_id: str
    iteration_number: int
    observation: str
    thought_summary: str
    action_tool: str
    tool_input_summary: str
    tool_result_status: str
    verification_status: str
    guardrail_decision: str
    recovered_amount: float = 0.0
    final_status: str
    termination_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
