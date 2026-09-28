"""Outer fixed pipeline for FIN-C2-161."""

from __future__ import annotations

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.main_node import DomainWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import AMLDispositionState


class AMLDispositionMemoAgent(AgentBaseGraph):
    """A Cat2 draft-only AML disposition memo agent."""

    @property
    def name(self) -> str:
        return "AMLDispositionMemoAgent"

    @property
    def state_schema(self) -> type:
        return AMLDispositionState

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = DomainWorkflowGraphNode(config=self.config)
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: AMLDispositionState) -> dict:
        """Never fall back to the inner graph's unverified ``result`` field."""
        output = state.get("formatted_output") if state.get("status") == AgentStatus.SUCCESS.value else None
        response = {
            "output": output if isinstance(output, dict) else None,
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
        if state.get("error_log"):
            response["error_log"] = state["error_log"]
        return response


# Backward-compatible import name for scaffolding and smoke tests.
Graph = AMLDispositionMemoAgent
