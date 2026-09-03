import logging
from enum import Enum
from datetime import datetime, timezone
from typing import Optional, Set, Dict

logger = logging.getLogger(__name__)

class RecoveryStatus(str, Enum):
    """
    Standardized lifecycle states for a Razorpay Test Mode Payment Link Recovery Attempt.
    created -> pending -> paid_verification_pending -> recovered
    """
    CREATED = "created"
    PENDING = "pending"
    PAID_VERIFICATION_PENDING = "paid_verification_pending"
    RECOVERED = "recovered"
    VERIFICATION_FAILED = "verification_failed"
    FAILED = "failed"
    EXPIRED = "expired"

ALLOWED_TRANSITIONS: Dict[str, Set[str]] = {
    RecoveryStatus.CREATED.value: {
        RecoveryStatus.PENDING.value,
        RecoveryStatus.FAILED.value
    },
    RecoveryStatus.PENDING.value: {
        RecoveryStatus.PAID_VERIFICATION_PENDING.value,
        RecoveryStatus.RECOVERED.value, # When atomic verification succeeds directly
        RecoveryStatus.VERIFICATION_FAILED.value,
        RecoveryStatus.FAILED.value,
        RecoveryStatus.EXPIRED.value
    },
    RecoveryStatus.PAID_VERIFICATION_PENDING.value: {
        RecoveryStatus.RECOVERED.value,
        RecoveryStatus.VERIFICATION_FAILED.value
    },
    # Terminal states cannot transition further
    RecoveryStatus.RECOVERED.value: set(),
    RecoveryStatus.VERIFICATION_FAILED.value: set(),
    RecoveryStatus.FAILED.value: set(),
    RecoveryStatus.EXPIRED.value: set()
}

def can_transition(current_status: str, target_status: str) -> bool:
    """
    Validates whether transitioning from current_status to target_status is permitted by the state machine.
    """
    curr = current_status.lower() if current_status else RecoveryStatus.CREATED.value
    tgt = target_status.lower() if target_status else ""
    
    # Allow idempotent transition to the same state
    if curr == tgt:
        return True

    allowed_next = ALLOWED_TRANSITIONS.get(curr, set())
    return tgt in allowed_next

def transition_recovery_attempt(
    attempt,
    target_status: str,
    reason: Optional[str] = None
) -> bool:
    """
    Applies a state transition to a RecoveryAttemptModel instance.
    Enforces the centralized state machine: rejects invalid transitions and updates audit metadata.
    """
    curr = attempt.status.lower() if attempt.status else RecoveryStatus.CREATED.value
    tgt = target_status.lower()

    if not can_transition(curr, tgt):
        logger.warning(
            f"[STATE_MACHINE_BLOCKED] Invalid recovery attempt transition from '{curr}' to '{tgt}' "
            f"for attempt {attempt.id} (txn {attempt.transaction_id})."
        )
        return False

    attempt.status = tgt
    attempt.updated_at = datetime.now(timezone.utc)
    if reason:
        attempt.error_message = reason

    logger.info(
        f"[STATE_MACHINE] Attempt {attempt.id} (txn {attempt.transaction_id}): "
        f"{curr} -> {tgt} ({reason or 'Compliant transition'})"
    )
    return True
