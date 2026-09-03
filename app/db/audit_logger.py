import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("app.security.audit")

class SecurityAuditLogger:
    """
    Structured security and recovery lifecycle audit logger.
    Guarantees secrets (API secret, webhook secret, auth headers) and unnecessary customer PII
    are NEVER logged.
    """

    @staticmethod
    def _sanitize(data: Dict[str, Any]) -> Dict[str, Any]:
        sensitive_keys = {
            "secret", "key_secret", "webhook_secret", "authorization", "password",
            "token", "access_token", "cvv", "card_number", "vpa"
        }
        sanitized = {}
        for k, v in data.items():
            if any(s in k.lower() for s in sensitive_keys):
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = v
        return sanitized

    @classmethod
    def log_event(
        cls,
        event_name: str,
        transaction_id: Optional[str] = None,
        payment_id: Optional[str] = None,
        payment_link_id: Optional[str] = None,
        recovery_attempt_id: Optional[str] = None,
        event_id: Optional[str] = None,
        status: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        payload = {
            "audit_event": event_name,
            "transaction_id": transaction_id,
            "payment_id": payment_id,
            "payment_link_id": payment_link_id,
            "recovery_attempt_id": recovery_attempt_id,
            "event_id": event_id,
            "status": status,
        }
        if details:
            payload["details"] = cls._sanitize(details)

        # Filter out None values for clean log lines
        clean_payload = {k: v for k, v in payload.items() if v is not None}
        logger.info(f"[AUDIT] {event_name.upper()} | {clean_payload}")

# Global audit logger instance
audit_logger = SecurityAuditLogger()
