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
from app.agents.tools.base_tool import BaseTool, ToolRegistry
from app.agents.tools.switch_routing_tool import SwitchRoutingTool
from app.agents.tools.whatsapp_nudge_tool import WhatsAppNudgeTool
from app.agents.tools.upi_switch_tool import UPISwitchTool
from app.agents.tools.human_escalation_tool import HumanEscalationTool
from app.agents.tools.give_up_tool import GiveUpTool
from app.agents.tools.guardrail_interceptor import (
    GuardrailInterceptor,
    GuardrailInterceptionResult,
)

@pytest.fixture
def clean_registry():
    return ToolRegistry()

@pytest.fixture
def interceptor():
    return GuardrailInterceptor()

@pytest.fixture
def sample_transaction():
    return Transaction(
        transaction_id="txn_test_agent_001",
        customer_id="cust_test_901",
        customer_name="Rohan Verma",
        customer_phone="+919876543210",
        customer_email="rohan.v@example.com",
        amount=2500.0,
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
        customer_history=CustomerHistory(reliability_score=0.9)
    )

# 1. Every expected tool can be registered.
def test_1_all_expected_tools_can_be_registered(clean_registry):
    tools = [
        SwitchRoutingTool(),
        WhatsAppNudgeTool(),
        UPISwitchTool(),
        HumanEscalationTool(),
        GiveUpTool(),
    ]
    for t in tools:
        clean_registry.register(t)
    
    assert len(clean_registry.list_tools()) == 5
    tool_names = clean_registry.get_tool_names()
    assert "switch_routing" in tool_names
    assert "whatsapp_nudge" in tool_names
    assert "upi_switch" in tool_names
    assert "human_escalation" in tool_names
    assert "give_up" in tool_names

# 2. Duplicate tool registration is rejected.
def test_2_duplicate_tool_registration_rejected(clean_registry):
    tool1 = SwitchRoutingTool()
    tool2 = SwitchRoutingTool()
    clean_registry.register(tool1)
    
    with pytest.raises(ValueError) as excinfo:
        clean_registry.register(tool2)
    assert "already registered" in str(excinfo.value)

# 3. Tool lookup works.
def test_3_tool_lookup_by_enum_and_string(clean_registry):
    clean_registry.register(WhatsAppNudgeTool())
    
    tool_by_enum = clean_registry.get(AgentToolName.WHATSAPP_NUDGE)
    assert tool_by_enum.name == AgentToolName.WHATSAPP_NUDGE
    
    tool_by_str = clean_registry.get("whatsapp_nudge")
    assert tool_by_str.name == AgentToolName.WHATSAPP_NUDGE
    
    with pytest.raises(KeyError):
        clean_registry.get("non_existent_tool")

# 4. Tool schemas validate correctly.
def test_4_agent_state_schemas_validation():
    # Test ToolCall
    call = ToolCall(tool_name=AgentToolName.SWITCH_ROUTING, input_arguments={"switch": "hdfc"})
    assert call.tool_name == AgentToolName.SWITCH_ROUTING
    assert call.call_id.startswith("call_")
    
    # Test ToolResult
    result = ToolResult(
        call_id=call.call_id,
        tool_name=AgentToolName.SWITCH_ROUTING,
        success=True,
        status="recovered",
        recovered_amount=1500.0
    )
    assert result.success is True
    assert result.recovered_amount == 1500.0
    
    # Test ThoughtStep
    step = ThoughtStep(
        step_number=1,
        observation="Transaction failed due to bank timeout",
        thought_summary="Bank node degraded, attempting switch routing",
        selected_action=AgentToolName.SWITCH_ROUTING,
        verification_result="Payment recovered via alternate switch"
    )
    assert step.step_number == 1
    
    # Test AutonomousAgentState
    state = AutonomousAgentState(
        transaction_id="txn_agent_state_test",
        current_observation="Failure observed",
        max_iterations=3
    )
    state.add_thought_step(step)
    state.add_tool_execution(call, result)
    assert len(state.thought_steps) == 1
    assert len(state.tool_calls) == 1
    assert state.recovered_amount == 1500.0
    assert state.final_status == "recovered"

# 5. Retry request with exhausted retry count is blocked.
def test_5_retry_request_with_exhausted_retry_count_blocked(interceptor, sample_transaction):
    sample_transaction.retry_count = 3
    sample_transaction.amount = 2000.0
    
    res = interceptor.intercept(
        requested_tool=AgentToolName.SWITCH_ROUTING,
        txn=sample_transaction
    )
    assert res.allowed is False
    assert res.rule_triggered == "MAX_RETRY_CAP"
    assert res.final_permitted_tool == AgentToolName.GIVE_UP
    assert "Max retry limit reached" in res.reason

# 6. Retry request without consent is blocked/transformed.
def test_6_retry_request_without_consent_transformed(interceptor, sample_transaction):
    sample_transaction.auto_charge_consent = False
    
    res = interceptor.intercept(
        requested_tool=AgentToolName.SWITCH_ROUTING,
        txn=sample_transaction
    )
    assert res.allowed is False
    assert res.rule_triggered == "CONSENT_MANDATE_ENFORCEMENT"
    assert res.final_permitted_tool == AgentToolName.WHATSAPP_NUDGE
    assert "auto-charge consent" in res.reason

# 7. Retry during cooldown is blocked/transformed.
def test_7_retry_during_cooldown_blocked(interceptor, sample_transaction):
    sample_transaction.retry_count = 1
    sample_transaction.auto_charge_consent = True
    now_ts = datetime(2026, 9, 2, 10, 15, 0, tzinfo=timezone.utc)
    sample_transaction.timestamp = now_ts
    sample_transaction.last_retry_timestamp = now_ts - timedelta(minutes=10) # 10m < 30m cooldown
    
    res = interceptor.intercept(
        requested_tool=AgentToolName.SWITCH_ROUTING,
        txn=sample_transaction
    )
    assert res.allowed is False
    assert res.rule_triggered == "COOLDOWN_WINDOW_ENFORCEMENT"
    assert res.final_permitted_tool == AgentToolName.WHATSAPP_NUDGE
    assert "Cooldown violation" in res.reason

# 8. risk_blocked cannot execute automated recovery.
def test_8_risk_blocked_cannot_execute_automated_recovery(interceptor, sample_transaction):
    diagnosis = DiagnosisResult(
        transaction_id=sample_transaction.transaction_id,
        root_cause=RootCause.RISK_BLOCKED,
        confidence=0.98,
        reasoning="Risk engine fraud trigger",
        source=DiagnosisSource.RULES
    )
    
    # Try calling switch_routing
    res1 = interceptor.intercept(
        requested_tool=AgentToolName.SWITCH_ROUTING,
        txn=sample_transaction,
        diagnosis=diagnosis
    )
    assert res1.allowed is False
    assert res1.rule_triggered == "RISK_SAFETY_ISOLATION"
    assert res1.final_permitted_tool == AgentToolName.HUMAN_ESCALATION
    
    # Try calling whatsapp_nudge
    res2 = interceptor.intercept(
        requested_tool=AgentToolName.WHATSAPP_NUDGE,
        txn=sample_transaction,
        diagnosis=diagnosis
    )
    assert res2.allowed is False
    assert res2.rule_triggered == "RISK_SAFETY_ISOLATION"
    assert res2.final_permitted_tool == AgentToolName.HUMAN_ESCALATION

# 9. human_escalation remains allowed for risk_blocked.
def test_9_human_escalation_allowed_for_risk_blocked(interceptor, sample_transaction):
    diagnosis = DiagnosisResult(
        transaction_id=sample_transaction.transaction_id,
        root_cause=RootCause.RISK_BLOCKED,
        confidence=0.98,
        reasoning="Risk engine fraud trigger",
        source=DiagnosisSource.RULES
    )
    
    res = interceptor.intercept(
        requested_tool=AgentToolName.HUMAN_ESCALATION,
        txn=sample_transaction,
        diagnosis=diagnosis
    )
    assert res.allowed is True
    assert res.rule_triggered is None
    assert res.final_permitted_tool == AgentToolName.HUMAN_ESCALATION

# 10. AgentState iteration cannot exceed max_iterations.
def test_10_agent_state_iteration_limit_enforced():
    state = AutonomousAgentState(
        transaction_id="txn_test_iterations",
        current_observation="Initial failure",
        max_iterations=3
    )
    
    assert state.increment_iteration() == 1
    assert state.increment_iteration() == 2
    assert state.increment_iteration() == 3
    
    with pytest.raises(ValueError) as excinfo:
        state.increment_iteration()
    assert "max iterations (3) reached" in str(excinfo.value)
