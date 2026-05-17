#!/usr/bin/env python3
"""
guardian_companion_bot.py — Guardian Companion Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
High‑frequency GPS monitoring interface. Receives location
updates from an Android companion app (or any client that
POSTs to /location). Runs anomaly detection for:
  • Prolonged stop in a low‑traffic area
  • Deviation from a known safe path

Posts alerts to the BotController hub. No simulation – real data
from the phone, real analysis.

Requirements:
    pip install flask requests geopy

Android side:
    Use Tasker, MacroDroid, or a simple HTTP‑POST script to
    send {lat, lon, timestamp} every 5‑10 seconds to
    http://<this-machine>:<port>/location

Configuration:
    On first run, `guardian_config.json` is created.
    Edit safe paths, home/work zones, stationary timeout, etc.
"""

import json
import time
import threading
import math
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, request, jsonify
import requests
from geopy.distance import distance as geodist, Point

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "guardian_companion"
BOT_NAME = "Guardian Companion"

CFG_FILE = Path(__file__).with_name("guardian_config.json")
DEFAULT_CONFIG = {
    "web_port": 5068,
    "user_id": "default_user",
    "safe_paths": [
        # Example: home -> gym -> work. List of [lat, lon] waypoints.
        # The path is a polyline; alarm triggers if user deviates > 80 m.
        {
            "name": "Home to Work",
            "waypoints": [
                [40.7128, -74.0060],   # home
                [40.7150, -74.0020],   # landmark
                [40.7200, -73.9990]    # work
            ]
        }
    ],
    "safe_zones": [           # Places where long stops are normal
        {"name": "Home",  "lat": 40.7128, "lon": -74.0060, "radius_m": 100},
        {"name": "Gym",   "lat": 40.7145, "lon": -74.0030, "radius_m": 80},
        {"name": "Work",  "lat": 40.7200, "lon": -73.9990, "radius_m": 150}
    ],
    "stationary_timeout_seconds": 300,   # 5 minutes before alert
    "stationary_radius_meters": 25,      # radius to consider "not moving"
    "deviation_distance_m": 80,          # max lateral distance from safe path
    "min_speed_for_deviation_check_mps": 0.5,  # ignore if moving very slowly
    "log_interval_seconds": 60,          # how often to send status to hub
    "scan_interval_seconds": 10          # re‑evaluate every N sec (internal)
}

# ── State ───────────────────────────────────────────────────────────────────
# In‑memory store for the latest locations and analysis results
location_history = []         # list of {"lat","lon","timestamp","speed"}
last_status_log = 0

# ── Hub helpers ─────────────────────────────────────────────────────────────
def post_to_hub(summary, level="info", payload=None):
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id":   BOT_ID,
            "bot_name": BOT_NAME,
            "summary":  summary,
            "level":    level,
            "payload":  payload or {},
        }, timeout=5)
    except Exception:
        pass

def wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Helpers ─────────────────────────────────────────────────────────────────
def is_in_safe_zone(lat, lon, zones):
    """Return zone name if within any safe zone, else None."""
    for zone in zones:
        dist = geodist((lat, lon), (zone["lat"], zone["lon"])).meters
        if dist <= zone.get("radius_m", 50):
            return zone["name"]
    return None

def distance_to_path(lat, lon, path_waypoints):
    """Minimum distance (meters) from point to polyline of waypoints."""
    min_dist = float("inf")
    for i in range(len(path_waypoints) - 1):
        p1 = path_waypoints[i]
        p2 = path_waypoints[i+1]
        dist = _distance_to_segment(lat, lon, p1, p2)
        if dist < min_dist:
            min_dist = dist
    return min_dist

def _distance_to_segment(lat, lon, p1, p2):
    """Calculate distance from point to a segment defined by two waypoints."""
    # Convert to Cartesian approximation (small area)
    from geopy.distance import great_circle
    # We'll project onto the line: use vector projection
    # Using pyproj would be better, but for short segments geodist is fine.
    # We'll approximate by converting to meters using a local projection.
    # Simpler: use geopy.distance with appropriate formula.
    A = Point(p1[0], p1[1])
    B = Point(p2[0], p2[1])
    P = Point(lat, lon)

    # Shortest distance from point to great circle segment:
    # Compute cross-track distance.
    # geopy doesn't have direct method, so we'll compute using spherical geometry.
    # We'll use the formula: d = 2 * asin( ... ) but easier: compute bearing and cross-track distance.
    # We'll use a known algorithm:
    # Convert to radians.
    import math
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    latP, lonP = math.radians(lat), math.radians(lon)

    # Distance from P to segment AB (cross-track distance)
    # δ13 = distance between A and P
    # θ13 = initial bearing from A to P
    # θ12 = initial bearing from A to B
    # d_xt = asin(sin(δ13)*sin(θ13-θ12)) * R
    R = 6371000.0  # meters

    # Distance A->P
    d13 = great_circle((p1[0], p1[1]), (lat, lon)).meters
    # Bearing A->P
    θ13 = _bearing(lat1, lon1, latP, lonP)
    θ12 = _bearing(lat1, lon1, lat2, lon2)

    # Along-track distance from A to the projection point
    δ13 = d13 / R  # angular distance
    d_xt = math.asin(math.sin(δ13) * math.sin(θ13 - θ12)) * R

    # Check if projection lies within the segment by computing along-track distance
    # along-track distance: d_at = acos( cos(δ13) / cos(d_xt/R) ) * R
    cos_xt = math.cos(abs(d_xt) / R)
    if cos_xt == 0:
        d_at = 0
    else:
        d_at = math.acos( max(-1, min(1, math.cos(δ13) / cos_xt)) ) * R

    # Distance from A to B
    d12 = great_circle((p1[0], p1[1]), (p2[0], p2[1])).meters

    if d_at < 0:
        # Projection behind A, distance is d13
        return d13
    elif d_at > d12:
        # Projection beyond B, distance is great_circle(P, B).meters
        return great_circle((lat, lon), (p2[0], p2[1])).meters
    else:
        return abs(d_xt)

def _bearing(lat1, lon1, lat2, lon2):
    """Initial bearing from point 1 to point 2 (radians)."""
    import math
    dLon = lon2 - lon1
    y = math.sin(dLon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dLon)
    return math.atan2(y, x)

# ── Flask server for GPS ingestion ──────────────────────────────────────────
app = Flask(__name__)
app.config["config"] = {}

@app.route("/location", methods=["POST"])
def receive_location():
    data = request.get_json(force=True)
    lat = data.get("lat")
    lon = data.get("lon")
    timestamp = data.get("timestamp", time.time())
    speed = data.get("speed", None)   # m/s, optional
    if lat is None or lon is None:
        return jsonify({"error": "lat/lon required"}), 400

    # Append to history (keep last 2 hours)
    cutoff = time.time() - 7200
    location_history.append({
        "lat": lat, "lon": lon, "timestamp": timestamp, "speed": speed
    })
    # Trim old
    while location_history and location_history[0]["timestamp"] < cutoff:
        location_history.pop(0)
    return jsonify({"status": "ok"})

# ── Anomaly detection logic ─────────────────────────────────────────────────
def detect_anomalies(config):
    if len(location_history) < 2:
        return

    # 1) Stationary check
    now = time.time()
    recent_window = config["stationary_timeout_seconds"]
    stationary_radius = config["stationary_radius_meters"]
    # Find the most recent position
    latest = location_history[-1]
    # Check how long we've been within a small radius
    stopped_start = None
    for idx in range(len(location_history)-1, -1, -1):
        pt = location_history[idx]
        dist = geodist((latest["lat"], latest["lon"]), (pt["lat"], pt["lon"])).meters
        if dist <= stationary_radius:
            stopped_start = pt["timestamp"]
        else:
            break
    if stopped_start and (now - stopped_start) >= stationary_timeout_seconds:
        # Check if in safe zone
        zone = is_in_safe_zone(latest["lat"], latest["lon"], config["safe_zones"])
        if not zone:
            post_to_hub(
                f"⚠️ User stationary for > {stationary_timeout_seconds//60} min in low‑traffic area",
                "error",
                {"lat": latest["lat"], "lon": latest["lon"], "duration": int(now - stopped_start)}
            )
            # reset to avoid re-sending every scan
            # We'll not clear history; it will repeat, but that's fine (throttle via time)

    # 2) Deviation from safe path
    if config["safe_paths"]:
        # Only check if moving faster than threshold
        if len(location_history) >= 2:
            prev = location_history[-2]
            curr = latest
            dt = curr["timestamp"] - prev["timestamp"]
            if dt > 0:
                dist_traveled = geodist((prev["lat"], prev["lon"]), (curr["lat"], curr["lon"])).meters
                speed = dist_traveled / dt
                if speed >= config.get("min_speed_for_deviation_check_mps", 0.5):
                    # Check against all safe paths
                    min_deviation = float("inf")
                    for path in config["safe_paths"]:
                        dev = distance_to_path(curr["lat"], curr["lon"], path["waypoints"])
                        if dev < min_deviation:
                            min_deviation = dev
                    if min_deviation > config["deviation_distance_m"]:
                        post_to_hub(
                            f"🛑 User deviated from safe path by {min_deviation:.0f}m",
                            "warning",
                            {"lat": curr["lat"], "lon": curr["lon"],
                             "deviation_m": round(min_deviation, 1)}
                        )

# ── Main monitoring loop ────────────────────────────────────────────────────
def monitor_loop(config):
    global last_status_log
    interval = config.get("scan_interval_seconds", 10)
    while True:
        try:
            detect_anomalies(config)
        except Exception as e:
            post_to_hub(f"Error in anomaly detection: {e}", "error")
        # Log status periodically
        now = time.time()
        if now - last_status_log > config.get("log_interval_seconds", 60):
            post_to_hub(
                f"📍 Guardian active — {len(location_history)} GPS points in memory",
                "info",
                {"history_length": len(location_history)}
            )
            last_status_log = now
        time.sleep(interval)

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config created at {CFG_FILE}. Edit safe paths and safe zones.",
            "warning"
        )
        # Don't exit; run with defaults

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["config"] = config

    # Start monitoring thread
    threading.Thread(target=monitor_loop, args=(config,), daemon=True).start()

    # Heartbeat thread
    def heartbeat():
        while True:
            time.sleep(20)
            try:
                requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
                    "bot_name": BOT_NAME,
                    "status": "online",
                }, timeout=3)
            except Exception:
                pass
    threading.Thread(target=heartbeat, daemon=True).start()

    port = config.get("web_port", 5068)
    post_to_hub(
        f"🛡️ Guardian Companion Bot listening on port {port} for GPS data",
        "info"
    )
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
