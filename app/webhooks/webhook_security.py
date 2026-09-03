import hmac
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)

def verify_razorpay_signature(
    raw_body: bytes,
    signature: Optional[str],
    secret: Optional[str]
) -> bool:
    """
    Validates the Razorpay webhook signature using HMAC-SHA256 against the raw request body.
    Zero secrets are logged or exposed.
    """
    if not signature or not secret:
        logger.warning("Webhook signature verification failed: Missing signature or secret.")
        return False

    try:
        expected_signature = hmac.new(
            key=secret.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256
        ).hexdigest()

        is_valid = hmac.compare_digest(
            expected_signature.lower(),
            signature.strip().lower()
        )
        if not is_valid:
            logger.warning("Webhook signature verification failed: Signature mismatch.")
        return is_valid
    except Exception as e:
        logger.error(f"Error during webhook signature calculation: {e}")
        return False

def compute_payload_hash(raw_body: bytes) -> str:
    """
    Calculates SHA-256 hash of the raw payload bytes for idempotency and integrity auditing.
    """
    return hashlib.sha256(raw_body).hexdigest()

def generate_test_signature(raw_body: bytes, secret: str) -> str:
    """
    Test helper: Generates a valid HMAC-SHA256 signature for automated tests.
    """
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256
    ).hexdigest()
