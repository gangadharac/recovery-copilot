import logging
from datetime import datetime, timezone
from typing import List, Optional
from app.config import settings
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult, RootCause
from app.schemas.strategy import RecoveryAction, GuardrailCheck, StrategyDecision

logger = logging.getLogger(__name__)

class StrategyAgent:
    """
    Stage 3 Strategy Agent:
    1. Recommends bounded recovery actions from a closed action set based on root cause.
    2. Strictly enforces 4 hard-coded Python guardrails (Retry Limit, Cooldown, Consent, Risk Safety).
    3. Populates explicit human-readable reasons for give_up decisions.
    """
    def __init__(self):
        self.max_retries = settings.MAX_RETRY_LIMIT
        self.cooldown_minutes = settings.MIN_COOLDOWN_MINUTES

    def decide_action(self, txn: Transaction, diagnosis: DiagnosisResult) -> StrategyDecision:
        """
        Determines the appropriate recovery strategy for a transaction and enforces hard guardrails.
        """
        root_cause = diagnosis.root_cause
        
        # Step 1: Baseline Strategy Mapping (Unbounded proposal)
        initial_action, base_reasoning = self._map_base_strategy(txn, root_cause)
        
        # Step 2: Apply Hardcoded Python Guardrails
        effective_action = initial_action
        reasoning = base_reasoning
        guardrail_checks: List[GuardrailCheck] = []
        guardrail_overridden = False
        give_up_reason: Optional[str] = None
        retry_scheduled_minutes: Optional[int] = None
        alt_method: Optional[str] = None

        # --- Guardrail 1: Strict Risk Isolation ---
        if root_cause == RootCause.RISK_BLOCKED:
            if effective_action != RecoveryAction.ESCALATE_HUMAN:
                guardrail_overridden = True
                effective_action = RecoveryAction.ESCALATE_HUMAN
                reasoning = "GUARDRAIL ENFORCED: Risk-blocked transaction escalated to human fraud ops. Automated recovery strictly prohibited."
            guardrail_checks.append(GuardrailCheck(
                rule_name="RISK_SAFETY_ISOLATION",
                passed=True,
                description="Transaction flagged with risk_blocked routed exclusively to human ops."
            ))
            return StrategyDecision(
                transaction_id=txn.transaction_id,
                action=effective_action,
                original_action=initial_action,
                reasoning=reasoning,
                guardrail_checks=guardrail_checks,
                guardrail_overridden=guardrail_overridden,
                give_up_reason=None
            )
        else:
            guardrail_checks.append(GuardrailCheck(
                rule_name="RISK_SAFETY_ISOLATION",
                passed=True,
                description="No fraud/risk block detected."
            ))

        # --- Guardrail 2: Hard Retry Cap (Max 3 attempts) ---
        if txn.retry_count >= self.max_retries:
            guardrail_overridden = True
            if txn.amount >= 15000.0:
                effective_action = RecoveryAction.ESCALATE_HUMAN
                reasoning = f"GUARDRAIL ENFORCED: High-value transaction (INR {txn.amount}) hit max retry cap ({txn.retry_count}/{self.max_retries}). Escalated to VIP Concierge Ops."
            else:
                effective_action = RecoveryAction.GIVE_UP
                give_up_reason = f"Max retry limit reached ({txn.retry_count}/{self.max_retries} attempts exhausted without recovery)."
                reasoning = f"GUARDRAIL ENFORCED: Stopping rule triggered. {give_up_reason}"
                
            guardrail_checks.append(GuardrailCheck(
                rule_name="MAX_RETRY_CAP",
                passed=False,
                description=f"Transaction exceeded maximum allowed retries ({txn.retry_count} >= {self.max_retries})."
            ))
            return StrategyDecision(
                transaction_id=txn.transaction_id,
                action=effective_action,
                original_action=initial_action,
                reasoning=reasoning,
                guardrail_checks=guardrail_checks,
                guardrail_overridden=guardrail_overridden,
                give_up_reason=give_up_reason
            )
        else:
            guardrail_checks.append(GuardrailCheck(
                rule_name="MAX_RETRY_CAP",
                passed=True,
                description=f"Retry count within allowed limit ({txn.retry_count}/{self.max_retries})."
            ))

        # --- Guardrail 3: Auto-Charge Consent Enforcement ---
        if effective_action == RecoveryAction.RETRY_NOW:
            if not txn.auto_charge_consent:
                guardrail_overridden = True
                effective_action = RecoveryAction.NUDGE_CUSTOMER
                reasoning = "GUARDRAIL ENFORCED: Silent auto-retry prohibited without explicit customer auto-charge consent. Re-routed to interactive customer nudge."
                guardrail_checks.append(GuardrailCheck(
                    rule_name="CONSENT_MANDATE_ENFORCEMENT",
                    passed=False,
                    description="auto_charge_consent is False; silent debit blocked."
                ))
            else:
                guardrail_checks.append(GuardrailCheck(
                    rule_name="CONSENT_MANDATE_ENFORCEMENT",
                    passed=True,
                    description="auto_charge_consent verified. Automated retry permitted."
                ))
        else:
            guardrail_checks.append(GuardrailCheck(
                rule_name="CONSENT_MANDATE_ENFORCEMENT",
                passed=True,
                description="Action does not attempt silent debit."
            ))

        # --- Guardrail 4: Cooldown Window Check (Min 30 minutes) ---
        if effective_action == RecoveryAction.RETRY_NOW and txn.last_retry_timestamp is not None:
            now_ts = txn.timestamp
            last_ts = txn.last_retry_timestamp
            
            # Make sure timestamps are tz-aware or tz-naive consistently
            if now_ts.tzinfo is not None and last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            elif now_ts.tzinfo is None and last_ts.tzinfo is not None:
                now_ts = now_ts.replace(tzinfo=timezone.utc)
                
            elapsed_mins = (now_ts - last_ts).total_seconds() / 60.0
            if elapsed_mins < self.cooldown_minutes:
                guardrail_overridden = True
                effective_action = RecoveryAction.RETRY_LATER
                wait_mins = int(self.cooldown_minutes - elapsed_mins) + 1
                retry_scheduled_minutes = wait_mins
                reasoning = f"GUARDRAIL ENFORCED: Cooldown violation ({int(elapsed_mins)}m since last attempt < {self.cooldown_minutes}m required). Rescheduled for {wait_mins}m later."
                guardrail_checks.append(GuardrailCheck(
                    rule_name="COOLDOWN_WINDOW_ENFORCEMENT",
                    passed=False,
                    description=f"Elapsed time {int(elapsed_mins)}m is less than cooldown {self.cooldown_minutes}m."
                ))
            else:
                guardrail_checks.append(GuardrailCheck(
                    rule_name="COOLDOWN_WINDOW_ENFORCEMENT",
                    passed=True,
                    description=f"Cooldown satisfied ({int(elapsed_mins)}m elapsed >= {self.cooldown_minutes}m)."
                ))
        else:
            guardrail_checks.append(GuardrailCheck(
                rule_name="COOLDOWN_WINDOW_ENFORCEMENT",
                passed=True,
                description="Cooldown rule satisfied or initial attempt."
            ))

        # Additional Context for Specific Actions
        if effective_action == RecoveryAction.OFFER_ALT_METHOD:
            alt_method = "upi" if txn.payment_method.value != "upi" else "netbanking"

        if effective_action == RecoveryAction.GIVE_UP and not give_up_reason:
            give_up_reason = "Unresolvable decline reason with no actionable recovery pathway."

        return StrategyDecision(
            transaction_id=txn.transaction_id,
            action=effective_action,
            original_action=initial_action,
            reasoning=reasoning,
            guardrail_checks=guardrail_checks,
            guardrail_overridden=guardrail_overridden,
            give_up_reason=give_up_reason,
            retry_scheduled_minutes=retry_scheduled_minutes,
            target_alternate_method=alt_method
        )

    def _map_base_strategy(self, txn: Transaction, root_cause: RootCause) -> tuple[RecoveryAction, str]:
        """
        Maps root causes to optimal baseline actions before guardrail checks.
        """
        if root_cause == RootCause.BANK_TIMEOUT:
            return (
                RecoveryAction.RETRY_NOW,
                "Bank switch timeout: Route transaction through backup gateway switch."
            )
        elif root_cause == RootCause.NETWORK_GLITCH:
            return (
                RecoveryAction.RETRY_NOW,
                "Transient network glitch: Re-initiate payment verification on active channel."
            )
        elif root_cause == RootCause.WRONG_OTP:
            return (
                RecoveryAction.NUDGE_CUSTOMER,
                "Authentication timeout/wrong OTP: Send WhatsApp nudge with 1-click re-auth link."
            )
        elif root_cause == RootCause.INSUFFICIENT_FUNDS:
            return (
                RecoveryAction.NUDGE_CUSTOMER,
                "Insufficient funds / limit: Nudge customer to approve with alternate account or UPI."
            )
        elif root_cause == RootCause.CARD_EXPIRED:
            return (
                RecoveryAction.OFFER_ALT_METHOD,
                "Card expired: Present instant UPI / Netbanking payment link to replace expired card."
            )
        elif root_cause == RootCause.RISK_BLOCKED:
            return (
                RecoveryAction.ESCALATE_HUMAN,
                "Risk/fraud block: Escalate directly to risk operations team for verification."
            )
        else: # UNKNOWN
            return (
                RecoveryAction.NUDGE_CUSTOMER,
                "Ambiguous failure: Dispatch universal recovery link to prompt user retry."
            )

# Global Strategy Agent singleton
strategy_agent = StrategyAgent()
