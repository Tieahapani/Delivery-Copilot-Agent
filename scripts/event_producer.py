"""
event_producer.py

Reads the delivery manifest, finds every stop with an exception,
and publishes each as an event to a Redis Stream.

Two modes:
  --mode batch       Publish all exceptions at once (for testing)
  --mode simulate    Publish one event every N seconds (mimics real-time)

Prerequisites:
  - Redis running: docker compose up -d
  - pip install redis
  - data/manifest.json exists (run generate_manifest.py first)

Usage:
  python scripts/event_producer.py
  python scripts/event_producer.py --mode simulate --interval 3
"""

import argparse
import json
import os
import sys
import time
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


def setup_stream(r: redis.Redis):
    """Create the consumer group if it doesn't exist."""
    try:
        r.xgroup_create(STREAM_NAME, CONSUMER_GROUP, id="0", mkstream=True)
        print(f"  [setup] Created consumer group '{CONSUMER_GROUP}'")
    except redis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            print(f"  [setup] Consumer group '{CONSUMER_GROUP}' already exists")
        else:
            raise


def load_manifest(manifest_path: str) -> dict:
    """Load the delivery manifest JSON."""
    if not os.path.exists(manifest_path):
        print(f"ERROR: Manifest not found at {manifest_path}")
        print("Run: python scripts/generate_manifest.py")
        sys.exit(1)

    with open(manifest_path) as f:
        return json.load(f)


def extract_exception_events(manifest: dict) -> list:
    """Pull every delivery with an exception into a flat event list."""
    events = []

    for driver in manifest["drivers"]:
        driver_id = driver["driver_id"]
        total_stops = driver["total_stops"]

        for i, stop in enumerate(driver["stops"]):
            if stop["exception"] is None:
                continue

            event = {
                "delivery_id": stop["delivery_id"],
                "driver_id": driver_id,
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
                "stop_number": str(i + 1),
                "total_stops": str(total_stops),
                "remaining_stops": str(total_stops - i - 1),
                "published_at": datetime.now().isoformat(),
            }
            events.append(event)

    return events


def publish_batch(r: redis.Redis, events: list):
    """Publish all events at once."""
    print(f"\n  Publishing {len(events)} events in batch mode...\n")

    for event in events:
        msg_id = r.xadd(STREAM_NAME, event)
        severity_marker = "!!" if event["severity"] == "escalate" else "  "
        print(
            f"  {severity_marker} {msg_id} | {event['delivery_id']} | "
            f"{event['driver_id']} | {event['exception_type']:24s} | "
            f"{event['business_name'][:30]}"
        )

    print(f"\n  Done. {len(events)} events on stream '{STREAM_NAME}'")


def publish_simulate(r: redis.Redis, events: list, interval: float):
    """Publish events one at a time with a delay between each."""
    print(f"\n  Simulating real-time delivery exceptions ({interval}s between events)...")
    print(f"  Press Ctrl+C to stop.\n")

    try:
        for i, event in enumerate(events):
            event["published_at"] = datetime.now().isoformat()
            msg_id = r.xadd(STREAM_NAME, event)

            severity_marker = "!!" if event["severity"] == "escalate" else "  "
            print(
                f"  {severity_marker} [{i+1}/{len(events)}] {msg_id} | "
                f"{event['delivery_id']} | {event['driver_id']} | "
                f"{event['exception_type']:24s} | {event['severity']:12s} | "
                f"{event['business_name'][:30]}"
            )

            if i < len(events) - 1:
                time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\n  Stopped. Published {i+1}/{len(events)} events.")
        return

    print(f"\n  Done. All {len(events)} events published.")


def print_summary(events: list):
    """Print a breakdown of what will be published."""
    breakdown = {}
    severity_counts = {"auto_resolve": 0, "escalate": 0}
    drivers_affected = set()

    for e in events:
        t = e["exception_type"]
        breakdown[t] = breakdown.get(t, 0) + 1
        severity_counts[e["severity"]] += 1
        drivers_affected.add(e["driver_id"])

    print("=" * 60)
    print("  Event Producer Summary")
    print("=" * 60)
    print(f"  Total events:       {len(events)}")
    print(f"  Drivers affected:   {len(drivers_affected)}")
    print(f"  Auto-resolvable:    {severity_counts['auto_resolve']}")
    print(f"  Needs escalation:   {severity_counts['escalate']}")
    print(f"  Breakdown:")
    for exc_type, count in sorted(breakdown.items()):
        print(f"    {exc_type}: {count}")
    print("=" * 60)


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Publish delivery exceptions to Redis Stream")
    parser.add_argument("--mode", choices=["batch", "simulate"], default="batch",
                        help="batch: all at once, simulate: one per interval (default: batch)")
    parser.add_argument("--interval", type=float, default=3.0,
                        help="Seconds between events in simulate mode (default: 3)")
    parser.add_argument("--flush", action="store_true",
                        help="Delete existing stream before publishing")
    args = parser.parse_args()

    print("=" * 60)
    print("  Delivery Exception Event Producer")
    print("=" * 60)
    print()

    # Connect to Redis
    print("[1/4] Connecting to Redis...")
    r = connect_redis()
    print("  Connected.\n")

    # Flush if requested
    if args.flush:
        r.delete(STREAM_NAME)
        print(f"  [flush] Deleted stream '{STREAM_NAME}'\n")

    # Setup consumer group
    print("[2/4] Setting up stream...")
    setup_stream(r)
    print()

    # Load manifest
    manifest_path = os.path.join(os.path.dirname(__file__), "..", "data", "manifest.json")
    manifest_path = os.path.abspath(manifest_path)

    print("[3/4] Loading manifest...")
    manifest = load_manifest(manifest_path)
    events = extract_exception_events(manifest)
    print(f"  Found {len(events)} exception events across {len(manifest['drivers'])} drivers")
    print()

    # Print summary
    print_summary(events)
    print()

    # Publish
    print(f"[4/4] Publishing ({args.mode} mode)...")
    if args.mode == "batch":
        publish_batch(r, events)
    else:
        publish_simulate(r, events, args.interval)


if __name__ == "__main__":
    main()