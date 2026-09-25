from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import (
    coordinator_node,
    order_node,
    payment_node,
    policy_node,
    shipment_node,
    verifier_node,
)
from .state import CaseGraphState


def build_case_graph():
    """Build the acyclic, per-case workflow and compile it once per solve."""
    graph = StateGraph(CaseGraphState)
    graph.add_node("coordinator", coordinator_node)
    graph.add_node("order", order_node)
    graph.add_node("payment", payment_node)
    graph.add_node("shipment", shipment_node)
    graph.add_node("policy", policy_node)
    graph.add_node("verifier", verifier_node)
    graph.add_edge(START, "coordinator")
    graph.add_edge("coordinator", "order")
    graph.add_edge("order", "payment")
    graph.add_edge("payment", "shipment")
    graph.add_edge("shipment", "policy")
    graph.add_edge("policy", "verifier")
    graph.add_edge("verifier", END)
    return graph.compile()
