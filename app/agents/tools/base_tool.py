from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Union
from app.schemas.transaction import Transaction
from app.schemas.diagnosis import DiagnosisResult
from app.schemas.agent_state import AgentToolName, ToolResult

class BaseTool(ABC):
    """
    Abstract base class for all Recovery Agent tools.
    Encapsulates execution logic as a thin adapter around existing capabilities.
    """
    name: AgentToolName
    description: str

    @abstractmethod
    def execute(
        self,
        txn: Transaction,
        diagnosis: Optional[DiagnosisResult] = None,
        **kwargs: Any
    ) -> ToolResult:
        """Executes the tool for a given transaction context and returns a typed ToolResult."""
        pass

class ToolRegistry:
    """
    Central registry for autonomous agent tools.
    Rejects duplicate registrations and provides typed lookups.
    """
    def __init__(self):
        self._tools: Dict[AgentToolName, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Registers a new tool. Raises ValueError if tool with same name already exists."""
        if not isinstance(tool, BaseTool):
            raise TypeError(f"Expected BaseTool instance, got {type(tool)}")
        
        if tool.name in self._tools:
            raise ValueError(f"Tool with name '{tool.name.value}' is already registered.")
        
        self._tools[tool.name] = tool

    def get(self, tool_name: Union[AgentToolName, str]) -> BaseTool:
        """Retrieves a registered tool by name or enum. Raises KeyError if not found."""
        if isinstance(tool_name, str):
            try:
                tool_enum = AgentToolName(tool_name)
            except ValueError:
                raise KeyError(f"Unknown tool name '{tool_name}'.")
        else:
            tool_enum = tool_name

        if tool_enum not in self._tools:
            raise KeyError(f"Tool '{tool_enum.value}' is not registered.")
        return self._tools[tool_enum]

    def list_tools(self) -> List[BaseTool]:
        """Returns a list of all registered tools."""
        return list(self._tools.values())

    def get_tool_names(self) -> List[str]:
        """Returns a list of registered tool name strings."""
        return [tool.name.value for tool in self._tools.values()]

    def clear(self) -> None:
        """Clears all registered tools (useful for testing)."""
        self._tools.clear()
