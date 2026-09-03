from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field

class RecoveryAction(str, Enum):
    RETRY_NOW = "retry_now"
    RETRY_LATER = "retry_later"
    NUDGE_CUSTOMER = "nudge_customer"
    OFFER_ALT_METHOD = "offer_alt_method"
    ESCALATE_HUMAN = "escalate_human"
    GIVE_UP = "give_up"

class GuardrailCheck(BaseModel):
    rule_name: str
    passed: bool
    description: str

class StrategyDecision(BaseModel):
    transaction_id: str
    action: RecoveryAction
    original_action: RecoveryAction
    reasoning: str
    guardrail_checks: List[GuardrailCheck] = Field(default_factory=list)
    guardrail_overridden: bool = False
    give_up_reason: Optional[str] = None  # Explicit human-readable reason for exception list
    retry_scheduled_minutes: Optional[int] = None
    target_alternate_method: Optional[str] = None
