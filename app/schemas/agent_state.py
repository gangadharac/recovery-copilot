import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field, model_validator

class AgentToolName(str, Enum):
    """Closed set of tools available to the autonomous recovery agent."""
    SWITCH_ROUTING = "switch_routing"
    WHATSAPP_NUDGE = "whatsapp_nudge"
    UPI_SWITCH = "upi_switch"
    HUMAN_ESCALATION = "human_escalation"
    GIVE_UP = "give_up"

class ToolCall(BaseModel):
    """Represents a specific tool invocation request by the agent."""
    call_id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:8]}")
    tool_name: AgentToolName
    input_arguments: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class ToolResult(BaseModel):
    """Represents the execution outcome of an invoked tool."""
    call_id: str
    tool_name: AgentToolName
    success: bool
    status: str                         # e.g., "recovered", "failed", "escalated", "abandoned", "nudged"
    result_data: Dict[str, Any] = Field(default_factory=dict)
    error_reason: Optional[str] = None
    recovered_amount: float = Field(default=0.0, ge=0.0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class ThoughtStep(BaseModel):
    """
    Represents an agent state transition / reasoning milestone.
    Stores concise decision summaries suitable for audit/debugging (no hidden CoT).
    """
    step_number: int = Field(ge=1)
    observation: str
    thought_summary: str
    selected_action: AgentToolName
    verification_result: Optional[str] = None
    reflection_summary: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class AutonomousAgentState(BaseModel):
    """
    Complete execution state and memory for one transaction recovery lifecycle.
    Enforces iteration bounding and full traceability.
    """
    transaction_id: str
    current_observation: str
    current_diagnosis: Optional[str] = None
    current_planned_action: Optional[AgentToolName] = None
    tool_calls: List[ToolCall] = Field(default_factory=list)
    tool_results: List[ToolResult] = Field(default_factory=list)
    thought_steps: List[ThoughtStep] = Field(default_factory=list)
    iteration_count: int = Field(default=0, ge=0)
    max_iterations: int = Field(default=3, ge=1, le=10)
    recovered_amount: float = Field(default=0.0, ge=0.0)
    final_status: str = Field(default="in_progress") # "in_progress", "recovered", "escalated", "abandoned"
    final_decision: Optional[str] = None
    termination_reason: Optional[str] = None

    @model_validator(mode="after")
    def validate_iteration_limits(self) -> "AutonomousAgentState":
        if self.iteration_count > self.max_iterations:
            raise ValueError(
                f"iteration_count ({self.iteration_count}) cannot exceed max_iterations ({self.max_iterations})"
            )
        return self

    def increment_iteration(self) -> int:
        """Safely increments iteration counter, enforcing max_iterations bound."""
        if self.iteration_count >= self.max_iterations:
            raise ValueError(
                f"Cannot advance agent state: max iterations ({self.max_iterations}) reached for transaction {self.transaction_id}"
            )
        self.iteration_count += 1
        return self.iteration_count

    def add_thought_step(self, step: ThoughtStep) -> None:
        """Appends a verified thought step to history."""
        self.thought_steps.append(step)

    def add_tool_execution(self, call: ToolCall, result: ToolResult) -> None:
        """Appends tool call and corresponding result to history."""
        self.tool_calls.append(call)
        self.tool_results.append(result)
        if result.success and result.recovered_amount > 0:
            self.recovered_amount = result.recovered_amount
            self.final_status = "recovered"
