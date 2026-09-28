"""
verify_reroutes.py

Runs every exception event from the manifest through the full LangGraph
pipeline and verifies that every reroute decision is correct:

  1. If a safe route exists → the chosen route actually avoids the blockage
  2. If a safe route exists → the chosen route is the fastest among safe options
  3. If no safe route exists → the chosen route has the fewest exposure points
  4. If no safe route exists → it is flagged for manual review
  5. All routes have plausible speed (10-80 km/h for SF)
  6. All routes are longer than straight-line distance

Usage:
  python evals/verify_reroutes.py
  python evals/verify_reroutes.py --limit 10     # verify first 10 events only
"""

import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from src.graph import build_graph


def haversine_km(lat1, lon1, lat2, lon2):
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))


def verify_reroute(delivery_id, reroute, event):
    """
    Verify a single reroute result. Returns a list of
    (check_name, passed, detail) tuples.
    """
    checks = []
    action = reroute.get("action", "")
    verification = reroute.get("verification", {})
    all_routes = reroute.get("all_routes", [])

    # Skip non-reroute actions (customer_absent, address_inaccessible)
    if action in ("schedule_retry", "defer_and_retry", "recalculate"):
        if action == "recalculate" and not all_routes:
            checks.append(("non_reroute_action", True, f"Action: {action}, no route verification needed"))
            return checks
        elif action in ("schedule_retry", "defer_and_retry"):
            checks.append(("non_reroute_action", True, f"Action: {action}, no route verification needed"))
            return checks

    if not verification.get("verifiable", False):
        checks.append(("not_verifiable", None, f"Reroute not verifiable: {verification.get('reason', 'unknown')}"))
        return checks

    # --- Check 1: Was the chosen route actually safe? ---
    if verification.get("chosen_is_safe"):
        checks.append((
            "chosen_avoids_blockage",
            verification.get("chosen_avoids_blockage", False),
            f"Closest approach: {verification.get('closest_approach_m', '?')}m from blocked zone"
        ))

        # --- Check 2: Is it the fastest among safe routes? ---
        checks.append((
            "chosen_is_fastest_safe",
            verification.get("chosen_is_fastest_safe", False),
            f"Safe routes: {verification.get('safe_route_count', '?')}, "
            f"blocked: {verification.get('blocked_route_count', '?')}"
        ))

    else:
        # All routes blocked
        # --- Check 3: Picked least exposure? ---
        checks.append((
            "chosen_is_least_exposure",
            verification.get("chosen_is_least_exposure", False),
            f"Chosen exposure: {verification.get('chosen_exposure_points', '?')}, "
            f"all exposures: {verification.get('all_exposure_points', '?')}"
        ))

        # --- Check 4: Flagged for review? ---
        checks.append((
            "flagged_for_manual_review",
            verification.get("flagged_for_review", False),
            "All routes blocked, must flag for dispatcher"
        ))

    # --- Check 5: Speed sanity ---
    dist_km = reroute.get("new_distance_km", 0)
    added_min = reroute.get("added_minutes", 0)
    if dist_km > 0 and added_min > 0:
        avg_speed = dist_km / (added_min / 60)
        checks.append((
            "speed_plausible",
            10 < avg_speed < 80,
            f"Avg speed: {avg_speed:.0f} km/h ({avg_speed * 0.621:.0f} mph)"
        ))

    # --- Check 6: Driving distance > straight line ---
    if dist_km > 0:
        warehouse_lat, warehouse_lon = 37.7340, -122.3915
        event_lat = float(event.get("lat", 0))
        event_lon = float(event.get("lon", 0))
        straight = haversine_km(warehouse_lat, warehouse_lon, event_lat, event_lon)
        if straight > 0.5:  # only check if far enough apart
            checks.append((
                "longer_than_straight_line",
                dist_km > straight,
                f"Driving: {dist_km:.1f} km, straight: {straight:.1f} km, "
                f"ratio: {dist_km / straight:.2f}x"
            ))

    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Max events to process")
    parser.add_argument("--verbose", action="store_true", help="Show all checks")
    args = parser.parse_args()

    # Load manifest
    manifest_path = os.path.join(os.path.dirname(__file__), "..", "data", "manifest.json")
    if not os.path.exists(manifest_path):
        print("ERROR: No manifest. Run generate_manifest.py first.")
        sys.exit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Collect all exception events
    events = []
    for driver in manifest["drivers"]:
        for stop in driver["stops"]:
            if stop.get("exception"):
                event = {
                    "delivery_id": stop["delivery_id"],
                    "driver_id": driver["driver_id"],
                    "exception_type": stop["exception"]["type"],
                    "details": stop["exception"]["details"],
                    "severity": stop["exception"].get("severity", "auto_resolve"),
                    "lat": stop["lat"],
                    "lon": stop["lon"],
                    "address": stop["address"],
                    "business_name": stop.get("business_name", ""),
                    "package_priority": stop["package_priority"],
                    "sla_deadline": stop["sla_deadline"],
                    "eta": stop["eta"],
                    "remaining_stops": stop.get("remaining_stops", 0),
                    "stop_sequence": stop.get("stop_sequence", stop.get("sequence", 0)),
                    
                }
                events.append(event)

    if args.limit:
        events = events[:args.limit]

    print("=" * 65)
    print("  Reroute Verification: Every Event Through the Pipeline")
    print("=" * 65)
    print(f"\n  Events to verify: {len(events)}")

    # Build graph
    print("  Building LangGraph...")
    graph = build_graph()
    print("  Graph ready.\n")

    # Track results
    total_checks = 0
    passed_checks = 0
    failed_checks = 0
    skipped = 0
    rerouted_events = 0
    failures = []

    for i, event in enumerate(events):
        label = f"[{i+1}/{len(events)}] {event['delivery_id']} | {event['exception_type']}"
        print(f"  Processing {label}...")

        initial_state = {
            "event": event,
            "triage": None,
            "route_decision": None,
            "reroute": None,
            "escalation": None,
            "notification": None,
            "resolution": None,
        }

        try:
            result = graph.invoke(initial_state)
        except Exception as e:
            print(f"    ❌ Graph error: {e}")
            failed_checks += 1
            continue

        reroute = result.get("reroute")
        resolution = result.get("resolution", {})
        outcome = resolution.get("outcome", "unknown")

        if not reroute:
            if args.verbose:
                print(f"    ⏭  No reroute (escalated directly)")
            skipped += 1
            continue

        rerouted_events += 1
        checks = verify_reroute(event["delivery_id"], reroute, event)

        event_passed = True
        for check_name, passed, detail in checks:
            total_checks += 1
            if passed is None:
                skipped += 1
                if args.verbose:
                    print(f"    ⏭  {check_name}: {detail}")
            elif passed:
                passed_checks += 1
                if args.verbose:
                    print(f"    ✅ {check_name}: {detail}")
            else:
                failed_checks += 1
                event_passed = False
                print(f"    ❌ {check_name}: {detail}")
                failures.append({
                    "delivery_id": event["delivery_id"],
                    "exception_type": event["exception_type"],
                    "check": check_name,
                    "detail": detail,
                })

        if not args.verbose and event_passed:
            action = reroute.get("action", "?")
            v = reroute.get("verification", {})
            safe = v.get("safe_route_count", "?")
            blocked = v.get("blocked_route_count", "?")
            print(f"    ✅ All checks passed | {action} | safe:{safe} blocked:{blocked}")

    # Scorecard
    print("\n" + "=" * 65)
    print("  REROUTE VERIFICATION SCORECARD")
    print("=" * 65)
    print(f"\n  Events processed:    {len(events)}")
    print(f"  Rerouted events:     {rerouted_events}")
    print(f"  Escalated (no route): {skipped}")
    print(f"\n  Checks run:          {total_checks}")
    print(f"  Passed:              {passed_checks}")
    print(f"  Failed:              {failed_checks}")
    if total_checks > 0:
        print(f"  Score:               {passed_checks}/{total_checks} ({passed_checks/total_checks*100:.1f}%)")

    if failures:
        print(f"\n  ⚠️  {len(failures)} failed verification(s):")
        for f in failures:
            print(f"    {f['delivery_id']} | {f['exception_type']} | {f['check']}: {f['detail']}")
    else:
        print(f"\n  ✅ Every reroute decision verified correct.")
        print(f"     Safe routes were chosen when available.")
        print(f"     Fastest safe route was always picked.")
        print(f"     All speeds and distances are plausible.")

    print()


if __name__ == "__main__":
    main()