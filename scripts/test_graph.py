"""
test_graph.py

Runs the copilot graph on a single delivery exception event
to verify the full pipeline works: triage → route → comms → resolve.

Prerequisites:
  - OSRM running at localhost:5050
  - ANTHROPIC_API_KEY set in environment
  - pip install langgraph langchain-anthropic

Usage:
  export ANTHROPIC_API_KEY=your-key-here
  python scripts/test_graph.py
"""

import json
import os
import sys
from dotenv import load_dotenv
load_dotenv()

# Add project root to path so we can import src/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.graph import build_graph


def load_sample_event() -> dict:
    """Load the first exception event from the manifest."""
    manifest_path = os.path.join(os.path.dirname(__file__), "..", "data", "manifest.json")
    manifest_path = os.path.abspath(manifest_path)

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Find the first stop with an exception
    for driver in manifest["drivers"]:
        for stop in driver["stops"]:
            if stop["exception"] is not None:
                # Flatten into the event format (same as event_producer)
                return {
                    "delivery_id": stop["delivery_id"],
                    "driver_id": driver["driver_id"],
                    "exception_type": stop["exception"]["type"],
                    "severity": stop["exception"]["severity"],
                    "details": stop["exception"]["details"],
                    "business_name": stop["business_name"],
                    "address": stop["address"],
                    "neighborhood": stop["neighborhood"],
                    "lat": str(stop["lat"]),
                    "lon": str(stop["lon"]),
                    "eta": stop["eta"],
                    "sla_deadline": stop["sla_deadline"],
                    "package_priority": stop["package_priority"],
                    "drive_time_minutes": str(stop["drive_time_minutes"]),
                    "distance_km": str(stop["distance_km"]),
                    "remaining_stops": str(driver["total_stops"]),
                }

    print("ERROR: No exceptions found in manifest. Regenerate with:")
    print("  python scripts/generate_manifest.py")
    sys.exit(1)


def main():
    # Check for API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: Set your ANTHROPIC_API_KEY environment variable:")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    print("=" * 60)
    print("  Copilot Graph Test")
    print("=" * 60)
    print()

    # Load a sample event
    print("[1/3] Loading sample event from manifest...")
    event = load_sample_event()
    print(f"  Delivery: {event['delivery_id']}")
    print(f"  Driver:   {event['driver_id']}")
    print(f"  Type:     {event['exception_type']}")
    print(f"  Severity: {event['severity']}")
    print(f"  Business: {event['business_name']}")
    print(f"  Priority: {event['package_priority']}")
    print()

    # Build graph
    print("[2/3] Building copilot graph...")
    graph = build_graph()
    print("  Graph compiled.\n")

    # Run
    print("[3/3] Running graph on event...")
    print("  (calling Claude API for triage and comms)\n")

    initial_state = {
        "event": event,
        "triage": None,
        "route_decision": None,
        "reroute": None,
        "escalation": None,
        "notification": None,
        "resolution": None,
    }

    result = graph.invoke(initial_state)

    # Print results
    print("=" * 60)
    print("  TRIAGE")
    print("=" * 60)
    triage = result.get("triage", {})
    print(f"  Urgency:   {triage.get('urgency')}")
    print(f"  Decision:  {triage.get('decision')}")
    print(f"  Reasoning: {triage.get('reasoning')}")
    print(f"  SLA in:    {triage.get('time_to_sla_minutes')} min")
    print(f"  LLM time:  {triage.get('processing_time_ms')} ms")
    print()

    print("=" * 60)
    print(f"  ROUTE: {result.get('route_decision', 'unknown').upper()}")
    print("=" * 60)

    if result.get("reroute"):
        reroute = result["reroute"]
        print(f"  Action:    {reroute.get('action')}")
        print(f"  New ETA:   {reroute.get('new_eta')}")
        print(f"  Added:     {reroute.get('added_minutes')} min")
        print(f"  SLA met:   {reroute.get('sla_met')}")
        print(f"  Summary:   {reroute.get('summary')}")
    elif result.get("escalation"):
        esc = result["escalation"]
        print(f"  Priority:  {esc.get('priority')}")
        print(f"  Action:    {esc.get('recommended_action')}")
        print(f"  Immediate: {esc.get('requires_immediate_attention')}")
    print()

    print("=" * 60)
    print("  NOTIFICATION")
    print("=" * 60)
    notif = result.get("notification", {})
    print(f"  Channel:   {notif.get('channel')}")
    print(f"  Tone:      {notif.get('tone')}")
    print(f"  Message:   {notif.get('message')}")
    print()

    print("=" * 60)
    print("  RESOLUTION")
    print("=" * 60)
    res = result.get("resolution", {})
    print(f"  Outcome:   {res.get('outcome')}")
    print(f"  Nodes:     {' → '.join(res.get('nodes_executed', []))}")
    print(f"  SLA:       {res.get('sla_impact')}")
    print(f"  Total ms:  {res.get('total_processing_time_ms')}")
    print("=" * 60)


if __name__ == "__main__":
    main()