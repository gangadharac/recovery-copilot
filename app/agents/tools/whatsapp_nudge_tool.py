import uuid
from typing import Dict, Any, Optional
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult, RootCause, DiagnosisSource
from app.schemas.agent_state import AgentToolName, ToolResult
from app.agents.tools.base_tool import BaseTool
from app.executor.nudge_generator import nudge_generator
from app.executor.razorpay_simulator import razorpay_simulator

class WhatsAppNudgeTool(BaseTool):
    """
    Tool: whatsapp_nudge
    Thin adapter wrapping Hinglish WhatsApp nudge generation & customer conversion simulation.
    """
    name = AgentToolName.WHATSAPP_NUDGE
    description = (
        "Dispatches a contextual, few-shot prompted Hinglish WhatsApp recovery message to the customer "
        "with an instant 1-click retry link (https://rzp.io/i/...) to re-authorize payment."
    )

    def execute(
        self,
        txn: Transaction,
        diagnosis: Optional[DiagnosisResult] = None,
        **kwargs: Any
    ) -> ToolResult:
        call_id = kwargs.get("call_id", f"call_nudge_{uuid.uuid4().hex[:8]}")
        
        diag = diagnosis or DiagnosisResult(
            transaction_id=txn.transaction_id,
            root_cause=RootCause.WRONG_OTP,
            confidence=0.90,
            reasoning="Customer interaction requested via agent tool",
            source=DiagnosisSource.RULES
        )

        nudge_message = nudge_generator.generate_message(txn, diag)
        converted, notes = razorpay_simulator.simulate_nudge_conversion(txn, diag.root_cause)
        
        status = "recovered" if converted else "nudged_unconverted"
        err = None if converted else "Customer received WhatsApp nudge but did not complete payment."

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            success=converted,
            status=status,
            result_data={
                "channel": "whatsapp",
                "nudge_message": nudge_message,
                "execution_notes": notes,
                "amount": txn.amount
            },
            error_reason=err,
            recovered_amount=txn.amount if converted else 0.0
        )
