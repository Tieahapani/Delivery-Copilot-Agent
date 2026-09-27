#!/bin/bash
# ============================================================
# setup_osrm.sh
# Downloads Northern California OSM extract and preprocesses
# it for the OSRM routing engine.
#
# Prerequisites: Docker installed and running
# Usage: ./scripts/setup_osrm.sh
# ============================================================

set -euo pipefail

DATA_DIR="$(cd "$(dirname "$0")/.." && pwd)/data/osrm"
OSM_FILE="northern-california-latest.osm.pbf"
GEOFABRIK_URL="https://download.geofabrik.de/north-america/us/california/norcal-latest.osm.pbf"

echo "=== OSRM Setup for Delivery Copilot ==="
echo ""

# ---- Step 1: Create data directory ----
mkdir -p "$DATA_DIR"
cd "$DATA_DIR"

# ---- Step 2: Download OSM extract ----
if [ -f "$OSM_FILE" ]; then
    echo "[skip] $OSM_FILE already exists"
else
    echo "[1/4] Downloading Northern California OSM extract..."
    echo "      Source: $GEOFABRIK_URL"
    echo "      This is ~300-400 MB, may take a few minutes."
    curl -L -o "$OSM_FILE" "$GEOFABRIK_URL"
    echo "      Done."
fi

# ---- Step 3: Extract (build routing graph) ----
if [ -f "northern-california-latest.osrm" ]; then
    echo "[skip] Extract already completed"
else
    echo "[2/4] Extracting road network (this takes 2-5 min)..."
    docker run --rm -t \
        -v "$DATA_DIR:/data" \
        osrm/osrm-backend:latest \
        osrm-extract -p /opt/car.lua /data/"$OSM_FILE"
    echo "      Done."
fi

# ---- Step 4: Partition ----
if [ -f "northern-california-latest.osrm.partition" ]; then
    echo "[skip] Partition already completed"
else
    echo "[3/4] Partitioning graph..."
    docker run --rm -t \
        -v "$DATA_DIR:/data" \
        osrm/osrm-backend:latest \
        osrm-partition /data/northern-california-latest.osrm
    echo "      Done."
fi

# ---- Step 5: Customize ----
if [ -f "northern-california-latest.osrm.cell_metrics" ]; then
    echo "[skip] Customize already completed"
else
    echo "[4/4] Customizing graph (final preprocessing)..."
    docker run --rm -t \
        -v "$DATA_DIR:/data" \
        osrm/osrm-backend:latest \
        osrm-customize /data/northern-california-latest.osrm
    echo "      Done."
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Start the routing engine with:"
echo "  docker compose up -d"
echo ""
echo "Test it with:"
echo "  python scripts/test_osrm.py"
echo ""