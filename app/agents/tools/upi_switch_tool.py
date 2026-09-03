import uuid
from typing import Dict, Any, Optional
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult, RootCause, DiagnosisSource
from app.schemas.agent_state import AgentToolName, ToolResult
from app.agents.tools.base_tool import BaseTool
from app.executor.nudge_generator import nudge_generator
from app.executor.razorpay_simulator import razorpay_simulator

class UPISwitchTool(BaseTool):
    """
    Tool: upi_switch
    Thin adapter offering dynamic alternate payment method (e.g., instant UPI)
    when card has expired or instrument is permanently degraded.
    """
    name = AgentToolName.UPI_SWITCH
    description = (
        "Prompts the customer to seamlessly switch payment methods from a failed/expired instrument "
        "to instant UPI or Netbanking via Razorpay Custom Checkout."
    )

    def execute(
        self,
        txn: Transaction,
        diagnosis: Optional[DiagnosisResult] = None,
        **kwargs: Any
    ) -> ToolResult:
        call_id = kwargs.get("call_id", f"call_upi_{uuid.uuid4().hex[:8]}")
        target_method = kwargs.get("target_method", "upi")
        
        diag = diagnosis or DiagnosisResult(
            transaction_id=txn.transaction_id,
            root_cause=RootCause.CARD_EXPIRED,
            confidence=0.95,
            reasoning="Instrument invalid/expired; alternative payment method offered.",
            source=DiagnosisSource.RULES
        )

        nudge_message = nudge_generator.generate_message(txn, diag)
        converted, notes = razorpay_simulator.simulate_alt_method_conversion(txn, target_method=target_method)
        
        status = "recovered" if converted else "alt_method_abandoned"
        err = None if converted else f"Customer offered {target_method.upper()} payment link but abandoned session."

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            success=converted,
            status=status,
            result_data={
                "target_method": target_method,
                "nudge_message": nudge_message,
                "execution_notes": notes,
                "amount": txn.amount
            },
            error_reason=err,
            recovered_amount=txn.amount if converted else 0.0
        )
