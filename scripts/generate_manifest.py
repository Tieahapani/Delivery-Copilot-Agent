"""
generate_manifest.py

Builds a synthetic daily delivery manifest grounded in real SF data:
1. Loads real business addresses from DataSF CSV (downloaded manually)
2. Clusters them into driver routes via K-Means
3. Sequences stops using nearest-neighbor
4. Queries OSRM for real driving ETAs
5. Injects probabilistic exceptions
6. Outputs a complete manifest as JSON

Prerequisites:
  - OSRM running at localhost:5050 (docker compose up -d)
  - pip install requests scikit-learn pandas
  - Download CSV from: https://data.sfgov.org/api/views/g8m3-pdis/rows.csv?accessType=DOWNLOAD
    Save to: data/sf_businesses.csv

Usage:
  python scripts/generate_manifest.py
  python scripts/generate_manifest.py --deliveries 80 --drivers 8
"""

import argparse
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from sklearn.cluster import KMeans
import numpy as np

# ----------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------

OSRM_BASE = "https://router.project-osrm.org"

# Warehouse location: Bayview industrial area (realistic hub)
WAREHOUSE = {"lat": 37.7327, "lon": -122.3914, "name": "Bayview Distribution Hub"}

# SF bounding box (filter out addresses outside the city)
SF_BOUNDS = {
    "lat_min": 37.708,
    "lat_max": 37.812,
    "lon_min": -122.515,
    "lon_max": -122.355,
}

# Exception injection probabilities
EXCEPTION_RATES = {
    "address_inaccessible": 0.15,
    "customer_absent": 0.10,
    "traffic_delay": 0.10,
    "weather_disruption": 0.05,
    "vehicle_breakdown": 0.02,
    "misloaded_package": 0.03,
}

EXCEPTION_DETAILS = {
    "address_inaccessible": [
        "Gate code required, not provided by customer",
        "Construction blocking building entrance",
        "No safe access to delivery point",
        "Building entrance locked, no intercom response",
        "Driveway blocked by parked vehicles",
    ],
    "customer_absent": [
        "No response after doorbell and phone call",
        "Customer not at specified delivery location",
        "Business closed, no one available to receive",
    ],
    "traffic_delay": [
        "Accident on route, road closed",
        "Unexpected road closure for utility work",
        "Heavy congestion on primary route",
        "Bridge closure causing major detour",
    ],
    "weather_disruption": [
        "Heavy rain reducing visibility and speed",
        "Flooding on low-lying route segments",
        "High wind advisory, unsafe for large vehicles",
    ],
    "vehicle_breakdown": [
        "Flat tire, driver awaiting roadside assistance",
        "Engine warning light, must pull over",
        "Electrical system failure, vehicle inoperable",
    ],
    "misloaded_package": [
        "Package not found on truck, likely misloaded at hub",
        "Wrong package scanned onto this vehicle",
        "Package damaged during loading, cannot deliver",
    ],
}

PRIORITY_WEIGHTS = {
    "standard": 0.60,
    "priority": 0.30,
    "medical": 0.10,
}


# ----------------------------------------------------------------
# Step 1: Load and parse SF business addresses
# ----------------------------------------------------------------

def parse_business_location(point_str):
    """Parse 'POINT (lon lat)' into (latitude, longitude)."""
    if pd.isna(point_str):
        return None, None
    match = re.match(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)", str(point_str))
    if match:
        lon = float(match.group(1))
        lat = float(match.group(2))
        return lat, lon
    return None, None


def load_addresses(csv_path: str) -> pd.DataFrame:
    """Load business addresses from the DataSF CSV."""

    if not os.path.exists(csv_path):
        print(f"  ERROR: CSV not found at {csv_path}")
        print(f"  Download it from:")
        print(f"    https://data.sfgov.org/api/views/g8m3-pdis/rows.csv?accessType=DOWNLOAD")
        print(f"  Save it to: data/sf_businesses.csv")
        sys.exit(1)

    print(f"  [load] Reading {csv_path}...")
    df = pd.read_csv(csv_path, low_memory=False)
    print(f"  [load] {len(df)} total rows")

    # Parse lat/lon from 'Business Location' column
    print(f"  [parse] Extracting coordinates from Business Location...")
    coords = df["Business Location"].apply(parse_business_location)
    df["latitude"] = coords.apply(lambda x: x[0])
    df["longitude"] = coords.apply(lambda x: x[1])

    # Rename columns to match our internal format
    df = df.rename(columns={
        "DBA Name": "dba_name",
        "Street Address": "street_address",
        "City": "city",
        "State": "state",
        "Source Zipcode": "business_zip",
        "Neighborhoods - Analysis Boundaries": "nhood",
    })

    return df


def filter_addresses(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only addresses within SF bounds with valid coordinates."""

    before = len(df)

    df = df.dropna(subset=["latitude", "longitude", "street_address", "dba_name"])
    df = df[
        (df["latitude"] >= SF_BOUNDS["lat_min"])
        & (df["latitude"] <= SF_BOUNDS["lat_max"])
        & (df["longitude"] >= SF_BOUNDS["lon_min"])
        & (df["longitude"] <= SF_BOUNDS["lon_max"])
    ]

    # Drop duplicates by address
    df = df.drop_duplicates(subset=["street_address"])

    print(f"  [filter] {before} -> {len(df)} addresses (within SF, valid coords, unique)")
    return df.reset_index(drop=True)


# ----------------------------------------------------------------
# Step 2: Sample and cluster into driver routes
# ----------------------------------------------------------------

def sample_and_cluster(df: pd.DataFrame, n_deliveries: int, n_drivers: int) -> pd.DataFrame:
    """Sample delivery addresses and cluster into driver routes."""

    if len(df) < n_deliveries:
        print(f"  [warn] Only {len(df)} addresses available, using all")
        sample = df.copy()
    else:
        sample = df.sample(n=n_deliveries, random_state=random.randint(0, 9999))

    sample = sample.reset_index(drop=True)

    # K-Means on lat/lon to group nearby addresses
    coords = sample[["latitude", "longitude"]].values
    kmeans = KMeans(n_clusters=n_drivers, random_state=42, n_init=10)
    sample["driver_cluster"] = kmeans.fit_predict(coords)

    for i in range(n_drivers):
        count = (sample["driver_cluster"] == i).sum()
        print(f"  [cluster] Driver {i+1}: {count} stops")

    return sample


# ----------------------------------------------------------------
# Step 3: Sequence stops with nearest-neighbor
# ----------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    """Quick haversine distance in km."""
    R = 6371
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2) ** 2
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def nearest_neighbor_order(stops: list, start_lat: float, start_lon: float) -> list:
    """Order stops greedily by nearest unvisited from current position."""

    remaining = list(range(len(stops)))
    order = []
    cur_lat, cur_lon = start_lat, start_lon

    while remaining:
        best_idx = None
        best_dist = float("inf")
        for idx in remaining:
            d = haversine_km(cur_lat, cur_lon, stops[idx]["lat"], stops[idx]["lon"])
            if d < best_dist:
                best_dist = d
                best_idx = idx
        order.append(best_idx)
        remaining.remove(best_idx)
        cur_lat = stops[best_idx]["lat"]
        cur_lon = stops[best_idx]["lon"]

    return order


# ----------------------------------------------------------------
# Step 4: Query OSRM for real driving ETAs
# ----------------------------------------------------------------

def get_driving_time(origin_lat, origin_lon, dest_lat, dest_lon) -> dict:
    """Query OSRM for driving duration and distance between two points."""

    url = (
        f"{OSRM_BASE}/route/v1/driving/"
        f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
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
    except Exception as e:
        print(f"  [osrm] Error: {e}")

    # Fallback: estimate from haversine
    dist_km = haversine_km(origin_lat, origin_lon, dest_lat, dest_lon)
    return {
        "duration_sec": (dist_km / 30) * 3600,  # assume 30 km/h average
        "distance_m": dist_km * 1000,
    }


# ----------------------------------------------------------------
# Step 5: Inject exceptions
# ----------------------------------------------------------------

def generate_exception(stop_index: int, total_stops: int, driver_had_breakdown: bool):
    """Probabilistically assign an exception to a delivery stop."""

    # If driver already broke down, all subsequent stops are affected
    if driver_had_breakdown:
        return {
            "type": "vehicle_breakdown",
            "severity": "escalate",
            "details": "Driver vehicle disabled, all remaining stops affected",
        }

    # Roll for each exception type
    roll = random.random()
    cumulative = 0.0

    for exc_type, rate in EXCEPTION_RATES.items():
        cumulative += rate
        if roll < cumulative:
            # Determine severity based on exception type
            if exc_type in ("vehicle_breakdown", "weather_disruption"):
                severity = "escalate"
            elif exc_type == "misloaded_package":
                severity = "escalate"
            else:
                severity = random.choice(["auto_resolve", "auto_resolve", "escalate"])

            return {
                "type": exc_type,
                "severity": severity,
                "details": random.choice(EXCEPTION_DETAILS[exc_type]),
            }

    return None


def assign_priority():
    """Randomly assign a package priority based on weights."""
    roll = random.random()
    if roll < PRIORITY_WEIGHTS["standard"]:
        return "standard"
    elif roll < PRIORITY_WEIGHTS["standard"] + PRIORITY_WEIGHTS["priority"]:
        return "priority"
    return "medical"


# ----------------------------------------------------------------
# Step 6: Build the full manifest
# ----------------------------------------------------------------

def build_manifest(sample_df: pd.DataFrame, n_drivers: int, dispatch_hour: int = 10) -> dict:
    """Assemble the complete daily delivery manifest."""

    today = datetime.now().strftime("%Y-%m-%d")
    dispatch_time = datetime.now().replace(hour=dispatch_hour, minute=0, second=0, microsecond=0)

    drivers = []
    delivery_counter = 1000

    for cluster_id in range(n_drivers):
        cluster_df = sample_df[sample_df["driver_cluster"] == cluster_id]
        if cluster_df.empty:
            continue

        driver_id = f"DR-{cluster_id + 1:02d}"

        # Build raw stop list
        raw_stops = []
        for _, row in cluster_df.iterrows():
            raw_stops.append({
                "lat": round(row["latitude"], 6),
                "lon": round(row["longitude"], 6),
                "business_name": str(row["dba_name"]).strip(),
                "address": str(row["street_address"]).strip(),
                "neighborhood": str(row.get("nhood", "Unknown")).strip(),
            })

        # Order stops using nearest-neighbor from warehouse
        order = nearest_neighbor_order(raw_stops, WAREHOUSE["lat"], WAREHOUSE["lon"])
        ordered_stops = [raw_stops[i] for i in order]

        # Compute ETAs by querying OSRM between consecutive stops
        stops_with_eta = []
        current_time = dispatch_time
        prev_lat, prev_lon = WAREHOUSE["lat"], WAREHOUSE["lon"]
        driver_had_breakdown = False

        for i, stop in enumerate(ordered_stops):
            delivery_counter += 1
            delivery_id = f"D-{delivery_counter}"

            # Get driving time from previous stop
            route = get_driving_time(prev_lat, prev_lon, stop["lat"], stop["lon"])
            drive_minutes = route["duration_sec"] / 60

            # Add 3 min service time per stop (park, deliver, depart)
            service_minutes = 3
            current_time += timedelta(minutes=drive_minutes + service_minutes)

            # SLA: 4 hours from dispatch for standard, 2 hours for medical
            priority = assign_priority()
            if priority == "medical":
                sla_hours = 2
            elif priority == "priority":
                sla_hours = 3
            else:
                sla_hours = 4
            sla_deadline = dispatch_time + timedelta(hours=sla_hours)

            # Inject exception
            exception = generate_exception(i, len(ordered_stops), driver_had_breakdown)
            if exception and exception["type"] == "vehicle_breakdown":
                driver_had_breakdown = True

            # If traffic delay, inflate the ETA
            if exception and exception["type"] == "traffic_delay":
                delay_factor = random.uniform(0.5, 1.5)
                delay_minutes = drive_minutes * delay_factor
                current_time += timedelta(minutes=delay_minutes)

            stop_entry = {
                "delivery_id": delivery_id,
                "business_name": stop["business_name"],
                "address": stop["address"],
                "neighborhood": stop["neighborhood"],
                "lat": stop["lat"],
                "lon": stop["lon"],
                "eta": current_time.isoformat(),
                "sla_deadline": sla_deadline.isoformat(),
                "package_priority": priority,
                "drive_time_minutes": round(drive_minutes, 1),
                "distance_km": round(route["distance_m"] / 1000, 1),
                "exception": exception,
            }
            stops_with_eta.append(stop_entry)

            prev_lat, prev_lon = stop["lat"], stop["lon"]

        # Driver summary
        total_exceptions = sum(1 for s in stops_with_eta if s["exception"] is not None)
        sla_at_risk = sum(
            1 for s in stops_with_eta
            if datetime.fromisoformat(s["eta"]) > datetime.fromisoformat(s["sla_deadline"])
        )

        driver_entry = {
            "driver_id": driver_id,
            "total_stops": len(stops_with_eta),
            "total_exceptions": total_exceptions,
            "sla_at_risk": sla_at_risk,
            "stops": stops_with_eta,
        }
        drivers.append(driver_entry)

    # Manifest summary
    all_stops = [s for d in drivers for s in d["stops"]]
    total_exceptions = sum(1 for s in all_stops if s["exception"])
    exception_breakdown = {}
    for s in all_stops:
        if s["exception"]:
            t = s["exception"]["type"]
            exception_breakdown[t] = exception_breakdown.get(t, 0) + 1

    manifest = {
        "date": today,
        "dispatch_time": dispatch_time.isoformat(),
        "warehouse": WAREHOUSE,
        "summary": {
            "total_drivers": len(drivers),
            "total_deliveries": len(all_stops),
            "total_exceptions": total_exceptions,
            "exception_rate": round(total_exceptions / max(len(all_stops), 1) * 100, 1),
            "exception_breakdown": exception_breakdown,
            "sla_at_risk": sum(d["sla_at_risk"] for d in drivers),
        },
        "drivers": drivers,
    }

    return manifest


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate synthetic delivery manifest")
    parser.add_argument("--deliveries", type=int, default=100, help="Number of deliveries (default 100)")
    parser.add_argument("--drivers", type=int, default=10, help="Number of drivers (default 10)")
    args = parser.parse_args()

    print("=" * 55)
    print("  Delivery Manifest Generator")
    print("=" * 55)
    print()

    # Check OSRM
    print("[1/5] Checking OSRM server...")
    try:
        r = requests.get(f"{OSRM_BASE}/nearest/v1/driving/-122.4194,37.7749", timeout=5)
        print("  OSRM is running.\n")
    except requests.ConnectionError:
        print("  ERROR: OSRM not reachable. Run: docker compose up -d")
        sys.exit(1)

    # Load addresses
    csv_path = os.path.join(os.path.dirname(__file__), "..", "data", "sf_businesses.csv")
    csv_path = os.path.abspath(csv_path)

    print("[2/5] Loading SF business addresses...")
    df = load_addresses(csv_path)
    df = filter_addresses(df)
    print()

    # Sample and cluster
    print(f"[3/5] Sampling {args.deliveries} deliveries across {args.drivers} drivers...")
    sample = sample_and_cluster(df, args.deliveries, args.drivers)
    print()

    # Build manifest (includes OSRM queries and exception injection)
    print("[4/5] Building manifest (querying OSRM for ETAs)...")
    manifest = build_manifest(sample, args.drivers)
    print()

    # Save
    output_path = os.path.join(os.path.dirname(__file__), "..", "data", "manifest.json")
    output_path = os.path.abspath(output_path)

    print("[5/5] Saving manifest...")
    with open(output_path, "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    print(f"  Saved to {output_path}")
    print()

    # Print summary
    s = manifest["summary"]
    print("=" * 55)
    print(f"  Manifest Summary")
    print("=" * 55)
    print(f"  Date:             {manifest['date']}")
    print(f"  Drivers:          {s['total_drivers']}")
    print(f"  Deliveries:       {s['total_deliveries']}")
    print(f"  Exceptions:       {s['total_exceptions']} ({s['exception_rate']}%)")
    print(f"  SLA at risk:      {s['sla_at_risk']}")
    print(f"  Breakdown:")
    for exc_type, count in s["exception_breakdown"].items():
        print(f"    {exc_type}: {count}")
    print("=" * 55)


if __name__ == "__main__":
    main()