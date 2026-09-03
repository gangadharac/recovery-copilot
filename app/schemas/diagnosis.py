from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field

class RootCause(str, Enum):
    BANK_TIMEOUT = "bank_timeout"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    WRONG_OTP = "wrong_otp"
    CARD_EXPIRED = "card_expired"
    RISK_BLOCKED = "risk_blocked"
    NETWORK_GLITCH = "network_glitch"
    UNKNOWN = "unknown"

class DiagnosisSource(str, Enum):
    RULES = "rules"
    LLM = "llm"

class DiagnosisResult(BaseModel):
    transaction_id: str
    root_cause: RootCause
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    source: DiagnosisSource
    diagnostic_factors: List[str] = Field(default_factory=list)
