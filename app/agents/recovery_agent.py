import logging
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from app.config import settings
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult, RootCause
from app.schemas.agent_state import (
    AgentToolName,
    ToolCall,
    ToolResult,
    ThoughtStep,
    AutonomousAgentState,
)
from app.agents.diagnosis_agent import DiagnosisAgent, diagnosis_agent
from app.agents.tools.base_tool import ToolRegistry
from app.agents.tools import default_tool_registry
from app.agents.tools.guardrail_interceptor import GuardrailInterceptor, guardrail_interceptor
from app.agents.verification import Verifier, default_verifier, VerificationStatus

logger = logging.getLogger(__name__)

class RecoveryAgentResult(BaseModel):
    """
    Structured outcome returned by the Autonomous RecoveryAgent.
    Provides complete visibility into the multi-step lifecycle.
    """
    transaction_id: str
    final_status: str                   # "recovered", "escalated", "abandoned", "in_progress"
    final_action: str                   # e.g., "switch_routing", "whatsapp_nudge", "upi_switch", "human_escalation", "give_up"
    recovered_amount: float = Field(default=0.0, ge=0.0)
    iterations_used: int = Field(ge=1)
    diagnosis: Optional[DiagnosisResult] = None
    concise_decision_summary: str
    tool_history: List[ToolResult] = Field(default_factory=list)
    thought_history: List[ThoughtStep] = Field(default_factory=list)
    unresolved_reason: Optional[str] = None
    state: AutonomousAgentState

class RecoveryAgent:
    """
    Autonomous Revenue Recovery Agent.
    Coordinates: OBSERVE → DIAGNOSE → PLAN → GUARDRAIL CHECK → EXECUTE → VERIFY → RE-PLAN → AUDIT.
    """
    def __init__(
        self,
        diag_agent: Optional[DiagnosisAgent] = None,
        tool_reg: Optional[ToolRegistry] = None,
        interceptor: Optional[GuardrailInterceptor] = None,
        verifier: Optional[Verifier] = None,
        max_iterations: int = 3
    ):
        self.diagnosis_agent = diag_agent or diagnosis_agent
        self.tool_registry = tool_reg or default_tool_registry
        self.guardrail_interceptor = interceptor or guardrail_interceptor
        self.verifier = verifier or default_verifier
        self.max_iterations = min(max(max_iterations, 1), 3) # Bound to max 3 iterations

    def recover(
        self,
        txn: Transaction,
        custom_diagnosis: Optional[DiagnosisResult] = None
    ) -> RecoveryAgentResult:
        """
        Executes the autonomous recovery lifecycle for a failed transaction.
        """
        # 1. OBSERVE: Initialize state memory with facts
        obs = (
            f"Failed txn {txn.transaction_id}: INR {txn.amount:,.2f} via {txn.payment_method.value}. "
            f"Error: {txn.error_code} ({txn.error_source}). Past retries: {txn.retry_count}. "
            f"Auto-charge consent: {txn.auto_charge_consent}."
        )
        
        state = AutonomousAgentState(
            transaction_id=txn.transaction_id,
            current_observation=obs,
            max_iterations=self.max_iterations
        )

        # 2. DIAGNOSE: Fast-pass rules or Claude LLM fallback
        diagnosis = custom_diagnosis or self.diagnosis_agent.diagnose(txn)
        state.current_diagnosis = diagnosis.root_cause.value

        unresolved_reason: Optional[str] = None
        final_action_str = "give_up"

        # 3. AGENT STATE LOOP (OBSERVE -> PLAN -> GUARDRAIL -> EXECUTE -> VERIFY -> RE-PLAN)
        while state.iteration_count < state.max_iterations:
            current_iter = state.increment_iteration()
            
            # --- STEP A: PLAN ---
            candidate_tool = self._plan_next_action(txn, diagnosis, state)
            state.current_planned_action = candidate_tool

            # --- STEP B: GUARDRAIL INTERCEPTION ---
            interception = self.guardrail_interceptor.intercept(
                requested_tool=candidate_tool,
                txn=txn,
                diagnosis=diagnosis
            )
            
            executable_tool = interception.final_permitted_tool
            final_action_str = executable_tool.value
            
            # Formulate concise thought summary (no raw CoT)
            if not interception.allowed:
                thought_summary = (
                    f"Iteration {current_iter}: Planned '{candidate_tool.value}', but guardrail '{interception.rule_triggered}' intervened. "
                    f"Transformed action to '{executable_tool.value}'. Reason: {interception.reason}"
                )
            elif current_iter > 1:
                thought_summary = (
                    f"Iteration {current_iter}: Previous tool failed; re-planned to '{executable_tool.value}' based on failure feedback."
                )
            else:
                thought_summary = (
                    f"Iteration {current_iter}: Diagnosed root cause '{diagnosis.root_cause.value}'. "
                    f"Selected initial recovery tool '{executable_tool.value}' (confidence: {diagnosis.confidence:.2f})."
                )

            # --- STEP C: EXECUTE ---
            tool_call = ToolCall(
                tool_name=executable_tool,
                input_arguments={"iteration": current_iter, "amount": txn.amount}
            )
            
            try:
                tool = self.tool_registry.get(executable_tool)
                tool_result = tool.execute(
                    txn=txn,
                    diagnosis=diagnosis,
                    call_id=tool_call.call_id,
                    reason=interception.reason if not interception.allowed else None
                )
            except Exception as e:
                logger.error(f"Error executing tool {executable_tool.value}: {e}", exc_info=True)
                tool_result = ToolResult(
                    call_id=tool_call.call_id,
                    tool_name=executable_tool,
                    success=False,
                    status="failed",
                    error_reason=f"Tool execution exception: {str(e)}",
                    recovered_amount=0.0
                )

            state.add_tool_execution(tool_call, tool_result)

            # --- STEP D: VERIFY ---
            v_result = self.verifier.verify(
                tool_result=tool_result,
                current_iteration=current_iter,
                max_iterations=state.max_iterations
            )

            # --- STEP E: RECORD THOUGHT & REFLECTION STEP ---
            reflection = (
                f"Verification: {v_result.status.value}. {v_result.summary}"
            )
            
            thought_step = ThoughtStep(
                step_number=current_iter,
                observation=f"Attempt {current_iter} with {executable_tool.value}",
                thought_summary=thought_summary,
                selected_action=executable_tool,
                verification_result=v_result.status.value,
                reflection_summary=reflection
            )
            state.add_thought_step(thought_step)

            # --- STEP F: TERMINATION OR RE-PLAN ---
            if v_result.status == VerificationStatus.SUCCESS:
                state.final_status = "recovered"
                state.recovered_amount = v_result.recovered_amount
                state.final_decision = f"Revenue successfully recovered (INR {v_result.recovered_amount:,.2f}) via '{executable_tool.value}'."
                break

            elif v_result.status == VerificationStatus.ESCALATED:
                state.final_status = "escalated"
                state.final_decision = "Transaction quarantined to Human Operations queue."
                unresolved_reason = interception.reason if not interception.allowed else (tool_result.error_reason or "Quarantined for manual fraud & risk review.")
                break

            elif v_result.status == VerificationStatus.ABANDONED:
                state.final_status = "abandoned"
                state.final_decision = "Recovery halted by stopping rule."
                unresolved_reason = interception.reason if not interception.allowed else (tool_result.error_reason or "Max attempts exhausted without recovery.")
                break

            else:
                # Tool failed; check if another iteration is permitted
                if current_iter >= state.max_iterations:
                    state.final_status = "abandoned"
                    state.termination_reason = f"Max iterations ({state.max_iterations}) reached without customer conversion."
                    state.final_decision = "Recovery stopping rule reached: max attempts exhausted."
                    unresolved_reason = f"Max iterations ({state.max_iterations}) reached. Last failure: {tool_result.error_reason or 'No conversion'}"
                    break
                else:
                    # Proceed to re-plan on next loop iteration
                    logger.info(
                        f"Txn {txn.transaction_id}: Tool {executable_tool.value} failed on iter {current_iter}. Re-planning for iter {current_iter + 1}."
                    )

        # 4. ASSEMBLE AUDIT & SUMMARY RESULT
        concise_summary = (
            f"[{txn.transaction_id}] Final: {state.final_status.upper()} | "
            f"Tool: {final_action_str} | Recovered: INR {state.recovered_amount:,.2f} | "
            f"Iterations: {state.iteration_count}/{state.max_iterations} | "
            f"Diagnosis: {diagnosis.root_cause.value} ({diagnosis.source.value})"
        )

        return RecoveryAgentResult(
            transaction_id=txn.transaction_id,
            final_status=state.final_status,
            final_action=final_action_str,
            recovered_amount=state.recovered_amount,
            iterations_used=state.iteration_count,
            diagnosis=diagnosis,
            concise_decision_summary=concise_summary,
            tool_history=state.tool_results,
            thought_history=state.thought_steps,
            unresolved_reason=unresolved_reason,
            state=state
        )

    def _plan_next_action(
        self,
        txn: Transaction,
        diagnosis: DiagnosisResult,
        state: AutonomousAgentState
    ) -> AgentToolName:
        """
        Determines the optimal next tool, adapting strategy dynamically based on previous failures.
        """
        used_tools = [call.tool_name for call in state.tool_calls]
        root_cause = diagnosis.root_cause

        # Hard Rule: Risk blocked always targets human escalation
        if root_cause == RootCause.RISK_BLOCKED:
            return AgentToolName.HUMAN_ESCALATION

        # --- ITERATION 1: Primary Baseline Strategy ---
        if not used_tools:
            if root_cause in [RootCause.BANK_TIMEOUT, RootCause.NETWORK_GLITCH]:
                return AgentToolName.SWITCH_ROUTING
            elif root_cause in [RootCause.WRONG_OTP, RootCause.INSUFFICIENT_FUNDS]:
                return AgentToolName.WHATSAPP_NUDGE
            elif root_cause == RootCause.CARD_EXPIRED:
                return AgentToolName.UPI_SWITCH
            else:
                return AgentToolName.WHATSAPP_NUDGE

        # --- ITERATION > 1: Adaptive Re-Planning (Never repeat a failed tool) ---
        last_tool = used_tools[-1]

        if last_tool == AgentToolName.SWITCH_ROUTING:
            # If gateway switch failed, adapt to interactive WhatsApp customer nudge
            if AgentToolName.WHATSAPP_NUDGE not in used_tools:
                return AgentToolName.WHATSAPP_NUDGE
            else:
                return AgentToolName.UPI_SWITCH

        elif last_tool == AgentToolName.WHATSAPP_NUDGE:
            # If customer did not convert on WhatsApp, offer alternative instant UPI checkout link
            if AgentToolName.UPI_SWITCH not in used_tools:
                return AgentToolName.UPI_SWITCH
            else:
                # If high value, escalate to human concierge; otherwise give up
                return AgentToolName.HUMAN_ESCALATION if txn.amount >= 15000.0 else AgentToolName.GIVE_UP

        elif last_tool == AgentToolName.UPI_SWITCH:
            # If UPI switch was abandoned, escalate to VIP ops or trigger stopping rule
            if txn.amount >= 15000.0:
                return AgentToolName.HUMAN_ESCALATION
            else:
                return AgentToolName.GIVE_UP

        else:
            return AgentToolName.GIVE_UP

# Global RecoveryAgent singleton
recovery_agent = RecoveryAgent()
