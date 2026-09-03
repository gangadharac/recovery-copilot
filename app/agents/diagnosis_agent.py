import logging
from typing import Dict, Any, Tuple, Optional
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import RootCause, DiagnosisSource, DiagnosisResult
from app.agents.llm_client import llm_client

logger = logging.getLogger(__name__)

# Rule Mapping for Deterministic Fast Pass
RULE_MAP: Dict[str, Tuple[RootCause, float, str]] = {
    # Bank Timeout / Switch Errors
    "BANK_TIMEOUT": (
        RootCause.BANK_TIMEOUT,
        0.98,
        "Direct match on BANK_TIMEOUT error code: Bank switch timed out during authorization."
    ),
    "ISSUER_SWITCH_UNAVAILABLE": (
        RootCause.BANK_TIMEOUT,
        0.96,
        "Issuer switch temporarily unreachable or in maintenance."
    ),
    
    # Insufficient Funds
    "insufficient_funds": (
        RootCause.INSUFFICIENT_FUNDS,
        0.99,
        "Direct match on insufficient_funds error code: Customer account balance or credit limit exceeded."
    ),
    "INSUFFICIENT_BALANCE": (
        RootCause.INSUFFICIENT_FUNDS,
        0.99,
        "Direct match on INSUFFICIENT_BALANCE: Account has inadequate funds for the transaction."
    ),
    "LIMIT_EXCEEDED": (
        RootCause.INSUFFICIENT_FUNDS,
        0.95,
        "Card/Account velocity or amount limit exceeded."
    ),
    
    # OTP / Authentication Failure
    "otp_timeout": (
        RootCause.WRONG_OTP,
        0.98,
        "Direct match on otp_timeout: Customer did not submit OTP in time."
    ),
    "OTP_EXPIRED": (
        RootCause.WRONG_OTP,
        0.98,
        "Direct match on OTP_EXPIRED: 3D Secure OTP validity expired before submission."
    ),
    "AUTH_FAILED": (
        RootCause.WRONG_OTP,
        0.97,
        "Direct match on AUTH_FAILED: Customer failed two-factor authentication challenge."
    ),
    "3DS_VERIFICATION_FAILED": (
        RootCause.WRONG_OTP,
        0.97,
        "3D Secure verification rejected by issuer authentication server."
    ),
    
    # Expired Instrument
    "card_expired": (
        RootCause.CARD_EXPIRED,
        0.99,
        "Direct match on card_expired: Card expiration month/year is in the past."
    ),
    "EXPIRED_CARD": (
        RootCause.CARD_EXPIRED,
        0.99,
        "Direct match on EXPIRED_CARD: Issuer rejected expired card payment."
    ),
    "INVALID_EXPIRY_DATE": (
        RootCause.CARD_EXPIRED,
        0.97,
        "Card expiry date format is invalid or expired."
    ),
    
    # Risk / Fraud
    "risk_blocked": (
        RootCause.RISK_BLOCKED,
        0.99,
        "Direct match on risk_blocked: Transaction blocked by Razorpay Shield / risk engine."
    ),
    "FRAUD_SUSPECTED": (
        RootCause.RISK_BLOCKED,
        0.98,
        "Fraud scoring threshold breached on transaction metadata."
    ),
    "VELOCITY_EXCEEDED": (
        RootCause.RISK_BLOCKED,
        0.95,
        "Velocity limit breached for card / IP fingerprint."
    ),
    "BLACKLISTED_CARD": (
        RootCause.RISK_BLOCKED,
        0.99,
        "Instrument is listed on the global blacklist / chargeback register."
    ),
    
    # Network Errors
    "network_error": (
        RootCause.NETWORK_GLITCH,
        0.95,
        "Direct match on network_error: Network transport dropped during transaction."
    ),
    "CONNECTION_DROPPED": (
        RootCause.NETWORK_GLITCH,
        0.95,
        "Socket connection closed unexpectedly by peer before response received."
    ),
    "CLIENT_DISCONNECTED": (
        RootCause.NETWORK_GLITCH,
        0.94,
        "Client browser/app dropped connection during 3DS redirection."
    )
}

class DiagnosisAgent:
    """
    Stage 2 Diagnosis Agent:
    1. Deterministic Fast-Pass via Error Code Rules.
    2. Fallback to Claude LLM for ambiguous / unclassified errors.
    """
    def __init__(self):
        self.rules = RULE_MAP

    def diagnose(self, txn: Transaction) -> DiagnosisResult:
        """
        Diagnoses the root cause of a failed transaction.
        """
        error_code = txn.error_code.strip()
        
        # 1. Fast-Pass: Check rule map
        if error_code in self.rules:
            root_cause, confidence, reasoning = self.rules[error_code]
            return DiagnosisResult(
                transaction_id=txn.transaction_id,
                root_cause=root_cause,
                confidence=confidence,
                reasoning=reasoning,
                source=DiagnosisSource.RULES,
                diagnostic_factors=[f"Rule matched error_code='{error_code}'", f"Source: {txn.error_source}"]
            )
            
        # 2. Rule Check based on error_source
        if txn.error_source == "risk_engine":
            return DiagnosisResult(
                transaction_id=txn.transaction_id,
                root_cause=RootCause.RISK_BLOCKED,
                confidence=0.96,
                reasoning="Classified via error_source='risk_engine'. Strict risk rule applied.",
                source=DiagnosisSource.RULES,
                diagnostic_factors=["error_source: risk_engine"]
            )
            
        # 3. Ambiguous / Generic Cases -> LLM Fallback (Claude)
        logger.info(f"Invoking LLM Diagnosis for ambiguous transaction {txn.transaction_id} (code: {error_code})")
        context = {
            "transaction_id": txn.transaction_id,
            "amount": txn.amount,
            "payment_method": txn.payment_method.value,
            "payment_method_details": txn.payment_method_details.model_dump(),
            "error_code": txn.error_code,
            "error_description": txn.error_description,
            "error_source": txn.error_source,
            "customer_history": txn.customer_history.model_dump(),
            "raw_gateway_response": txn.raw_gateway_response.model_dump() if txn.raw_gateway_response else {}
        }
        
        llm_output = llm_client.diagnose_ambiguous_failure(context)
        
        # Validate root_cause returned by LLM belongs to closed set
        raw_rc = llm_output.get("root_cause", "unknown")
        try:
            root_cause = RootCause(raw_rc)
        except ValueError:
            root_cause = RootCause.UNKNOWN
            
        return DiagnosisResult(
            transaction_id=txn.transaction_id,
            root_cause=root_cause,
            confidence=float(llm_output.get("confidence", 0.75)),
            reasoning=llm_output.get("reasoning", "Diagnosed via LLM reasoning on transaction context."),
            source=DiagnosisSource.LLM,
            diagnostic_factors=llm_output.get("diagnostic_factors", ["LLM contextual synthesis"])
        )

# Global Diagnosis Agent singleton
diagnosis_agent = DiagnosisAgent()
