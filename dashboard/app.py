"""
app.py

FastAPI backend for the dispatcher dashboard.
  - Loads manifest data for fleet overview
  - Reads exception events from Redis Stream
  - Processes each through the LangGraph copilot
  - Pushes results to connected browsers via WebSocket
  - Serves the dashboard HTML page

Usage:
  uvicorn dashboard.app:app --reload --port 8000
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.graph import build_graph

app = FastAPI(title="Delivery Copilot Dashboard")

# ----------------------------------------------------------------
# State
# ----------------------------------------------------------------

# Connected WebSocket clients
connected_clients: list[WebSocket] = []

# Dashboard state (in-memory, reset on restart)
dashboard_state = {
    "manifest": None,
    "fleet": {
        "active_drivers": 0,
        "total_deliveries": 0,
        "exceptions_active": 0,
        "sla_compliance": 100.0,
        "auto_resolved": 0,
        "pending_escalation": 0,
        "avg_resolution_ms": 0,
    },
    "events_processed": [],
    "escalation_queue": [],
    "activity_feed": [],
    "driver_locations": [],
    "exception_markers": [],
}

# Build graph once at startup
graph = None


# ----------------------------------------------------------------
# Startup
# ----------------------------------------------------------------

@app.on_event("startup")
async def startup():
    global graph
    print("  Building copilot graph...")
    graph = build_graph()
    print("  Graph compiled.")

    # Load manifest
    manifest_path = os.path.join(os.path.dirname(__file__), "..", "data", "manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            dashboard_state["manifest"] = json.load(f)
        load_fleet_data()
        print(f"  Manifest loaded: {dashboard_state['fleet']['total_deliveries']} deliveries")
    else:
        print("  WARNING: No manifest found. Run generate_manifest.py first.")


def load_fleet_data():
    """Extract fleet overview and driver positions from manifest."""
    manifest = dashboard_state["manifest"]
    if not manifest:
        return

    drivers = manifest["drivers"]
    all_stops = [s for d in drivers for s in d["stops"]]

    dashboard_state["fleet"]["active_drivers"] = len(drivers)
    dashboard_state["fleet"]["total_deliveries"] = len(all_stops)

    # Driver locations (use their first stop as initial position)
    driver_locs = []
    for driver in drivers:
        if driver["stops"]:
            first_stop = driver["stops"][0]
            driver_locs.append({
                "driver_id": driver["driver_id"],
                "lat": first_stop["lat"],
                "lon": first_stop["lon"],
                "total_stops": driver["total_stops"],
                "status": "active",
            })
    dashboard_state["driver_locations"] = driver_locs


# ----------------------------------------------------------------
# WebSocket
# ----------------------------------------------------------------

async def broadcast(message: dict):
    """Send a message to all connected dashboard clients."""
    disconnected = []
    for client in connected_clients:
        try:
            await client.send_json(message)
        except Exception:
            disconnected.append(client)
    for client in disconnected:
        connected_clients.remove(client)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    print(f"  Dashboard client connected ({len(connected_clients)} total)")

    # Send current state immediately
    await websocket.send_json({
        "type": "init",
        "fleet": dashboard_state["fleet"],
        "driver_locations": dashboard_state["driver_locations"],
        "escalation_queue": dashboard_state["escalation_queue"],
        "activity_feed": dashboard_state["activity_feed"][-50:],
        "exception_markers": dashboard_state["exception_markers"],
    })

    try:
        while True:
            # Listen for dispatcher actions (approve/dismiss)
            data = await websocket.receive_json()
            await handle_dispatcher_action(data)
    except WebSocketDisconnect:
        connected_clients.remove(websocket)
        print(f"  Dashboard client disconnected ({len(connected_clients)} total)")


async def handle_dispatcher_action(data: dict):
    """Handle approve/override/dismiss from the dispatcher."""
    action = data.get("action")
    delivery_id = data.get("delivery_id")

    if action == "approve":
        # Remove from escalation queue
        dashboard_state["escalation_queue"] = [
            e for e in dashboard_state["escalation_queue"]
            if e.get("delivery_id") != delivery_id
        ]
        dashboard_state["fleet"]["pending_escalation"] = len(dashboard_state["escalation_queue"])

        # Add to activity feed
        feed_entry = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "delivery_id": delivery_id,
            "outcome": "dispatcher_approved",
            "icon": "👤",
            "exception_type": data.get("exception_type", "unknown"),
            "processing_time": "manual",
        }
        dashboard_state["activity_feed"].append(feed_entry)

        await broadcast({
            "type": "escalation_resolved",
            "delivery_id": delivery_id,
            "fleet": dashboard_state["fleet"],
            "feed_entry": feed_entry,
        })

    elif action == "dismiss":
        dashboard_state["escalation_queue"] = [
            e for e in dashboard_state["escalation_queue"]
            if e.get("delivery_id") != delivery_id
        ]
        dashboard_state["fleet"]["pending_escalation"] = len(dashboard_state["escalation_queue"])

        await broadcast({
            "type": "escalation_resolved",
            "delivery_id": delivery_id,
            "fleet": dashboard_state["fleet"],
        })


# ----------------------------------------------------------------
# Event Processing
# ----------------------------------------------------------------

@app.post("/process_event")
async def process_event(event: dict):
    """
    Receive a delivery exception event, run it through the copilot graph,
    update dashboard state, and broadcast to all connected clients.
    """
    global graph

    # Run through LangGraph
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
        print(f"  Graph error: {e}")
        return {"error": str(e)}

    triage = result.get("triage", {})
    resolution = result.get("resolution", {})
    escalation = result.get("escalation")
    reroute = result.get("reroute")
    notification = result.get("notification", {})

    # Update fleet metrics
    dashboard_state["fleet"]["exceptions_active"] += 1

    if resolution.get("outcome") == "auto_resolved":
        dashboard_state["fleet"]["auto_resolved"] += 1
    elif resolution.get("outcome") == "escalated":
        # Add to escalation queue
        esc_entry = {
            "delivery_id": event.get("delivery_id"),
            "driver_id": event.get("driver_id"),
            "exception_type": event.get("exception_type"),
            "urgency": triage.get("urgency", "HIGH"),
            "business_name": event.get("business_name"),
            "address": event.get("address"),
            "package_priority": event.get("package_priority"),
            "sla_deadline": event.get("sla_deadline"),
            "remaining_stops": event.get("remaining_stops"),
            "recommended_action": escalation.get("recommended_action", "") if escalation else "",
            "reasoning": triage.get("reasoning", ""),
            "time_to_sla": triage.get("time_to_sla_minutes"),
        }
        dashboard_state["escalation_queue"].append(esc_entry)
        # Sort by urgency
        urgency_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        dashboard_state["escalation_queue"].sort(
            key=lambda x: urgency_order.get(x.get("urgency", "LOW"), 3)
        )
        dashboard_state["fleet"]["pending_escalation"] = len(dashboard_state["escalation_queue"])

    # Update SLA compliance
    total = dashboard_state["fleet"]["total_deliveries"]
    exceptions = dashboard_state["fleet"]["exceptions_active"]
    if total > 0:
        dashboard_state["fleet"]["sla_compliance"] = round(
            (total - exceptions) / total * 100, 1
        )

    # Update average resolution time
    processed = dashboard_state["events_processed"]
    processed.append(resolution.get("total_processing_time_ms", 0))
    dashboard_state["fleet"]["avg_resolution_ms"] = round(
        sum(processed) / len(processed)
    )

    # Add exception marker for the map
    marker = {
        "lat": float(event.get("lat", 0)),
        "lon": float(event.get("lon", 0)),
        "delivery_id": event.get("delivery_id"),
        "exception_type": event.get("exception_type"),
        "urgency": triage.get("urgency", "MEDIUM"),
        "outcome": resolution.get("outcome", "unknown"),
        "business_name": event.get("business_name", ""),
    }
    dashboard_state["exception_markers"].append(marker)

    # Add to activity feed
    outcome = resolution.get("outcome", "unknown")
    icon = "✅" if outcome == "auto_resolved" else "🔴"
    feed_entry = {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "delivery_id": event.get("delivery_id"),
        "outcome": outcome,
        "icon": icon,
        "exception_type": event.get("exception_type"),
        "processing_time": f"{resolution.get('total_processing_time_ms', 0) / 1000:.1f}s",
    }
    dashboard_state["activity_feed"].append(feed_entry)

    # Broadcast to all connected dashboards
    await broadcast({
        "type": "event_processed",
        "fleet": dashboard_state["fleet"],
        "feed_entry": feed_entry,
        "marker": marker,
        "escalation_queue": dashboard_state["escalation_queue"],
        "notification": {
            "channel": notification.get("channel"),
            "message": notification.get("message"),
            "tone": notification.get("tone"),
        },
    })

    return {"status": "processed", "outcome": outcome}


# ----------------------------------------------------------------
# REST Endpoints
# ----------------------------------------------------------------

@app.get("/api/fleet")
async def get_fleet():
    return dashboard_state["fleet"]


@app.get("/api/escalations")
async def get_escalations():
    return dashboard_state["escalation_queue"]


@app.get("/api/feed")
async def get_feed():
    return dashboard_state["activity_feed"][-50:]


@app.get("/api/markers")
async def get_markers():
    return dashboard_state["exception_markers"]


# ----------------------------------------------------------------
# Serve Dashboard HTML
# ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path) as f:
        return f.read()