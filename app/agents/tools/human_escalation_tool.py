import uuid
from typing import Dict, Any, Optional
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult
from app.schemas.agent_state import AgentToolName, ToolResult
from app.agents.tools.base_tool import BaseTool

class HumanEscalationTool(BaseTool):
    """
    Tool: human_escalation
    Thin adapter for quarantining high-risk, fraud-blocked, or VIP transactions
    and dispatching an escalation ticket to Human Operations.
    """
    name = AgentToolName.HUMAN_ESCALATION
    description = (
        "Quarantines a high-risk or high-value transaction from automated chasing and escalates "
        "to the Human Fraud / VIP Concierge Operations queue with full audit context."
    )

    def execute(
        self,
        txn: Transaction,
        diagnosis: Optional[DiagnosisResult] = None,
        **kwargs: Any
    ) -> ToolResult:
        call_id = kwargs.get("call_id", f"call_esc_{uuid.uuid4().hex[:8]}")
        reason = kwargs.get("reason", "Escalated for human operations review.")

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            success=False,
            status="escalated_to_ops",
            result_data={
                "ticket_queue": "Fraud_and_VIP_Ops",
                "escalation_reason": reason,
                "amount": txn.amount,
                "customer_id": txn.customer_id
            },
            error_reason=reason,
            recovered_amount=0.0
        )
