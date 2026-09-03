import uuid
from typing import Dict, Any, Optional
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult, RootCause
from app.schemas.agent_state import AgentToolName, ToolResult
from app.agents.tools.base_tool import BaseTool
from app.executor.razorpay_simulator import razorpay_simulator

class SwitchRoutingTool(BaseTool):
    """
    Tool: switch_routing
    Thin adapter wrapping the payment gateway switch rerouting simulation.
    Attempts immediate capture through an alternate gateway switch.
    """
    name = AgentToolName.SWITCH_ROUTING
    description = (
        "Reroutes a failed transaction through an alternate healthy banking gateway switch "
        "(e.g., switch_hdfc_direct_v2, switch_icici_mesh_v3) to bypass degraded bank nodes."
    )

    def execute(
        self,
        txn: Transaction,
        diagnosis: Optional[DiagnosisResult] = None,
        **kwargs: Any
    ) -> ToolResult:
        call_id = kwargs.get("call_id", f"call_switch_{uuid.uuid4().hex[:8]}")
        root_cause = diagnosis.root_cause if diagnosis else RootCause.BANK_TIMEOUT

        success, switch_used, notes = razorpay_simulator.simulate_retry(txn, root_cause)
        status = "recovered" if success else "failed_retry"
        err = None if success else f"Alternate switch {switch_used} also reported decline."

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            success=success,
            status=status,
            result_data={
                "gateway_switch_used": switch_used,
                "execution_notes": notes,
                "amount": txn.amount
            },
            error_reason=err,
            recovered_amount=txn.amount if success else 0.0
        )
