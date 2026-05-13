#!/usr/bin/env python3
"""
perfume_sampler_bot.py — Personalised Perfume Sampler Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Recommends sample sets from Scentbird / LuckyScent based on
preferred scent notes (floral, woody, fresh, etc.) with
affiliate links. Other bots can request recommendations
via HTTP API.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `perfume_sampler_config.json` in the same directory:

{
  "catalog_file": "perfume_catalog.json",
  "http_port": 9620,
  "file_watch": {
    "enabled": false,
    "directory": "/data/perfume_requests",
    "output_directory": "/data/perfume_recommendations"
  },
  "heartbeat_interval": 30
}

The catalog file (`perfume_catalog.json`) should contain an array of objects:

[
  {
    "name": "Scentbird - Floral Fantasy",
    "brand": "Scentbird",
    "notes": ["floral", "rose", "jasmine"],
    "affiliate_link": "https://www.scentbird.com/sample/floral-fantasy?ref=your_id",
    "description": "A romantic blend of rose, jasmine, and peony."
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
from typing import List, Dict

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "perfume_sampler_bot"
BOT_NAME = "Perfume Sampler Affiliate Bot"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "perfume_sampler_config.json"
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
def load_catalog(catalog_path: str) -> List[Dict]:
    try:
        with open(catalog_path, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []

def match_perfumes(notes: List[str], catalog: List[Dict]) -> List[Dict]:
    """Return perfumes that match any of the given notes (case‑insensitive)."""
    results = []
    notes_lower = [n.strip().lower() for n in notes if n.strip()]
    for perfume in catalog:
        perfume_notes = [n.lower() for n in perfume.get("notes", [])]
        if any(n in perfume_notes for n in notes_lower):
            results.append(perfume)
    return results

# ── HTTP API handler ─────────────────────────────────────────────
class PerfumeHandler(BaseHTTPRequestHandler):
    catalog: List[Dict] = []

    def do_POST(self):
        if self.path == "/recommend":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                notes = data.get("notes", [])
                if not isinstance(notes, list):
                    notes = [notes]
                matches = match_perfumes(notes, self.catalog)
                summary = f"Request for notes {notes} → {len(matches)} recommendations"
                _post(summary, "info", {"notes": notes, "count": len(matches)})
                self._respond(200, {"recommendations": matches})
            except Exception as e:
                self._respond(400, {"error": str(e)})
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
    PerfumeHandler.catalog = catalog
    server = HTTPServer(("0.0.0.0", port), PerfumeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Perfume Sampler API listening on port {port}", "info")

# ── File watcher ─────────────────────────────────────────────────
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
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            notes = data.get("notes", [])
            matches = match_perfumes(notes, catalog)
            out_name = Path(fname).stem + "_recommendations.json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w", encoding="utf-8") as out:
                json.dump({"notes": notes, "recommendations": matches}, out, indent=2)
            _post(f"Processed {fname} → {out_name}", "info", {"file": fname})
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Perfume Sampler Affiliate Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        catalog_file = config.get("catalog_file", "perfume_catalog.json")
        catalog = load_catalog(catalog_file)
        if not catalog:
            _post("Perfume catalog is empty or missing", "warning")
        else:
            _post(f"Loaded {len(catalog)} perfumes", "info")

        port = int(config.get("http_port", 9620))
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
