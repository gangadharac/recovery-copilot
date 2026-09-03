import uuid
from typing import Dict, Any, Optional
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult
from app.schemas.agent_state import AgentToolName, ToolResult
from app.agents.tools.base_tool import BaseTool

class GiveUpTool(BaseTool):
    """
    Tool: give_up
    Thin adapter for stopping recovery when stopping rules are hit
    (e.g., max retry limit reached, customer declined, unrecoverable).
    """
    name = AgentToolName.GIVE_UP
    description = (
        "Permanently halts recovery attempts and logs an explicit, transparent stopping reason "
        "for the Honest Exception List."
    )

    def execute(
        self,
        txn: Transaction,
        diagnosis: Optional[DiagnosisResult] = None,
        **kwargs: Any
    ) -> ToolResult:
        call_id = kwargs.get("call_id", f"call_giveup_{uuid.uuid4().hex[:8]}")
        reason = kwargs.get("reason", "Stopping rule triggered: recovery attempts halted.")

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            success=False,
            status="abandoned",
            result_data={
                "stopping_rule_reason": reason,
                "amount": txn.amount,
                "retry_count": txn.retry_count
            },
            error_reason=reason,
            recovered_amount=0.0
        )
