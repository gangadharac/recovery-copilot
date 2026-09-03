import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from app.config import settings
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult, RootCause
from app.schemas.agent_state import AgentToolName

logger = logging.getLogger(__name__)

class GuardrailInterceptionResult(BaseModel):
    """
    Structured result returned by the GuardrailInterceptor before any tool executes.
    Explains whether the tool invocation is permitted or transformed for compliance.
    """
    allowed: bool
    rule_triggered: Optional[str] = None
    original_requested_tool: AgentToolName
    final_permitted_tool: AgentToolName
    reason: str
    guardrail_checks: List[Dict[str, Any]] = Field(default_factory=list)

class GuardrailInterceptor:
    """
    Pre-execution Safety Layer for the Autonomous Agent.
    Strictly intercepts and programmatically validates every tool call against the 4 non-negotiable
    Python guardrails:
      1. Maximum Retry Cap (MAX_RETRY_LIMIT = 3)
      2. 30-Minute Banking Cooldown (MIN_COOLDOWN_MINUTES = 30)
      3. Auto-Charge Consent Mandate
      4. Strict Risk Safety & Fraud Isolation
    """
    def __init__(self):
        self.max_retries = settings.MAX_RETRY_LIMIT
        self.cooldown_minutes = settings.MIN_COOLDOWN_MINUTES

    def intercept(
        self,
        requested_tool: AgentToolName,
        txn: Transaction,
        diagnosis: Optional[DiagnosisResult] = None
    ) -> GuardrailInterceptionResult:
        """
        Validates the requested tool before execution.
        Returns a structured GuardrailInterceptionResult indicating permission or transformed action.
        """
        root_cause = diagnosis.root_cause if diagnosis else RootCause.UNKNOWN
        guardrail_checks: List[Dict[str, Any]] = []

        # =========================================================================
        # 1. RISK SAFETY ISOLATION GUARDRAIL
        # =========================================================================
        is_risk_flagged = (
            root_cause == RootCause.RISK_BLOCKED or
            txn.error_source == "risk_engine" or
            txn.error_code.strip() in ["risk_blocked", "FRAUD_SUSPECTED", "VELOCITY_EXCEEDED", "BLACKLISTED_CARD"]
        )

        if is_risk_flagged:
            guardrail_checks.append({
                "rule_name": "RISK_SAFETY_ISOLATION",
                "passed": (requested_tool == AgentToolName.HUMAN_ESCALATION),
                "description": "Transaction flagged with risk/fraud block. Quarantined to human ops."
            })

            if requested_tool != AgentToolName.HUMAN_ESCALATION:
                return GuardrailInterceptionResult(
                    allowed=False,
                    rule_triggered="RISK_SAFETY_ISOLATION",
                    original_requested_tool=requested_tool,
                    final_permitted_tool=AgentToolName.HUMAN_ESCALATION,
                    reason="GUARDRAIL ENFORCED: Risk-blocked transaction quarantined to human ops. Automated recovery or nudging is strictly prohibited.",
                    guardrail_checks=guardrail_checks
                )
            else:
                return GuardrailInterceptionResult(
                    allowed=True,
                    rule_triggered=None,
                    original_requested_tool=requested_tool,
                    final_permitted_tool=AgentToolName.HUMAN_ESCALATION,
                    reason="Transaction correctly routed to human ops queue for risk review.",
                    guardrail_checks=guardrail_checks
                )

        guardrail_checks.append({
            "rule_name": "RISK_SAFETY_ISOLATION",
            "passed": True,
            "description": "No fraud/risk block detected."
        })

        # =========================================================================
        # 2. MAX RETRY CAP GUARDRAIL (Max 3 attempts)
        # =========================================================================
        if txn.retry_count >= self.max_retries:
            guardrail_checks.append({
                "rule_name": "MAX_RETRY_CAP",
                "passed": False,
                "description": f"Retry limit reached ({txn.retry_count} >= {self.max_retries})."
            })

            if txn.amount >= 15000.0:
                final_tool = AgentToolName.HUMAN_ESCALATION
                reason = f"GUARDRAIL ENFORCED: High-value transaction (INR {txn.amount:,.2f}) reached retry limit ({txn.retry_count}/{self.max_retries}). Escalated to VIP Concierge Ops."
            else:
                final_tool = AgentToolName.GIVE_UP
                reason = f"GUARDRAIL ENFORCED: Stopping rule triggered. Max retry limit reached ({txn.retry_count}/{self.max_retries} attempts exhausted without recovery)."

            return GuardrailInterceptionResult(
                allowed=False,
                rule_triggered="MAX_RETRY_CAP",
                original_requested_tool=requested_tool,
                final_permitted_tool=final_tool,
                reason=reason,
                guardrail_checks=guardrail_checks
            )

        guardrail_checks.append({
            "rule_name": "MAX_RETRY_CAP",
            "passed": True,
            "description": f"Retry count within allowed limit ({txn.retry_count}/{self.max_retries})."
        })

        # =========================================================================
        # 3. AUTO-CHARGE CONSENT MANDATE GUARDRAIL
        # =========================================================================
        if requested_tool == AgentToolName.SWITCH_ROUTING:
            if not txn.auto_charge_consent:
                guardrail_checks.append({
                    "rule_name": "CONSENT_MANDATE_ENFORCEMENT",
                    "passed": False,
                    "description": "auto_charge_consent is False; silent debit blocked."
                })
                return GuardrailInterceptionResult(
                    allowed=False,
                    rule_triggered="CONSENT_MANDATE_ENFORCEMENT",
                    original_requested_tool=requested_tool,
                    final_permitted_tool=AgentToolName.WHATSAPP_NUDGE,
                    reason="GUARDRAIL ENFORCED: Silent auto-retry prohibited without explicit customer auto-charge consent. Transformed to interactive WhatsApp customer nudge.",
                    guardrail_checks=guardrail_checks
                )
            else:
                guardrail_checks.append({
                    "rule_name": "CONSENT_MANDATE_ENFORCEMENT",
                    "passed": True,
                    "description": "auto_charge_consent verified. Silent retry permitted."
                })
        else:
            guardrail_checks.append({
                "rule_name": "CONSENT_MANDATE_ENFORCEMENT",
                "passed": True,
                "description": "Tool does not attempt silent auto-debit."
            })

        # =========================================================================
        # 4. COOLDOWN WINDOW GUARDRAIL (Min 30 minutes between retries)
        # =========================================================================
        if requested_tool == AgentToolName.SWITCH_ROUTING and txn.last_retry_timestamp is not None:
            now_ts = txn.timestamp
            last_ts = txn.last_retry_timestamp

            if now_ts.tzinfo is not None and last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            elif now_ts.tzinfo is None and last_ts.tzinfo is not None:
                now_ts = now_ts.replace(tzinfo=timezone.utc)

            elapsed_mins = (now_ts - last_ts).total_seconds() / 60.0
            if elapsed_mins < self.cooldown_minutes:
                guardrail_checks.append({
                    "rule_name": "COOLDOWN_WINDOW_ENFORCEMENT",
                    "passed": False,
                    "description": f"Elapsed time {int(elapsed_mins)}m is less than cooldown {self.cooldown_minutes}m."
                })
                wait_mins = int(self.cooldown_minutes - elapsed_mins) + 1
                return GuardrailInterceptionResult(
                    allowed=False,
                    rule_triggered="COOLDOWN_WINDOW_ENFORCEMENT",
                    original_requested_tool=requested_tool,
                    final_permitted_tool=AgentToolName.WHATSAPP_NUDGE,
                    reason=f"GUARDRAIL ENFORCED: Cooldown violation ({int(elapsed_mins)}m since last attempt < {self.cooldown_minutes}m required). Immediate switch retry blocked; transformed to customer nudge.",
                    guardrail_checks=guardrail_checks
                )
            else:
                guardrail_checks.append({
                    "rule_name": "COOLDOWN_WINDOW_ENFORCEMENT",
                    "passed": True,
                    "description": f"Cooldown satisfied ({int(elapsed_mins)}m elapsed >= {self.cooldown_minutes}m)."
                })
        else:
            guardrail_checks.append({
                "rule_name": "COOLDOWN_WINDOW_ENFORCEMENT",
                "passed": True,
                "description": "Cooldown rule satisfied or initial attempt."
            })

        # =========================================================================
        # ALL GUARDRAILS PASSED
        # =========================================================================
        return GuardrailInterceptionResult(
            allowed=True,
            rule_triggered=None,
            original_requested_tool=requested_tool,
            final_permitted_tool=requested_tool,
            reason="All hardcoded safety guardrails verified and passed.",
            guardrail_checks=guardrail_checks
        )

# Global Guardrail Interceptor singleton
guardrail_interceptor = GuardrailInterceptor()
