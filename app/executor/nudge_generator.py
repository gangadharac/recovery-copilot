import logging
from typing import Dict, Any
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult
from app.agents.llm_client import llm_client

logger = logging.getLogger(__name__)

class NudgeGenerator:
    """
    Generates tailored, high-converting Hinglish WhatsApp/SMS recovery messages
    using Claude (with few-shot prompts and tone matching).
    """
    def generate_message(self, txn: Transaction, diagnosis: DiagnosisResult) -> str:
        """
        Generates the personalized WhatsApp recovery message.
        """
        bank_name = txn.payment_method_details.bank_code or "Bank"
        
        nudge_context = {
            "transaction_id": txn.transaction_id,
            "customer_name": txn.customer_name,
            "amount": f"{txn.amount:,.2f}",
            "root_cause": diagnosis.root_cause.value,
            "bank_code": bank_name,
            "payment_method": txn.payment_method.value,
        }
        
        message = llm_client.generate_hinglish_nudge(nudge_context)
        return message

# Global Nudge Generator singleton
nudge_generator = NudgeGenerator()
