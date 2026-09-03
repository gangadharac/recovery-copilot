from app.agents.tools.base_tool import BaseTool, ToolRegistry
from app.agents.tools.switch_routing_tool import SwitchRoutingTool
from app.agents.tools.whatsapp_nudge_tool import WhatsAppNudgeTool
from app.agents.tools.upi_switch_tool import UPISwitchTool
from app.agents.tools.human_escalation_tool import HumanEscalationTool
from app.agents.tools.give_up_tool import GiveUpTool
from app.agents.tools.guardrail_interceptor import (
    GuardrailInterceptor,
    GuardrailInterceptionResult,
    guardrail_interceptor,
)

# Initialize default ToolRegistry with all 5 recovery tools
default_tool_registry = ToolRegistry()
default_tool_registry.register(SwitchRoutingTool())
default_tool_registry.register(WhatsAppNudgeTool())
default_tool_registry.register(UPISwitchTool())
default_tool_registry.register(HumanEscalationTool())
default_tool_registry.register(GiveUpTool())

__all__ = [
    "BaseTool",
    "ToolRegistry",
    "default_tool_registry",
    "SwitchRoutingTool",
    "WhatsAppNudgeTool",
    "UPISwitchTool",
    "HumanEscalationTool",
    "GiveUpTool",
    "GuardrailInterceptor",
    "GuardrailInterceptionResult",
    "guardrail_interceptor",
]
