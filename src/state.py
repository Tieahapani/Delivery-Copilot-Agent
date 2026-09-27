"""
state.py 

Defines the state schema that flows through the LangGraph graph.
Every node reads from and writes to this shared state.
Each field maps to exactly one node's output.

"""


from typing import TypedDict, Optional

class DeliveryExceptionState(TypedDict):
    """State that flows through the entire copilot graph."""

    event: dict 
    triage: Optional[dict]
    route_decision: Optional[str]
    reroute: Optional[dict]
    escalation: Optional[dict]
    notification: Optional[dict]
    resolution: Optional[dict]

    

