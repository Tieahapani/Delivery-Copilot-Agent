"""
test_osrm.py

Verifies OSRM is running by computing routes between real SF locations.
Prints driving time, distance, and a summary for each test route.

Usage: python scripts/test_osrm.py
"""

import requests
import sys

OSRM_BASE = "http://localhost:5050"

# Real San Francisco locations for testing
TEST_ROUTES = [
    {
        "name": "SFSU to Mission District",
        "origin": (-122.4786, 37.7219),       # SFSU campus
        "destination": (-122.4194, 37.7599),   # Mission & 16th
    },
    {
        "name": "Warehouse (Bayview) to Financial District",
        "origin": (-122.3914, 37.7327),        # Bayview industrial area
        "destination": (-122.3990, 37.7946),    # Financial District
    },
    {
        "name": "Sunset to North Beach (cross-city)",
        "origin": (-122.4953, 37.7601),        # Inner Sunset
        "destination": (-122.4074, 37.8005),    # North Beach
    },
    {
        "name": "SFO Airport to Downtown",
        "origin": (-122.3790, 37.6213),        # SFO
        "destination": (-122.4098, 37.7852),    # Union Square
    },
]


def check_health():
    """Verify OSRM server is reachable."""
    try:
        resp = requests.get(f"{OSRM_BASE}/nearest/v1/driving/-122.4194,37.7749", timeout=5)
        resp.raise_for_status()
        return True
    except requests.ConnectionError:
        return False
    except requests.HTTPError as e:
        print(f"  Server responded but returned error: {e}")
        return False


def get_route(origin_lon_lat, dest_lon_lat):
    """
    Query OSRM for a route.
    Returns (duration_seconds, distance_meters, route_summary) or None.
    """
    o_lon, o_lat = origin_lon_lat
    d_lon, d_lat = dest_lon_lat

    url = (
        f"{OSRM_BASE}/route/v1/driving/"
        f"{o_lon},{o_lat};{d_lon},{d_lat}"
        f"?overview=full&steps=true"
    )

    resp = requests.get(url, timeout=10)
    data = resp.json()

    if data.get("code") != "Ok":
        return None

    route = data["routes"][0]
    return {
        "duration_sec": route["duration"],
        "distance_m": route["distance"],
        "legs": len(route["legs"]),
    }


def format_duration(seconds):
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes >= 60:
        hours = minutes // 60
        minutes = minutes % 60
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


def main():
    print("=" * 55)
    print("  OSRM Routing Engine Test")
    print("=" * 55)
    print()

    # Health check
    print("Checking OSRM server at localhost:5050 ...")
    if not check_health():
        print()
        print("ERROR: Cannot reach OSRM server.")
        print("Make sure you have run:")
        print("  docker compose up -d")
        sys.exit(1)

    print("Server is up.\n")

    # Run test routes
    all_passed = True
    for route_info in TEST_ROUTES:
        name = route_info["name"]
        result = get_route(route_info["origin"], route_info["destination"])

        if result is None:
            print(f"  FAIL  {name}")
            print(f"        Could not compute route\n")
            all_passed = False
            continue

        duration = format_duration(result["duration_sec"])
        distance_km = result["distance_m"] / 1000

        print(f"  OK    {name}")
        print(f"        {distance_km:.1f} km | {duration} driving\n")

    # Summary
    print("=" * 55)
    if all_passed:
        print("  All routes computed successfully.")
        print("  OSRM is ready for the delivery copilot.")
    else:
        print("  Some routes failed. Check the OSM extract coverage.")
    print("=" * 55)


if __name__ == "__main__":
    main()