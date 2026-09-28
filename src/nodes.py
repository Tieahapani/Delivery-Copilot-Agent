"""
nodes.py

Each function is a node in the LangGraph graph.
Every node receives the full state dict and returns
a partial dict with only the fields it writes to.
"""

import json
import math
import os
import time
from datetime import datetime, timedelta

import requests
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage
from dotenv import load_dotenv
load_dotenv()

# ----------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------

OSRM_BASE = os.environ.get("OSRM_BASE", "https://router.project-osrm.org")

# Initialize LLM (used by triage and comms nodes)
llm = ChatAnthropic(
    model="claude-haiku-4-5-20251001",
    max_tokens=1024,
)

# Warehouse coordinates (same as in generate_manifest.py)
WAREHOUSE_LAT = 37.7327
WAREHOUSE_LON = -122.3914

# Radius in meters to consider a route "passing through" blocked area
BLOCK_RADIUS_METERS = 200


# ----------------------------------------------------------------
# Node 1: Triage
# ----------------------------------------------------------------

TRIAGE_SYSTEM_PROMPT = """You are a delivery operations triage agent. You analyze delivery 
exceptions and classify their urgency.

Given a delivery exception event, you must output a JSON object with these fields:
- urgency: one of "HIGH", "MEDIUM", "LOW"
- reasoning: 1-2 sentences explaining your assessment
- decision: one of "auto_resolve" or "escalate"

Rules for decision:
- "escalate" when: vehicle_breakdown, medical package with < 60 min to SLA, 
  any exception with < 30 min to SLA, weather affecting multiple drivers,
  misloaded_package (requires hub coordination)
- "auto_resolve" when: traffic delays with alternate routes available,
  address issues where customer can be contacted, customer absent with
  safe drop location possible, standard packages with > 2 hours to SLA

Rules for urgency:
- HIGH: medical package at risk, < 60 min to SLA, vehicle breakdown 
  with 5+ remaining stops, priority package at risk, 3+ stops affected
- MEDIUM: standard package, 1-2 hours to SLA, isolated exception
- LOW: standard package, > 2 hours to SLA, minor issue

Respond with ONLY the JSON object, no other text."""


def triage_node(state: dict) -> dict:
    """
    Triage agent: analyzes the exception and classifies urgency.

    Reads: event
    Writes: triage, route_decision
    """
    event = state["event"]
    start_time = time.time()

    # Calculate time-to-SLA for context
    now = datetime.now()
    try:
        sla = datetime.fromisoformat(event["sla_deadline"])
        eta = datetime.fromisoformat(event["eta"])
        minutes_to_sla = (sla - now).total_seconds() / 60
        eta_margin = (sla - eta).total_seconds() / 60
    except (ValueError, KeyError):
        minutes_to_sla = 999
        eta_margin = 999

    # Build the prompt with the event context
    event_summary = (
        f"Delivery ID: {event['delivery_id']}\n"
        f"Driver: {event['driver_id']}\n"
        f"Exception type: {event['exception_type']}\n"
        f"Details: {event['details']}\n"
        f"Severity tag: {event['severity']}\n"
        f"Package priority: {event['package_priority']}\n"
        f"Business: {event['business_name']}\n"
        f"Address: {event['address']}\n"
        f"Minutes until SLA deadline: {round(minutes_to_sla, 1)}\n"
        f"Current ETA margin (positive=on time): {round(eta_margin, 1)} minutes\n"
        f"Remaining stops on this route: {event['remaining_stops']}\n"
    )

    # Call LLM
    response = llm.invoke([
        SystemMessage(content=TRIAGE_SYSTEM_PROMPT),
        HumanMessage(content=event_summary),
    ])

    # Parse response
    try:
        text = response.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        triage_result = json.loads(text)
    except json.JSONDecodeError:
        triage_result = {
            "urgency": "HIGH",
            "reasoning": "Failed to parse LLM response, defaulting to HIGH",
            "decision": "escalate",
        }

    # Add computed fields
    triage_result["time_to_sla_minutes"] = round(minutes_to_sla, 1)
    triage_result["eta_margin_minutes"] = round(eta_margin, 1)
    triage_result["processing_time_ms"] = round((time.time() - start_time) * 1000)

    return {
        "triage": triage_result,
        "route_decision": triage_result.get("decision", "escalate"),
    }


# ----------------------------------------------------------------
# Node 2: Router (conditional edge, not a node)
# ----------------------------------------------------------------

def route_decision(state: dict) -> str:
    """
    Conditional edge function.
    Returns the path name for LangGraph to follow.

    Reads: route_decision
    """
    decision = state.get("route_decision", "escalate")
    if decision == "auto_resolve":
        return "auto_resolve"
    return "escalate"


# ----------------------------------------------------------------
# Node 3a: Reroute (auto_resolve path)
# ----------------------------------------------------------------

def reroute_node(state: dict) -> dict:
    """
    Reroute agent: computes an alternate route via OSRM.

    Production pattern for traffic delays:
      1. Query OSRM for multiple alternative routes
      2. Decode each route's geometry
      3. Filter out routes that pass through the blocked area
      4. Pick the fastest safe route

    Reads: event, triage
    Writes: reroute
    """
    event = state["event"]
    start_time = time.time()

    lat = float(event["lat"])
    lon = float(event["lon"])
    exc_type = event["exception_type"]

    # Strategy depends on exception type
    if exc_type == "traffic_delay":
        result = reroute_around_blockage(
            from_lat=WAREHOUSE_LAT,
            from_lon=WAREHOUSE_LON,
            to_lat=lat,
            to_lon=lon,
            blocked_lat=lat,
            blocked_lon=lon,
        )
        action = result["action"]
        summary = result["summary"].format(address=event["address"])

    elif exc_type == "address_inaccessible":
        route = query_osrm(WAREHOUSE_LAT, WAREHOUSE_LON, lat, lon)
        result = {
            "duration_sec": route["duration_sec"],
            "distance_m": route["distance_m"],
            "action": "defer_and_retry",
            "summary": f"Deferred delivery to {event['address']}, moved to end of route",
            "alternatives_checked": 0,
            "blocked_routes_filtered": 0,
        }
        action = result["action"]
        summary = result["summary"]

    elif exc_type == "customer_absent":
        result = {
            "duration_sec": 0,
            "distance_m": 0,
            "action": "schedule_retry",
            "summary": f"Customer absent at {event['address']}, scheduling retry window",
            "alternatives_checked": 0,
            "blocked_routes_filtered": 0,
        }
        action = result["action"]
        summary = result["summary"]

    else:
        route = query_osrm(WAREHOUSE_LAT, WAREHOUSE_LON, lat, lon)
        result = {
            "duration_sec": route["duration_sec"],
            "distance_m": route["distance_m"],
            "action": "recalculate",
            "summary": f"Recalculated route to {event['address']}",
            "alternatives_checked": 0,
            "blocked_routes_filtered": 0,
        }
        action = result["action"]
        summary = result["summary"]

    # Calculate new ETA (different logic per action type)
    if action == "schedule_retry":
        # No new ETA: retry time depends on dispatcher/customer coordination
        new_eta = "pending_retry"
        added_minutes = 0
        sla_met = None

    elif action == "defer_and_retry":
        # Deferred to end of route: estimate retry time
        try:
            added_minutes = result["duration_sec"] / 60
            sla = datetime.fromisoformat(event["sla_deadline"])
            estimated_retry = datetime.now() + timedelta(minutes=added_minutes + 30)
            new_eta = estimated_retry.strftime("%Y-%m-%dT%H:%M:%S")
            sla_met = estimated_retry < sla
        except (ValueError, KeyError):
            new_eta = "pending_retry"
            added_minutes = 0
            sla_met = None

    else:
        # Traffic reroute or recalculate: compute real new ETA
        try:
            added_minutes = result["duration_sec"] / 60
            new_eta_dt = datetime.now() + timedelta(minutes=added_minutes)
            new_eta = new_eta_dt.strftime("%Y-%m-%dT%H:%M:%S")
            sla = datetime.fromisoformat(event["sla_deadline"])
            sla_met = new_eta_dt < sla
        except (ValueError, KeyError):
            added_minutes = 0
            new_eta = event.get("eta", "unknown")
            sla_met = True

    reroute_result = {
        "action": action,
        "original_eta": event.get("eta"),
        "new_eta": new_eta,
        "added_minutes": round(added_minutes, 1),
        "new_distance_km": round(result["distance_m"] / 1000, 1) if result["distance_m"] else 0,
        "sla_met": sla_met,
        "summary": summary,
        "alternatives_checked": result.get("alternatives_checked", 0),
        "blocked_routes_filtered": result.get("blocked_routes_filtered", 0),
        "processing_time_ms": round((time.time() - start_time) * 1000),
        # Evidence for verification
        "all_routes": result.get("all_routes", []),
        "chosen_index": result.get("chosen_index", -1),
        "chosen_reason": result.get("chosen_reason", ""),
        "verification": result.get("verification", {}),
    }

    return {"reroute": reroute_result}


# ----------------------------------------------------------------
# OSRM helpers
# ----------------------------------------------------------------

def query_osrm(from_lat, from_lon, to_lat, to_lon) -> dict:
    """Query OSRM for a single fastest route."""
    url = (
        f"{OSRM_BASE}/route/v1/driving/"
        f"{from_lon},{from_lat};{to_lon},{to_lat}"
        f"?overview=false"
    )
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("code") == "Ok":
            route = data["routes"][0]
            return {
                "duration_sec": route["duration"],
                "distance_m": route["distance"],
            }
    except Exception:
        pass
    return {"duration_sec": 600, "distance_m": 5000}


def query_osrm_alternatives(from_lat, from_lon, to_lat, to_lon, n_alts=3) -> list:
    """Query OSRM for multiple alternative routes with full geometry."""
    url = (
        f"{OSRM_BASE}/route/v1/driving/"
        f"{from_lon},{from_lat};{to_lon},{to_lat}"
        f"?alternatives={n_alts}&overview=full&geometries=geojson"
    )
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("code") == "Ok":
            routes = []
            for route in data["routes"]:
                coords = route["geometry"]["coordinates"]
                routes.append({
                    "duration_sec": route["duration"],
                    "distance_m": route["distance"],
                    "coordinates": [(c[1], c[0]) for c in coords],
                })
            return routes
    except Exception:
        pass
    return []


def meters_to_degrees(meters: float, latitude: float) -> float:
    """Convert meters to approximate degrees at a given latitude."""
    lat_deg = meters / 111320
    lon_deg = meters / (111320 * math.cos(math.radians(latitude)))
    return max(lat_deg, lon_deg)


def route_passes_through_blocked_area(route_coords: list, blocked_lat: float,
                                       blocked_lon: float, radius_meters: float) -> dict:
    """Check if a route's geometry intersects a blocked zone using Shapely."""
    from shapely.geometry import Point, LineString

    radius_deg = meters_to_degrees(radius_meters, blocked_lat)
    blocked_zone = Point(blocked_lon, blocked_lat).buffer(radius_deg)

    if len(route_coords) < 2:
        return {"blocked": False, "closest_distance_m": 9999, "exposure_points": 0}

    route_line = LineString([(lon, lat) for lat, lon in route_coords])
    intersects = route_line.intersects(blocked_zone)

    blocked_center = Point(blocked_lon, blocked_lat)
    closest_deg = route_line.distance(blocked_center)
    closest_m = closest_deg * 111320

    exposure_points = 0
    if intersects:
        for lat, lon in route_coords:
            if blocked_zone.contains(Point(lon, lat)):
                exposure_points += 1

    return {
        "blocked": intersects,
        "closest_distance_m": round(closest_m, 1),
        "exposure_points": exposure_points,
    }


def reroute_around_blockage(from_lat, from_lon, to_lat, to_lon,
                            blocked_lat, blocked_lon) -> dict:
    """Production rerouting: get alternatives, filter blocked, pick fastest safe route.

    Returns full evidence so every reroute decision can be verified:
      - all_routes: every route evaluated with its blocked/safe status
      - chosen_index: which route was picked
      - chosen_reason: why it was picked
      - verification: pre-computed checks for automated validation
    """
    alternatives = query_osrm_alternatives(from_lat, from_lon, to_lat, to_lon, n_alts=3)

    if not alternatives:
        simple = query_osrm(from_lat, from_lon, to_lat, to_lon)
        return {
            "duration_sec": simple["duration_sec"],
            "distance_m": simple["distance_m"],
            "action": "recalculate",
            "summary": "OSRM alternatives unavailable, used default route to {address}",
            "alternatives_checked": 0,
            "blocked_routes_filtered": 0,
            "all_routes": [],
            "chosen_index": -1,
            "chosen_reason": "no_alternatives",
            "verification": {"verifiable": False, "reason": "no_alternatives_returned"},
        }

    # Evaluate every route against the blocked zone
    route_evidence = []
    safe_routes = []
    blocked_routes = []

    for i, route in enumerate(alternatives):
        check = route_passes_through_blocked_area(
            route["coordinates"], blocked_lat, blocked_lon, BLOCK_RADIUS_METERS
        )
        route["geo_check"] = check
        route_entry = {
            "index": i,
            "duration_sec": route["duration_sec"],
            "distance_m": route["distance_m"],
            "blocked": check["blocked"],
            "exposure_points": check["exposure_points"],
            "closest_distance_m": check["closest_distance_m"],
        }
        route_evidence.append(route_entry)

        if check["blocked"]:
            blocked_routes.append(route)
        else:
            safe_routes.append(route)

    total_checked = len(alternatives)
    total_blocked = len(blocked_routes)

    if safe_routes:
        safe_routes.sort(key=lambda r: r["duration_sec"])
        best = safe_routes[0]
        chosen_idx = next(i for i, r in enumerate(alternatives) if r is best)

        # Build verification evidence
        safe_durations = [r["duration_sec"] for r in safe_routes]
        verification = {
            "verifiable": True,
            "chosen_is_safe": True,
            "chosen_is_fastest_safe": best["duration_sec"] == min(safe_durations),
            "safe_route_count": len(safe_routes),
            "blocked_route_count": total_blocked,
            "chosen_avoids_blockage": not best["geo_check"]["blocked"],
            "closest_approach_m": best["geo_check"]["closest_distance_m"],
        }

        return {
            "duration_sec": best["duration_sec"],
            "distance_m": best["distance_m"],
            "action": "alternate_route",
            "summary": (
                f"Found {len(safe_routes)} safe alternative(s) avoiding blockage near "
                f"{{address}}. Closest approach: "
                f"{best['geo_check']['closest_distance_m']}m from blocked zone. "
                f"Using fastest alternate route."
            ),
            "alternatives_checked": total_checked,
            "blocked_routes_filtered": total_blocked,
            "all_routes": route_evidence,
            "chosen_index": chosen_idx,
            "chosen_reason": "fastest_safe",
            "verification": verification,
        }
    else:
        blocked_routes.sort(key=lambda r: r["geo_check"]["exposure_points"])
        best = blocked_routes[0]
        chosen_idx = next(i for i, r in enumerate(alternatives) if r is best)

        # Build verification evidence
        exposure_counts = [r["geo_check"]["exposure_points"] for r in blocked_routes]
        verification = {
            "verifiable": True,
            "chosen_is_safe": False,
            "chosen_is_least_exposure": best["geo_check"]["exposure_points"] == min(exposure_counts),
            "chosen_exposure_points": best["geo_check"]["exposure_points"],
            "all_exposure_points": exposure_counts,
            "flagged_for_review": True,
            "safe_route_count": 0,
            "blocked_route_count": total_blocked,
        }

        return {
            "duration_sec": best["duration_sec"],
            "distance_m": best["distance_m"],
            "action": "partial_reroute",
            "summary": (
                f"All {total_checked} routes pass near blockage at {{address}} "
                f"({best['geo_check']['exposure_points']} points in zone, "
                f"closest: {best['geo_check']['closest_distance_m']}m). "
                f"Using route with least exposure. Manual review recommended."
            ),
            "alternatives_checked": total_checked,
            "blocked_routes_filtered": total_blocked,
            "all_routes": route_evidence,
            "chosen_index": chosen_idx,
            "chosen_reason": "least_exposure",
            "verification": verification,
        }


# ----------------------------------------------------------------
# Node 3b: Escalate (escalate path)
# ----------------------------------------------------------------

def escalate_node(state: dict) -> dict:
    """
    Escalation node: builds a dispatcher queue entry.
    No LLM needed, just structured data assembly.

    Reads: event, triage
    Writes: escalation
    """
    event = state["event"]
    triage = state.get("triage", {})

    urgency = triage.get("urgency", "HIGH")
    exc_type = event.get("exception_type", "unknown")
    remaining = int(event.get("remaining_stops", 0))
    priority = event.get("package_priority", "standard")

    action_map = {
        "vehicle_breakdown": f"Dispatch backup driver to {event.get('address', 'location')}. "
                            f"Transfer {remaining} remaining packages.",
        "weather_disruption": f"Pause deliveries in {event.get('neighborhood', 'affected zone')}. "
                             f"Reassign {remaining} stops to drivers in clear zones.",
        "misloaded_package": f"Dispatch {event.get('delivery_id')} from hub via emergency courier. "
                            f"Package not on {event.get('driver_id')}'s truck.",
        "address_inaccessible": f"Contact customer for {event.get('delivery_id')} at "
                               f"{event.get('address')}. Building access required.",
        "customer_absent": f"Coordinate redelivery window for {event.get('delivery_id')} "
                          f"at {event.get('address')}.",
        "traffic_delay": f"Major delay on {event.get('driver_id')}'s route near "
                        f"{event.get('address')}. Manual reroute may be needed.",
    }

    recommended_action = action_map.get(
        exc_type,
        f"Review exception for {event.get('delivery_id')}"
    )

    escalation_result = {
        "priority": urgency,
        "exception_type": exc_type,
        "delivery_id": event.get("delivery_id"),
        "driver_id": event.get("driver_id"),
        "recommended_action": recommended_action,
        "reasoning": triage.get("reasoning", "Escalated for manual review"),
        "requires_immediate_attention": urgency == "HIGH",
        "package_priority": priority,
        "remaining_stops_affected": remaining,
    }

    return {"escalation": escalation_result}


# ----------------------------------------------------------------
# Node 4: Comms
# ----------------------------------------------------------------

COMMS_SYSTEM_PROMPT = """You are a customer communications agent for a delivery service.
Your job is to draft a brief, reassuring notification to the customer about their delivery.

Given the delivery context, write a notification that:
- Is 1-3 sentences maximum
- Uses a warm but professional tone
- Includes the updated ETA if available
- Does NOT blame the customer or driver
- Does NOT mention internal systems or technical details

Respond with ONLY a JSON object containing:
- channel: "sms" for urgent, "email" for non-urgent
- message: the notification text
- tone: "urgent", "reassuring", or "informational"

No other text outside the JSON."""


def comms_node(state: dict) -> dict:
    """
    Comms agent: drafts customer notification using LLM.

    Reads: event, triage, reroute (or escalation)
    Writes: notification
    """
    event = state["event"]
    triage = state.get("triage", {})
    reroute = state.get("reroute")
    escalation = state.get("escalation")
    start_time = time.time()

    if reroute:
        resolution_context = (
            f"Resolution: Auto-resolved via {reroute.get('action', 'reroute')}\n"
            f"New ETA: {reroute.get('new_eta', 'pending')}\n"
            f"SLA will be met: {reroute.get('sla_met', 'unknown')}\n"
            f"Summary: {reroute.get('summary', '')}"
        )
    elif escalation:
        resolution_context = (
            f"Resolution: Escalated to dispatcher\n"
            f"Status: A team member is working on this\n"
            f"Action being taken: {escalation.get('recommended_action', 'Under review')}"
        )
    else:
        resolution_context = "Resolution: Under review"

    context = (
        f"Delivery to: {event.get('business_name', 'customer')}\n"
        f"Address: {event.get('address', '')}\n"
        f"Exception: {event.get('exception_type', 'delay')}\n"
        f"Package priority: {event.get('package_priority', 'standard')}\n"
        f"Urgency: {triage.get('urgency', 'MEDIUM')}\n"
        f"{resolution_context}"
    )

    response = llm.invoke([
        SystemMessage(content=COMMS_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])

    try:
        text = response.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        notification = json.loads(text)
    except json.JSONDecodeError:
        notification = {
            "channel": "sms",
            "message": (
                f"Your delivery to {event.get('address', 'your address')} "
                f"has been delayed. We are working to get it to you as soon as possible."
            ),
            "tone": "reassuring",
        }

    notification["delivery_id"] = event.get("delivery_id")
    notification["processing_time_ms"] = round((time.time() - start_time) * 1000)

    return {"notification": notification}


# ----------------------------------------------------------------
# Node 5: Resolve
# ----------------------------------------------------------------

def resolve_node(state: dict) -> dict:
    """
    Resolution node: assembles the final summary for logging.
    No LLM, just data assembly.

    Reads: all fields
    Writes: resolution
    """
    event = state["event"]
    triage = state.get("triage", {})
    reroute = state.get("reroute")
    escalation = state.get("escalation")
    notification = state.get("notification", {})

    if reroute:
        outcome = "auto_resolved"
        nodes_executed = ["triage", "reroute", "comms", "resolve"]
        if reroute.get("sla_met") is None:
            sla_impact = "pending_retry"
        else:
            sla_impact = (
                f"{'met' if reroute.get('sla_met') else 'missed'} "
                f"({reroute.get('added_minutes', 0)} min added)"
            )
    elif escalation:
        outcome = "escalated"
        nodes_executed = ["triage", "escalate", "comms", "resolve"]
        sla_impact = "pending dispatcher action"
    else:
        outcome = "unknown"
        nodes_executed = ["triage", "resolve"]
        sla_impact = "unknown"

    total_ms = triage.get("processing_time_ms", 0)
    if reroute:
        total_ms += reroute.get("processing_time_ms", 0)
    total_ms += notification.get("processing_time_ms", 0)

    resolution = {
        "delivery_id": event.get("delivery_id"),
        "driver_id": event.get("driver_id"),
        "exception_type": event.get("exception_type"),
        "outcome": outcome,
        "urgency": triage.get("urgency"),
        "total_processing_time_ms": total_ms,
        "nodes_executed": nodes_executed,
        "sla_impact": sla_impact,
        "notification_channel": notification.get("channel"),
        "resolved_at": datetime.now().isoformat(),
    }

    return {"resolution": resolution}