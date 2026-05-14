#!/usr/bin/env python3
"""
beauty_device_comparison_bot.py — Beauty Device Comparison Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Compares LED masks, microcurrent tools, and cleansing brushes
based on clinical studies and user preferences, then recommends
the best deals with affiliate links. Uses a curated catalog of
devices with real clinical study references.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `beauty_device_config.json` in the same directory:

{
  "catalog_file": "beauty_devices_catalog.json",
  "http_port": 9650,
  "file_watch": {
    "enabled": false,
    "directory": "/data/device_queries",
    "output_directory": "/data/device_comparisons"
  },
  "heartbeat_interval": 30
}

The catalog file (`beauty_devices_catalog.json`) should be an array of objects:

[
  {
    "id": "dr_dennis_gross_spectralite",
    "name": "Dr. Dennis Gross Spectralite FaceWare Pro",
    "device_type": "led_mask",
    "price": 435.0,
    "concerns": ["anti-aging", "acne"],
    "features": ["100 red LED lights", "62 blue LED lights", "3 treatment modes"],
    "clinical_evidence": "A 2018 clinical study showed 97% of users saw improvement in fine lines after 2 weeks.",
    "affiliate_link": "https://www.sephora.com/product/...",
    "rating": 4.5,
    "image_url": "https://..."
  },
  ...
]
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import List, Dict, Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "beauty_device_comparison_bot"
BOT_NAME = "Beauty Device Comparison Bot"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "beauty_device_config.json"
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

# ── Catalog loading ─────────────────────────────────────────────
def load_catalog(path: str) -> List[Dict]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []

# ── Comparison logic ────────────────────────────────────────────
def compare_devices(device_type: str, budget: Optional[float],
                    concern: Optional[str], catalog: List[Dict]) -> List[Dict]:
    """Return ranked list of matching devices."""
    # Filter by device_type
    filtered = [d for d in catalog if d.get("device_type") == device_type]
    if not filtered:
        return []

    # Filter by budget
    if budget is not None:
        filtered = [d for d in filtered if d.get("price", 0) <= budget]

    # Filter by concern (optional)
    if concern:
        concern_lower = concern.lower()
        filtered = [d for d in filtered if concern_lower in [c.lower() for c in d.get("concerns", [])]]

    # Sort by rating descending (or clinical score if available)
    filtered.sort(key=lambda x: x.get("rating", 0), reverse=True)

    return filtered[:5]   # top 5

# ── HTTP API handler ─────────────────────────────────────────────
class ComparisonHandler(BaseHTTPRequestHandler):
    catalog: List[Dict] = []

    def do_POST(self):
        if self.path == "/compare":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                device_type = data.get("device_type", "")
                budget = data.get("budget")
                concern = data.get("concern")
                if not device_type:
                    self._respond(400, {"error": "Missing 'device_type' (led_mask, microcurrent, cleansing_brush)"})
                    return
                results = compare_devices(device_type, budget, concern, self.catalog)
                summary = f"Compared {len(results)} {device_type} devices (budget: {budget}, concern: {concern})"
                _post(summary, "info", {"results": results})
                self._respond(200, {"results": results})
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

def start_http(port: int, catalog: List[Dict]):
    ComparisonHandler.catalog = catalog
    server = HTTPServer(("0.0.0.0", port), ComparisonHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Beauty Device Comparison API on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set, catalog: List[Dict]):
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
            with open(fpath, "r") as f:
                data = json.load(f)
            device_type = data.get("device_type", "")
            budget = data.get("budget")
            concern = data.get("concern")
            results = compare_devices(device_type, budget, concern, catalog)
            out_name = Path(fname).stem + "_comparison.json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w") as out:
                json.dump({"query": data, "results": results}, out, indent=2)
            _post(f"Processed {fname} → {out_name}", "info")
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Beauty Device Comparison Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        catalog_file = config.get("catalog_file", "beauty_devices_catalog.json")
        catalog = load_catalog(catalog_file)
        if not catalog:
            _post("Beauty device catalog empty/missing", "warning")
        else:
            _post(f"Loaded {len(catalog)} devices", "info")

        port = int(config.get("http_port", 9650))
        start_http(port, catalog)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, output_dir, processed_cache, catalog)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
