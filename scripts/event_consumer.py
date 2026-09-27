"""
event_consumer.py

Reads delivery exception events from the Redis Stream and processes them.
This is a placeholder consumer that will be replaced by LangGraph agents.

For now it:
  1. Reads events from the 'delivery:exceptions' stream
  2. Classifies urgency based on SLA gap and severity
  3. Logs the triage decision
  4. Acknowledges the event

Prerequisites:
  - Redis running: docker compose up -d
  - Events published: python scripts/event_producer.py
  - pip install redis

Usage:
  python scripts/event_consumer.py
  python scripts/event_consumer.py --consumer agent-2
"""

import argparse
import sys
from datetime import datetime

import redis

# ----------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------

REDIS_HOST = "localhost"
REDIS_PORT = 6379
STREAM_NAME = "delivery:exceptions"
CONSUMER_GROUP = "copilot-agents"


def connect_redis() -> redis.Redis:
    """Connect to Redis and verify."""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    try:
        r.ping()
        return r
    except redis.ConnectionError:
        print("ERROR: Cannot connect to Redis.")
        print("Run: docker compose up -d")
        sys.exit(1)


def calculate_urgency(event: dict) -> dict:
    """
    Determine how urgent this exception is based on:
    - Time remaining until SLA deadline
    - Exception severity (auto_resolve vs escalate)
    - Package priority (medical > priority > standard)
    - Remaining stops affected
    """
    now = datetime.now()

    try:
        sla = datetime.fromisoformat(event["sla_deadline"])
        eta = datetime.fromisoformat(event["eta"])
        minutes_until_sla = (sla - now).total_seconds() / 60
        eta_vs_sla = (sla - eta).total_seconds() / 60  # positive = on time
    except (ValueError, KeyError):
        minutes_until_sla = 999
        eta_vs_sla = 999

    remaining_stops = int(event.get("remaining_stops", 0))
    priority = event.get("package_priority", "standard")
    severity = event.get("severity", "auto_resolve")

    # Score urgency (higher = more urgent)
    score = 0

    # SLA pressure
    if minutes_until_sla < 30:
        score += 50
    elif minutes_until_sla < 60:
        score += 30
    elif minutes_until_sla < 120:
        score += 10

    # Already going to miss SLA
    if eta_vs_sla < 0:
        score += 40

    # Package priority
    if priority == "medical":
        score += 30
    elif priority == "priority":
        score += 15

    # Severity from triage
    if severity == "escalate":
        score += 20

    # Cascade impact (breakdown affects many stops)
    if remaining_stops > 5:
        score += 15
    elif remaining_stops > 2:
        score += 5

    # Classify
    if score >= 60:
        urgency = "CRITICAL"
    elif score >= 30:
        urgency = "HIGH"
    elif score >= 15:
        urgency = "MEDIUM"
    else:
        urgency = "LOW"

    return {
        "urgency": urgency,
        "score": score,
        "minutes_until_sla": round(minutes_until_sla, 1),
        "eta_vs_sla_minutes": round(eta_vs_sla, 1),
        "recommended_action": recommend_action(event, urgency),
    }


def recommend_action(event: dict, urgency: str) -> str:
    """Suggest an action based on exception type and urgency."""

    exc_type = event.get("exception_type", "unknown")

    actions = {
        "address_inaccessible": {
            "CRITICAL": "Reassign to nearest available driver immediately",
            "HIGH": "Contact customer for access instructions, set 10min timer",
            "MEDIUM": "Contact customer for access instructions",
            "LOW": "Contact customer, attempt redelivery next pass",
        },
        "customer_absent": {
            "CRITICAL": "Leave at safe location if possible, notify customer",
            "HIGH": "Attempt phone call, wait 5 minutes max",
            "MEDIUM": "Leave notification, schedule redelivery",
            "LOW": "Leave notification, schedule redelivery",
        },
        "traffic_delay": {
            "CRITICAL": "Reroute immediately via OSRM alternate path",
            "HIGH": "Reroute via OSRM, recalculate all downstream ETAs",
            "MEDIUM": "Reroute via OSRM",
            "LOW": "Monitor, reroute if delay exceeds 15 minutes",
        },
        "weather_disruption": {
            "CRITICAL": "Pause deliveries in affected zone, reassign to clear zones",
            "HIGH": "Reroute around affected area",
            "MEDIUM": "Reduce speed expectations, update ETAs",
            "LOW": "Monitor conditions",
        },
        "vehicle_breakdown": {
            "CRITICAL": "Dispatch backup driver, transfer all remaining packages",
            "HIGH": "Dispatch backup driver, transfer all remaining packages",
            "MEDIUM": "Dispatch backup driver",
            "LOW": "Dispatch backup driver",
        },
        "misloaded_package": {
            "CRITICAL": "Dispatch package from hub via emergency courier",
            "HIGH": "Dispatch package from hub, update customer ETA",
            "MEDIUM": "Schedule package for next dispatch window",
            "LOW": "Schedule package for next dispatch window",
        },
    }

    return actions.get(exc_type, {}).get(urgency, "Escalate to dispatcher")


def format_event_log(event: dict, triage: dict, event_id: str) -> str:
    """Format a human-readable log line for the processed event."""

    urgency_colors = {
        "CRITICAL": "🔴",
        "HIGH": "🟠",
        "MEDIUM": "🟡",
        "LOW": "🟢",
    }

    icon = urgency_colors.get(triage["urgency"], "⚪")

    lines = [
        f"  {icon} {triage['urgency']} (score: {triage['score']})",
        f"     Event:     {event_id}",
        f"     Delivery:  {event['delivery_id']} | Driver: {event['driver_id']}",
        f"     Type:      {event['exception_type']} ({event['severity']})",
        f"     Business:  {event['business_name']}",
        f"     Address:   {event['address']}",
        f"     SLA in:    {triage['minutes_until_sla']} min | ETA margin: {triage['eta_vs_sla_minutes']} min",
        f"     Priority:  {event['package_priority']}",
        f"     Action:    {triage['recommended_action']}",
        f"     Remaining: {event['remaining_stops']} stops on this route",
        "",
    ]
    return "\n".join(lines)


def process_pending(r: redis.Redis, consumer_name: str) -> int:
    """Process any events that were read but not acknowledged (crash recovery)."""

    pending = r.xreadgroup(
        CONSUMER_GROUP, consumer_name, {STREAM_NAME: "0"}, count=100
    )

    if not pending or not pending[0][1]:
        return 0

    count = 0
    for stream_name, messages in pending:
        for msg_id, event in messages:
            triage = calculate_urgency(event)
            print(format_event_log(event, triage, msg_id))
            r.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
            count += 1

    return count


def consume_events(r: redis.Redis, consumer_name: str):
    """Main consumer loop. Blocks waiting for new events."""

    print(f"  Listening for events on '{STREAM_NAME}'...")
    print(f"  Consumer: {consumer_name}")
    print(f"  Press Ctrl+C to stop.\n")

    processed = 0

    try:
        while True:
            # xreadgroup blocks until an event arrives
            # ">" means only give me new (undelivered) events
            result = r.xreadgroup(
                CONSUMER_GROUP,
                consumer_name,
                {STREAM_NAME: ">"},
                count=1,
                block=5000,  # block for 5 seconds, then check again
            )

            if not result:
                continue  # timeout, loop back

            for stream_name, messages in result:
                for msg_id, event in messages:
                    processed += 1

                    # Triage the event
                    triage = calculate_urgency(event)

                    # Log it
                    print(format_event_log(event, triage, msg_id))

                    # Acknowledge (in production, only after agents complete)
                    r.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)

    except KeyboardInterrupt:
        print(f"\n  Stopped. Processed {processed} events.")


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Consume delivery exception events from Redis")
    parser.add_argument("--consumer", default="agent-1",
                        help="Consumer name within the group (default: agent-1)")
    args = parser.parse_args()

    print("=" * 60)
    print("  Delivery Exception Event Consumer")
    print("=" * 60)
    print()

    # Connect
    print("[1/3] Connecting to Redis...")
    r = connect_redis()
    print("  Connected.\n")

    # Check for pending (unacknowledged) events from previous runs
    print("[2/3] Checking for pending events...")
    pending_count = process_pending(r, args.consumer)
    if pending_count:
        print(f"  Recovered and processed {pending_count} pending events.\n")
    else:
        print("  No pending events.\n")

    # Consume
    print("[3/3] Starting consumer...")
    consume_events(r, args.consumer)


if __name__ == "__main__":
    main()