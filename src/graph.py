from langgraph.graph import StateGraph, END
from src.nodes import reroute_node, triage_node, escalate_node, comms_node, resolve_node, route_decision
from src.state import DeliveryExceptionState


def build_graph():
    # Create the graph with our state Schema
    graph = StateGraph(DeliveryExceptionState)

    graph.add_node("triage", triage_node)
    graph.add_node("reroute", reroute_node)
    graph.add_node("escalate", escalate_node)
    graph.add_node("comms", comms_node)
    graph.add_node("resolve", resolve_node)

    graph.set_entry_point("triage")

    graph.add_conditional_edges(
        "triage",
        route_decision,
        {
            "auto_resolve": "reroute",
            "escalate": "escalate",
        },
    )

    graph.add_edge("reroute", "comms")
    graph.add_edge("escalate", "comms")
    graph.add_edge("comms", "resolve")
    graph.add_edge("resolve", END)

    return graph.compile()


app = build_graph()
