#!/usr/bin/env python3
"""
geocoding_bot.py — Geocoding Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Converts addresses to lat/lon coordinates and timezone
offsets using the free Nominatim API and offline
timezonefinder library. Exposes an HTTP API.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests timezonefinder

Configuration
─────────────
Place `geocoding_config.json` in the same directory:

{
  "geocoding": {
    "provider": "nominatim",
    "user_agent": "NazgulGeocoder/1.0",
    "base_url": "https://nominatim.openstreetmap.org/search",
    "rate_limit": 1.1
  },
  "http_port": 9580,
  "file_watch": {
    "enabled": false,
    "directory": "/data/addresses",
    "output_directory": "/data/geocoded"
  },
  "heartbeat_interval": 30
}
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "geocoding_bot"
BOT_NAME = "Geocoding Bot"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "geocoding_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
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

def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME,
            "status":   "online",
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

# ── Geocoding engine ─────────────────────────────────────────────

class Geocoder:
    def __init__(self, config: dict):
        provider = config.get("geocoding", {}).get("provider", "nominatim")
        if provider == "nominatim":
            self.geocoder = NominatimGeocoder(config)
        else:
            raise ValueError(f"Unsupported geocoding provider: {provider}")

        # Timezone lookup (offline, requires timezonefinder)
        try:
            from timezonefinder import TimezoneFinder
            self.tz_finder = TimezoneFinder()
            self._use_tz = True
        except ImportError:
            self._use_tz = False
            _post("timezonefinder not installed, timezone lookup disabled", "warning")

    def geocode(self, address: str) -> dict:
        result = self.geocoder.geocode(address)
        if result.get("error"):
            return result
        lat = result.get("lat")
        lon = result.get("lon")
        tz_result = {"timezone": None, "utc_offset": None}
        if lat is not None and lon is not None and self._use_tz:
            try:
                tz_name = self.tz_finder.timezone_at(lat=lat, lng=lon)
                if tz_name:
                    import pytz
                    tz = pytz.timezone(tz_name)
                    now = datetime.now(tz)
                    tz_result["timezone"] = tz_name
                    tz_result["utc_offset"] = now.strftime("%z")
            except Exception:
                pass
        return {**result, **tz_result}

class NominatimGeocoder:
    def __init__(self, config: dict):
        cfg = config.get("geocoding", {})
        self.user_agent = cfg.get("user_agent", "NazgulGeocoder/1.0")
        self.base_url = cfg.get("base_url", "https://nominatim.openstreetmap.org/search")
        self.rate_limit = float(cfg.get("rate_limit", 1.1))
        self.last_request = 0.0

    def geocode(self, address: str) -> dict:
        # Rate limiting
        elapsed = time.time() - self.last_request
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self.last_request = time.time()

        try:
            resp = requests.get(
                self.base_url,
                params={"q": address, "format": "json", "limit": 1},
                headers={"User-Agent": self.user_agent},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return {"error": "No results"}
            return {
                "lat": float(data[0]["lat"]),
                "lon": float(data[0]["lon"]),
                "display_name": data[0].get("display_name", "")
            }
        except Exception as e:
            return {"error": str(e)}

# ── Global instance ──────────────────────────────────────────────
geocoder: Optional[Geocoder] = None

# ── HTTP API handler ─────────────────────────────────────────────
class GeocodeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/geocode":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                address = data.get("address", "")
                if not address:
                    self._respond(400, {"error": "Missing 'address' field"})
                    return
                result = geocoder.geocode(address)
                summary = f"Geocoded '{address[:50]}...' -> ({result.get('lat')}, {result.get('lon')})"
                _post(summary, "info", {"address": address, "result": result})
                self._respond(200, result)
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {"error": "Not found"})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass

def start_http(port: int):
    server = HTTPServer(("0.0.0.0", port), GeocodeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Geocoding API listening on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set):
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return
    for fname in entries:
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(directory, fname)
        if fpath in processed:
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            address = data.get("address") or data.get("query")
            if not address:
                _post(f"File {fname} missing address field", "warning")
                processed.add(fpath)
                continue
            result = geocoder.geocode(address)
            out_name = Path(fname).stem + "_geocoded.json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w", encoding="utf-8") as out:
                json.dump({"address": address, "result": result}, out, indent=2)
            _post(f"Geocoded {fname} → {out_name}", "info", {"file": fname})
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    global geocoder
    _post("Geocoding Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        geocoder = Geocoder(config)
        port = int(config.get("http_port", 9580))
        start_http(port)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, output_dir, processed_cache)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
