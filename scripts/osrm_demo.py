"""
osrm_demo.py

Demonstrates how OSRM works: snapping, real roads, turn-by-turn directions.
Shows the difference between straight-line and actual driving distance.

Usage:
  python scripts/osrm_demo.py
"""

import requests, json, math, os
from dotenv import load_dotenv

load_dotenv()

OSRM_BASE = os.environ.get("OSRM_BASE", "https://router.project-osrm.org")

# Raw coordinates: 16th & Mission → Fisherman's Wharf
origin_lat, origin_lon = 37.7649, -122.4194
dest_lat, dest_lon = 37.8080, -122.4177

# Query OSRM with full geometry and turn-by-turn steps
url = (
    f"{OSRM_BASE}/route/v1/driving/"
    f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    f"?alternatives=3&geometries=geojson&overview=full&steps=true"
)
resp = requests.get(url)
resp.raise_for_status()
data = resp.json()

print("=" * 65)
print("  OSRM Route: 16th & Mission → Fisherman's Wharf")
print("=" * 65)

# ---------------------------------------------------------------
# Step 1: SNAPPING — OSRM moves your point to the nearest road
# ---------------------------------------------------------------
print("\n--- Step 1: Coordinate Snapping ---\n")
labels = ["Origin (16th & Mission)", "Destination (Fisherman's Wharf)"]
inputs = [(origin_lat, origin_lon), (dest_lat, dest_lon)]

for i, wp in enumerate(data["waypoints"]):
    snap_dist = wp["distance"]
    in_lat, in_lon = inputs[i]
    snapped = wp["location"]  # [lon, lat]
    print(f"  {labels[i]}:")
    print(f"    You gave:       {in_lat:.4f}, {in_lon:.4f}")
    print(f"    OSRM snapped to: {snapped[1]:.6f}, {snapped[0]:.6f}")
    print(f"    Snap distance:  {snap_dist:.1f} meters to nearest road")
    print(f"    Road name:      \"{wp.get('name', 'unnamed')}\"")
    print()

print("  Why snapping matters: if your coordinate is in a park,")
print("  parking lot, or building, OSRM finds the closest drivable road.")
print("  It won't route through a building.")

# ---------------------------------------------------------------
# Step 2: Calculate straight-line distance for comparison
# ---------------------------------------------------------------
dlat = math.radians(dest_lat - origin_lat)
dlon = math.radians(dest_lon - origin_lon)
a = (math.sin(dlat / 2) ** 2
     + math.cos(math.radians(origin_lat))
     * math.cos(math.radians(dest_lat))
     * math.sin(dlon / 2) ** 2)
straight_km = 6371 * 2 * math.asin(math.sqrt(a))

# ---------------------------------------------------------------
# Step 3: Show all returned routes
# ---------------------------------------------------------------
print("\n--- Step 2: Route Alternatives ---\n")
print(f"  Straight-line distance: {straight_km:.2f} km")
print(f"  OSRM returned {len(data['routes'])} different drivable routes:\n")

for i, route in enumerate(data["routes"]):
    dur_min = route["duration"] / 60
    dist_km = route["distance"] / 1000
    coords = route["geometry"]["coordinates"]
    avg_speed = dist_km / (dur_min / 60) if dur_min > 0 else 0

    print(f"  Route {i + 1}:")
    print(f"    Distance:        {dist_km:.1f} km (vs {straight_km:.1f} km straight)")
    print(f"    Ratio:           {dist_km / straight_km:.2f}x longer than straight line")
    print(f"    Duration:        {dur_min:.1f} min")
    print(f"    Avg speed:       {avg_speed:.0f} km/h ({avg_speed * 0.621:.0f} mph)")
    print(f"    Geometry points: {len(coords)} coordinate pairs tracing the road")
    print()

# ---------------------------------------------------------------
# Step 4: Turn-by-turn for Route 1
# ---------------------------------------------------------------
print("--- Step 3: Turn-by-Turn Directions (Route 1) ---\n")
print("  OSRM follows real streets and obeys turn restrictions:\n")

steps = data["routes"][0]["legs"][0]["steps"]
for j, step in enumerate(steps):
    name = step.get("name", "") or "unnamed road"
    maneuver = step["maneuver"]["type"]
    modifier = step["maneuver"].get("modifier", "")
    dist = step["distance"]
    dur = step["duration"]
    if dist > 0:
        label = f"{maneuver} {modifier}".strip()
        print(f"    {j + 1:2d}. {label:25s} → {name:30s} ({dist:5.0f}m, {dur:4.0f}s)")

# ---------------------------------------------------------------
# Step 5: Geometry verification
# ---------------------------------------------------------------
print("\n--- Step 4: Geometry Verification ---\n")

route1_coords = data["routes"][0]["geometry"]["coordinates"]
total_haversine = 0.0
for k in range(len(route1_coords) - 1):
    lon1, lat1 = route1_coords[k]
    lon2, lat2 = route1_coords[k + 1]
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    total_haversine += 6371 * 2 * math.asin(math.sqrt(a))

osrm_km = data["routes"][0]["distance"] / 1000
diff_pct = abs(total_haversine - osrm_km) / osrm_km * 100

print(f"  OSRM reported distance:   {osrm_km:.3f} km")
print(f"  Haversine from geometry:  {total_haversine:.3f} km")
print(f"  Difference:               {diff_pct:.2f}%")
print()
if diff_pct < 2:
    print("  ✅ Geometry matches reported distance. The route is real,")
    print("     not a straight-line approximation.")
else:
    print("  ⚠️  Geometry diverges from reported distance.")

# ---------------------------------------------------------------
# Summary
# ---------------------------------------------------------------
r = data["routes"][0]
print("\n--- Summary ---\n")
print(f"  You gave two raw lat/lon points.")
print(f"  OSRM snapped them to real roads,")
print(f"  computed {len(data['routes'])} alternative driving routes,")
print(f"  returned turn-by-turn directions with {len(steps)} steps,")
print(f"  and traced the path with {len(route1_coords)} coordinate pairs.")
print()
print(f"  Straight line:  {straight_km:.1f} km")
print(f"  Actual driving:  {osrm_km:.1f} km ({osrm_km / straight_km:.1f}x longer)")
print(f"  Duration:        {r['duration'] / 60:.1f} min")
print()
print("  This is what 'real routing' means: not geometry,")
print("  but actual road-network pathfinding.")