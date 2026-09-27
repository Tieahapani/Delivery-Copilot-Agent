"""
feed_events.py

Reads exception events from the manifest and posts them to the
dashboard backend one at a time, simulating real-time delivery operations.

Tracks which events have been sent in a local JSON file so running
the script again only sends new/unsent events. Use --reset to clear
the tracker and resend everything.

Usage:
  python dashboard/feed_events.py
  python dashboard/feed_events.py --interval 5 --limit 20
  python dashboard/feed_events.py --reset
"""

import argparse
import json
import os
import sys
import time

import requests

SENT_TRACKER_PATH = os.path.join(os.path.dirname(__file__), ".sent_events.json")


def load_sent_tracker() -> set:
    """Load the set of delivery IDs that have already been sent."""
    if os.path.exists(SENT_TRACKER_PATH):
        with open(SENT_TRACKER_PATH) as f:
            return set(json.load(f))
    return set()


def save_sent_tracker(sent_ids: set):
    """Save the set of sent delivery IDs."""
    with open(SENT_TRACKER_PATH, "w") as f:
        json.dump(list(sent_ids), f)


def load_exception_events(manifest_path: str) -> list:
    """Extract all exception events from the manifest."""
    with open(manifest_path) as f:
        manifest = json.load(f)

    events = []
    for driver in manifest["drivers"]:
        for i, stop in enumerate(driver["stops"]):
            if stop["exception"] is None:
                continue

            event = {
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
                "remaining_stops": str(driver["total_stops"] - i - 1),
            }
            events.append(event)

    return events


def main():
    parser = argparse.ArgumentParser(description="Feed events to dashboard")
    parser.add_argument("--interval", type=float, default=8,
                        help="Seconds between events (default: 8, accounts for LLM processing)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max events to send (default: all)")
    parser.add_argument("--url", default="http://localhost:8000",
                        help="Dashboard backend URL")
    parser.add_argument("--reset", action="store_true",
                        help="Clear sent tracker and resend all events")
    args = parser.parse_args()

    manifest_path = os.path.join(os.path.dirname(__file__), "..", "data", "manifest.json")
    manifest_path = os.path.abspath(manifest_path)

    if not os.path.exists(manifest_path):
        print("ERROR: No manifest found. Run generate_manifest.py first.")
        sys.exit(1)

    # Handle reset
    if args.reset:
        if os.path.exists(SENT_TRACKER_PATH):
            os.remove(SENT_TRACKER_PATH)
        print("  Tracker reset. All events will be sent.\n")

    # Load events and filter out already-sent ones
    all_events = load_exception_events(manifest_path)
    sent_ids = load_sent_tracker()

    events = [e for e in all_events if e["delivery_id"] not in sent_ids]

    if not events:
        print(f"  All {len(all_events)} events have already been sent.")
        print(f"  Use --reset to resend them.")
        return

    if args.limit:
        events = events[:args.limit]

    skipped = len(all_events) - len(events) - (len(all_events) - len([e for e in all_events if e["delivery_id"] not in sent_ids]))
    print(f"  {len(sent_ids)} events already sent, {len(events)} new events to send")
    print(f"  Sending to {args.url}")
    print(f"  Interval: {args.interval}s between events")
    print(f"  Press Ctrl+C to stop.\n")

    sent_this_run = 0
    for i, event in enumerate(events):
        try:
            resp = requests.post(f"{args.url}/process_event", json=event, timeout=30)
            result = resp.json()
            outcome = result.get("outcome", "error")
            icon = "✅" if outcome == "auto_resolved" else "🔴" if outcome == "escalated" else "❌"
            print(f"  {icon} [{i+1}/{len(events)}] {event['delivery_id']} | {event['exception_type']:24s} | {outcome}")

            # Track as sent
            sent_ids.add(event["delivery_id"])
            save_sent_tracker(sent_ids)
            sent_this_run += 1

        except requests.ConnectionError:
            print(f"  ❌ [{i+1}/{len(events)}] Cannot reach {args.url}. Is the dashboard running?")
        except KeyboardInterrupt:
            print(f"\n  Stopped. Sent {sent_this_run} events this run ({len(sent_ids)} total).")
            save_sent_tracker(sent_ids)
            return
        except Exception as e:
            print(f"  ❌ [{i+1}/{len(events)}] Error: {e}")

        if i < len(events) - 1:
            time.sleep(args.interval)

    print(f"\n  Done. Sent {sent_this_run} events this run ({len(sent_ids)} total).")


if __name__ == "__main__":
    main()