"""
app.py

FastAPI backend for the dispatcher dashboard.
  - Loads manifest data for fleet overview
  - Consumes exception events from Redis Stream (production pattern)
  - Processes each through the LangGraph copilot
  - Pushes results to connected browsers via WebSocket
  - Serves the dashboard HTML page

Usage:
  uvicorn dashboard.app:app --port 8000
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime

import redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

load_dotenv()

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.graph import build_graph

app = FastAPI(title="Delivery Copilot Dashboard")

# ----------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
STREAM_NAME = "delivery:exceptions"
CONSUMER_GROUP = "copilot-dashboard"
CONSUMER_NAME = "dashboard-worker-1"

# ----------------------------------------------------------------
# State
# ----------------------------------------------------------------

connected_clients: list[WebSocket] = []

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

graph = None
redis_client = None


# ----------------------------------------------------------------
# Startup
# ----------------------------------------------------------------

@app.on_event("startup")
async def startup():
    global graph, redis_client

    # Build LangGraph
    print("  Building copilot graph...")
    graph = build_graph()
    print("  Graph compiled.")

    # Connect to Redis
    print("  Connecting to Redis...")
    try:
        redis_client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, decode_responses=True
        )
        redis_client.ping()
        print(f"  Redis connected at {REDIS_HOST}:{REDIS_PORT}")

        # Create consumer group (ignore if exists)
        try:
            redis_client.xgroup_create(STREAM_NAME, CONSUMER_GROUP, id="0", mkstream=True)
            print(f"  Created consumer group '{CONSUMER_GROUP}'")
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                print(f"  Consumer group '{CONSUMER_GROUP}' already exists")
            else:
                raise
    except redis.ConnectionError:
        print("  WARNING: Redis not available. Start Redis with: docker compose up -d")
        redis_client = None

    # Load manifest
    manifest_path = os.path.join(os.path.dirname(__file__), "..", "data", "manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            dashboard_state["manifest"] = json.load(f)
        load_fleet_data()
        print(f"  Manifest loaded: {dashboard_state['fleet']['total_deliveries']} deliveries")
    else:
        print("  WARNING: No manifest found. Run generate_manifest.py first.")

    # Start Redis consumer in background
    if redis_client:
        asyncio.create_task(redis_consumer_loop())


def load_fleet_data():
    """Extract fleet overview and driver positions from manifest."""
    manifest = dashboard_state["manifest"]
    if not manifest:
        return

    drivers = manifest["drivers"]
    all_stops = [s for d in drivers for s in d["stops"]]

    dashboard_state["fleet"]["active_drivers"] = len(drivers)
    dashboard_state["fleet"]["total_deliveries"] = len(all_stops)

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
# Redis Consumer Loop (background task)
# ----------------------------------------------------------------

async def redis_consumer_loop():
    """
    Background task that continuously reads from Redis Stream,
    processes each event through the LangGraph copilot, and
    broadcasts results to all connected dashboard clients.
    """
    print("  Redis consumer started. Listening for events...")

    # First, process any pending (unacknowledged) events from previous runs
    await process_pending_events()

    # Then listen for new events
    while True:
        try:
            # xreadgroup blocks until an event arrives
            # ">" means only give me new (undelivered) events
            result = redis_client.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {STREAM_NAME: ">"},
                count=1,
                block=2000,  # block for 2 seconds, then check again
            )

            if not result:
                await asyncio.sleep(0.1)  # yield to event loop
                continue

            for stream_name, messages in result:
                for msg_id, event in messages:
                    # Process through LangGraph
                    await process_and_broadcast(event, msg_id)

                    # Acknowledge the event
                    redis_client.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)

        except redis.ConnectionError:
            print("  Redis connection lost. Retrying in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"  Consumer error: {e}")
            await asyncio.sleep(1)


async def process_pending_events():
    """Process any events that were read but not acknowledged (crash recovery)."""
    try:
        result = redis_client.xreadgroup(
            CONSUMER_GROUP, CONSUMER_NAME, {STREAM_NAME: "0"}, count=100
        )
        if result and result[0][1]:
            pending_count = len(result[0][1])
            print(f"  Recovering {pending_count} pending events...")
            for stream_name, messages in result:
                for msg_id, event in messages:
                    await process_and_broadcast(event, msg_id)
                    redis_client.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
            print(f"  Recovered {pending_count} events.")
        else:
            print("  No pending events.")
    except Exception as e:
        print(f"  Error processing pending: {e}")


# ----------------------------------------------------------------
# Event Processing
# ----------------------------------------------------------------

async def process_and_broadcast(event: dict, msg_id: str = ""):
    """
    Run an event through the LangGraph copilot and broadcast
    the result to all connected dashboard clients.
    """
    global graph

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
        # Run graph in a thread pool to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, graph.invoke, initial_state)
    except Exception as e:
        print(f"  Graph error for {event.get('delivery_id', '?')}: {e}")
        return

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

    print(f"  {icon} {event.get('delivery_id')} | {event.get('exception_type')} | {outcome} | {msg_id}")

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
            data = await websocket.receive_json()
            await handle_dispatcher_action(data)
    except WebSocketDisconnect:
        connected_clients.remove(websocket)
        print(f"  Dashboard client disconnected ({len(connected_clients)} total)")


async def handle_dispatcher_action(data: dict):
    """Handle approve/dismiss from the dispatcher."""
    action = data.get("action")
    delivery_id = data.get("delivery_id")

    if action == "approve":
        dashboard_state["escalation_queue"] = [
            e for e in dashboard_state["escalation_queue"]
            if e.get("delivery_id") != delivery_id
        ]
        dashboard_state["fleet"]["pending_escalation"] = len(dashboard_state["escalation_queue"])

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
# REST Endpoints (for debugging)
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


# ----------------------------------------------------------------
# Serve Dashboard HTML
# ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path) as f:
        return f.read()