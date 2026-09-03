import pytest
from datetime import datetime, timezone, timedelta
from app.schemas.transaction import (
    Transaction,
    PaymentMethod,
    PaymentMethodDetails,
    CustomerHistory
)
from app.schemas.diagnosis import DiagnosisResult, RootCause, DiagnosisSource
from app.schemas.agent_state import (
    AgentToolName,
    ToolCall,
    ToolResult,
    ThoughtStep,
    AutonomousAgentState,
)
from app.agents.recovery_agent import RecoveryAgent, RecoveryAgentResult
from app.agents.tools.base_tool import BaseTool, ToolRegistry
from app.agents.tools.guardrail_interceptor import GuardrailInterceptor
from app.agents.verification import Verifier, VerificationStatus

# Mock Tool Implementations for Deterministic Agent Loop Testing
class DeterministicSuccessTool(BaseTool):
    name = AgentToolName.SWITCH_ROUTING
    description = "Mock switch routing tool that always succeeds"

    def execute(self, txn: Transaction, diagnosis=None, **kwargs) -> ToolResult:
        return ToolResult(
            call_id=kwargs.get("call_id", "test_call_success"),
            tool_name=self.name,
            success=True,
            status="recovered",
            result_data={"switch": "switch_hdfc_mesh"},
            recovered_amount=txn.amount
        )

class DeterministicFailTool(BaseTool):
    name = AgentToolName.SWITCH_ROUTING
    description = "Mock switch routing tool that always fails"

    def execute(self, txn: Transaction, diagnosis=None, **kwargs) -> ToolResult:
        return ToolResult(
            call_id=kwargs.get("call_id", "test_call_fail"),
            tool_name=self.name,
            success=False,
            status="failed_retry",
            result_data={"switch": "switch_icici_fail"},
            error_reason="Simulated gateway switch failure",
            recovered_amount=0.0
        )

class DeterministicNudgeSuccessTool(BaseTool):
    name = AgentToolName.WHATSAPP_NUDGE
    description = "Mock WhatsApp nudge tool that converts on attempt"

    def execute(self, txn: Transaction, diagnosis=None, **kwargs) -> ToolResult:
        return ToolResult(
            call_id=kwargs.get("call_id", "test_nudge_success"),
            tool_name=self.name,
            success=True,
            status="recovered",
            result_data={"nudge_message": "Aapka payment complete karein"},
            recovered_amount=txn.amount
        )

class DeterministicNudgeFailTool(BaseTool):
    name = AgentToolName.WHATSAPP_NUDGE
    description = "Mock WhatsApp nudge tool that does not convert"

    def execute(self, txn: Transaction, diagnosis=None, **kwargs) -> ToolResult:
        return ToolResult(
            call_id=kwargs.get("call_id", "test_nudge_fail"),
            tool_name=self.name,
            success=False,
            status="nudged_unconverted",
            error_reason="Customer dropped off without paying",
            recovered_amount=0.0
        )

class DeterministicUPISuccessTool(BaseTool):
    name = AgentToolName.UPI_SWITCH
    description = "Mock UPI switch tool that succeeds"

    def execute(self, txn: Transaction, diagnosis=None, **kwargs) -> ToolResult:
        return ToolResult(
            call_id=kwargs.get("call_id", "test_upi_success"),
            tool_name=self.name,
            success=True,
            status="recovered",
            result_data={"target_method": "upi"},
            recovered_amount=txn.amount
        )

class DeterministicUPIFailTool(BaseTool):
    name = AgentToolName.UPI_SWITCH
    description = "Mock UPI switch tool that fails"

    def execute(self, txn: Transaction, diagnosis=None, **kwargs) -> ToolResult:
        return ToolResult(
            call_id=kwargs.get("call_id", "test_upi_fail"),
            tool_name=self.name,
            success=False,
            status="alt_method_abandoned",
            error_reason="Customer abandoned UPI checkout link",
            recovered_amount=0.0
        )

class StandardHumanEscalationTool(BaseTool):
    name = AgentToolName.HUMAN_ESCALATION
    description = "Standard human escalation tool"

    def execute(self, txn: Transaction, diagnosis=None, **kwargs) -> ToolResult:
        return ToolResult(
            call_id=kwargs.get("call_id", "test_esc"),
            tool_name=self.name,
            success=False,
            status="escalated_to_ops",
            error_reason="Quarantined to human fraud review queue",
            recovered_amount=0.0
        )

class StandardGiveUpTool(BaseTool):
    name = AgentToolName.GIVE_UP
    description = "Standard give up tool"

    def execute(self, txn: Transaction, diagnosis=None, **kwargs) -> ToolResult:
        return ToolResult(
            call_id=kwargs.get("call_id", "test_giveup"),
            tool_name=self.name,
            success=False,
            status="abandoned",
            error_reason="Stopping rule triggered: max retries reached",
            recovered_amount=0.0
        )

@pytest.fixture
def sample_txn():
    return Transaction(
        transaction_id="txn_agent_test_101",
        customer_id="cust_test_001",
        customer_name="Priya Patel",
        customer_phone="+919876543211",
        customer_email="priya@example.com",
        amount=3499.0,
        currency="INR",
        payment_method=PaymentMethod.CARD,
        payment_method_details=PaymentMethodDetails(bank_code="HDFC"),
        error_code="GATEWAY_ERROR",
        error_description="Bank switch timed out",
        error_source="bank_switch",
        retry_count=0,
        last_retry_timestamp=None,
        timestamp=datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc),
        auto_charge_consent=True,
        customer_history=CustomerHistory(reliability_score=0.92)
    )

# 1. Successful first-tool recovery.
def test_1_successful_first_tool_recovery(sample_txn):
    reg = ToolRegistry()
    reg.register(DeterministicSuccessTool())
    reg.register(DeterministicNudgeFailTool())
    reg.register(DeterministicUPIFailTool())
    reg.register(StandardHumanEscalationTool())
    reg.register(StandardGiveUpTool())

    agent = RecoveryAgent(tool_reg=reg)
    result = agent.recover(sample_txn)

    assert result.final_status == "recovered"
    assert result.recovered_amount == 3499.0
    assert result.iterations_used == 1
    assert result.final_action == "switch_routing"
    assert len(result.tool_history) == 1
    assert result.tool_history[0].success is True

# 2. Failed first tool causes re-planning.
# 3. Second tool can recover the transaction.
def test_2_and_3_failed_first_tool_causes_replanning_and_recovers(sample_txn):
    # Step 1 fails on switch_routing, Step 2 succeeds on whatsapp_nudge
    reg = ToolRegistry()
    reg.register(DeterministicFailTool())          # switch_routing fails
    reg.register(DeterministicNudgeSuccessTool())   # whatsapp_nudge succeeds
    reg.register(DeterministicUPIFailTool())
    reg.register(StandardHumanEscalationTool())
    reg.register(StandardGiveUpTool())

    agent = RecoveryAgent(tool_reg=reg)
    result = agent.recover(sample_txn)

    assert result.final_status == "recovered"
    assert result.recovered_amount == 3499.0
    assert result.iterations_used == 2
    assert result.final_action == "whatsapp_nudge"
    assert len(result.tool_history) == 2
    assert result.tool_history[0].tool_name == AgentToolName.SWITCH_ROUTING
    assert result.tool_history[0].success is False
    assert result.tool_history[1].tool_name == AgentToolName.WHATSAPP_NUDGE
    assert result.tool_history[1].success is True

# 4. Max iteration limit prevents infinite loops.
def test_4_max_iteration_limit_prevents_infinite_loops(sample_txn):
    # All tools fail across iterations
    reg = ToolRegistry()
    reg.register(DeterministicFailTool())         # iter 1: switch_routing fails
    reg.register(DeterministicNudgeFailTool())    # iter 2: whatsapp_nudge fails
    reg.register(DeterministicUPIFailTool())      # iter 3: upi_switch fails
    reg.register(StandardHumanEscalationTool())
    reg.register(StandardGiveUpTool())

    agent = RecoveryAgent(tool_reg=reg, max_iterations=3)
    result = agent.recover(sample_txn)

    assert result.final_status == "abandoned"
    assert result.recovered_amount == 0.0
    assert result.iterations_used == 3
    assert len(result.thought_history) == 3
    assert "Max iterations (3) reached" in (result.unresolved_reason or "")

# 5. Risk-blocked transaction cannot use automated recovery.
def test_5_risk_blocked_strictly_escalates_to_human(sample_txn):
    sample_txn.error_code = "risk_blocked"
    sample_txn.error_source = "risk_engine"

    reg = ToolRegistry()
    reg.register(DeterministicSuccessTool())
    reg.register(DeterministicNudgeSuccessTool())
    reg.register(DeterministicUPISuccessTool())
    reg.register(StandardHumanEscalationTool())
    reg.register(StandardGiveUpTool())

    diag = DiagnosisResult(
        transaction_id=sample_txn.transaction_id,
        root_cause=RootCause.RISK_BLOCKED,
        confidence=0.99,
        reasoning="Risk engine fraud trigger",
        source=DiagnosisSource.RULES
    )

    agent = RecoveryAgent(tool_reg=reg)
    result = agent.recover(sample_txn, custom_diagnosis=diag)

    assert result.final_status == "escalated"
    assert result.recovered_amount == 0.0
    assert result.final_action == "human_escalation"
    assert result.iterations_used == 1
    assert result.tool_history[0].tool_name == AgentToolName.HUMAN_ESCALATION

# 6. Guardrail-blocked action is safely redirected.
def test_6_guardrail_consent_violation_safely_redirected(sample_txn):
    sample_txn.auto_charge_consent = False  # Consent missing

    reg = ToolRegistry()
    reg.register(DeterministicSuccessTool())
    reg.register(DeterministicNudgeSuccessTool())
    reg.register(DeterministicUPISuccessTool())
    reg.register(StandardHumanEscalationTool())
    reg.register(StandardGiveUpTool())

    diag = DiagnosisResult(
        transaction_id=sample_txn.transaction_id,
        root_cause=RootCause.BANK_TIMEOUT,
        confidence=0.96,
        reasoning="Bank timeout",
        source=DiagnosisSource.RULES
    )

    agent = RecoveryAgent(tool_reg=reg)
    result = agent.recover(sample_txn, custom_diagnosis=diag)

    # Candidate was switch_routing, but guardrail transformed to whatsapp_nudge
    assert result.final_action == "whatsapp_nudge"
    assert result.final_status == "recovered"
    assert result.iterations_used == 1
    assert "guardrail 'CONSENT_MANDATE_ENFORCEMENT' intervened" in result.thought_history[0].thought_summary

# 7. Human escalation produces correct final status.
def test_7_high_value_retry_cap_escalates_to_human(sample_txn):
    sample_txn.retry_count = 3
    sample_txn.amount = 25000.0  # High value VIP transaction

    reg = ToolRegistry()
    reg.register(DeterministicSuccessTool())
    reg.register(DeterministicNudgeSuccessTool())
    reg.register(DeterministicUPISuccessTool())
    reg.register(StandardHumanEscalationTool())
    reg.register(StandardGiveUpTool())

    agent = RecoveryAgent(tool_reg=reg)
    result = agent.recover(sample_txn)

    assert result.final_status == "escalated"
    assert result.recovered_amount == 0.0
    assert result.final_action == "human_escalation"

# 8. Give-up produces correct final status.
def test_8_standard_retry_cap_triggers_give_up(sample_txn):
    sample_txn.retry_count = 3
    sample_txn.amount = 1200.0  # Standard value

    reg = ToolRegistry()
    reg.register(DeterministicSuccessTool())
    reg.register(DeterministicNudgeSuccessTool())
    reg.register(DeterministicUPISuccessTool())
    reg.register(StandardHumanEscalationTool())
    reg.register(StandardGiveUpTool())

    agent = RecoveryAgent(tool_reg=reg)
    result = agent.recover(sample_txn)

    assert result.final_status == "abandoned"
    assert result.recovered_amount == 0.0
    assert result.final_action == "give_up"
    assert "Max retry limit reached" in (result.unresolved_reason or "")

# 9. Agent state records tool history.
def test_9_agent_state_full_traceability(sample_txn):
    reg = ToolRegistry()
    reg.register(DeterministicFailTool())
    reg.register(DeterministicNudgeSuccessTool())
    reg.register(DeterministicUPIFailTool())
    reg.register(StandardHumanEscalationTool())
    reg.register(StandardGiveUpTool())

    agent = RecoveryAgent(tool_reg=reg)
    result = agent.recover(sample_txn)

    state = result.state
    assert len(state.tool_calls) == 2
    assert len(state.tool_results) == 2
    assert len(state.thought_steps) == 2
    assert state.tool_calls[0].tool_name == AgentToolName.SWITCH_ROUTING
    assert state.tool_calls[1].tool_name == AgentToolName.WHATSAPP_NUDGE
    assert state.thought_steps[0].step_number == 1
    assert state.thought_steps[1].step_number == 2
    assert state.thought_steps[0].verification_result == "FAILED"
    assert state.thought_steps[1].verification_result == "SUCCESS"
