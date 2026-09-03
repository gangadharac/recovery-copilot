from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
from app.schemas.agent_state import ToolResult, AgentToolName

class VerificationStatus(str, Enum):
    """Standardized verification outcome categories for tool results."""
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ESCALATED = "ESCALATED"
    ABANDONED = "ABANDONED"
    SCHEDULED = "SCHEDULED"
    PENDING = "PENDING"

class VerificationResult(BaseModel):
    """
    Result of evaluating a ToolResult in the autonomous agent loop.
    Directs whether the agent can terminate or must re-plan.
    """
    status: VerificationStatus
    is_terminal: bool                   # True if recovery succeeded or reached an intentional endpoint
    can_replan: bool                    # True if another recovery tool can be attempted
    summary: str                        # Concise verification summary for state & audit
    recovered_amount: float = Field(default=0.0, ge=0.0)

class Verifier:
    """
    Verification component that converts raw ToolResult into actionable agent lifecycle signals.
    """
    def verify(
        self,
        tool_result: ToolResult,
        current_iteration: int,
        max_iterations: int = 3
    ) -> VerificationResult:
        # Case 0: Payment Link Created (Recovery Pending - Awaiting Customer Payment)
        if tool_result.status == "recovery_pending":
            plink_id = tool_result.result_data.get("payment_link_id", "link")
            return VerificationResult(
                status=VerificationStatus.PENDING,
                is_terminal=True,
                can_replan=False,
                summary=f"Recovery payment link {plink_id} created; awaiting customer payment.",
                recovered_amount=0.0
            )

        # Case 1: Recovery Succeeded
        if tool_result.success and tool_result.recovered_amount > 0:
            return VerificationResult(
                status=VerificationStatus.SUCCESS,
                is_terminal=True,
                can_replan=False,
                summary=f"Tool '{tool_result.tool_name.value}' captured INR {tool_result.recovered_amount:,.2f}.",
                recovered_amount=tool_result.recovered_amount
            )

        # Case 2: Human Escalation (Quarantined)
        if tool_result.tool_name == AgentToolName.HUMAN_ESCALATION or tool_result.status == "escalated_to_ops":
            return VerificationResult(
                status=VerificationStatus.ESCALATED,
                is_terminal=True,
                can_replan=False,
                summary=f"Transaction escalated to human ops ({tool_result.error_reason or 'Risk/VIP quarantine'}).",
                recovered_amount=0.0
            )

        # Case 3: Intentional Give Up / Abandoned
        if tool_result.tool_name == AgentToolName.GIVE_UP or tool_result.status == "abandoned":
            return VerificationResult(
                status=VerificationStatus.ABANDONED,
                is_terminal=True,
                can_replan=False,
                summary=f"Recovery halted by stopping rule: {tool_result.error_reason or 'Unrecoverable'}.",
                recovered_amount=0.0
            )

        # Case 4: Tool execution failed, check if re-planning is permitted
        has_remaining_iterations = (current_iteration < max_iterations)
        
        return VerificationResult(
            status=VerificationStatus.FAILED,
            is_terminal=not has_remaining_iterations,
            can_replan=has_remaining_iterations,
            summary=f"Tool '{tool_result.tool_name.value}' failed ({tool_result.error_reason or 'No conversion'}).",
            recovered_amount=0.0
        )

# Global Verifier singleton
default_verifier = Verifier()
