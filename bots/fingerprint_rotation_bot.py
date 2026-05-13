#!/usr/bin/env python3
"""
fingerprint_rotation_bot.py — Fingerprint Rotation Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Manages a pool of browser fingerprints (Canvas/WebGL,
user agent, screen, etc.) and rotates them across
multiple scraper instances to avoid detection.

Attachable to the Nazgul BotController (http://localhost:8765).

Configuration
─────────────
Place `fingerprint_config.json` in the same directory:
{
  "pool_file": "fingerprints.json",
  "rotation": {
    "max_uses_per_fingerprint": 3,
    "cooldown_minutes": 60,
    "strategy": "least_used"
  },
  "http_api": {
    "enabled": true,
    "port": 9123,
    "auth_token": "secret123"
  },
  "scan_interval": 300
}

Pool file (`fingerprints.json`) should contain an array of
real fingerprint objects:
[
  {
    "id": "fp_001",
    "userAgent": "Mozilla/5.0 ...",
    "screen": {"width": 1920, "height": 1080},
    "canvas_hash": "a1b2c3...",
    "webgl_hash": "d4e5f6...",
    ...
  },
  ...
]

Requirements
────────────
    pip install requests
"""

import json
import os
import time
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

HUB      = "http://localhost:8765"
BOT_ID   = "fingerprint_rotation_bot"
BOT_NAME = "Fingerprint Rotation Bot"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "fingerprint_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────────────────
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

# ── Fingerprint pool management ──────────────────────────────────────────────
class FingerprintPool:
    def __init__(self, fingerprints: list, config: dict):
        self.fingerprints = fingerprints
        self.config = config
        self.max_uses = config.get("rotation", {}).get("max_uses_per_fingerprint", 5)
        self.cooldown_min = config.get("rotation", {}).get("cooldown_minutes", 60)
        self.strategy = config.get("rotation", {}).get("strategy", "least_used")
        self.lock = threading.Lock()
        # Track usage: {fp_id: {"used_count": int, "last_used": ISO string, "assigned_to": scraper_id}}
        self.usage = {}
        for fp in fingerprints:
            fp_id = fp.get("id")
            if fp_id:
                self.usage[fp_id] = {"used_count": 0, "last_used": None, "assigned_to": None}

    def _is_fingerprint_ready(self, fp_id: str) -> bool:
        entry = self.usage.get(fp_id)
        if not entry:
            return False
        if entry["used_count"] >= self.max_uses:
            # Check cooldown if any
            if entry["last_used"]:
                last = datetime.fromisoformat(entry["last_used"])
                if datetime.now(timezone.utc) - last < timedelta(minutes=self.cooldown_min):
                    return False
                # Reset uses after cooldown
                entry["used_count"] = 0
        return True

    def get_available_fingerprint(self, scraper_id: str = None) -> dict | None:
        with self.lock:
            available_ids = [fp_id for fp_id in self.usage if self._is_fingerprint_ready(fp_id)]
            if not available_ids:
                return None
            if self.strategy == "least_used":
                # pick the one with the lowest used_count
                best_id = min(available_ids, key=lambda fid: self.usage[fid]["used_count"])
            else:
                # round_robin or random – default least_used
                best_id = available_ids[0]
            self.usage[best_id]["used_count"] += 1
            self.usage[best_id]["last_used"] = datetime.now(timezone.utc).isoformat()
            self.usage[best_id]["assigned_to"] = scraper_id or "unknown"
            # Find the fingerprint data
            for fp in self.fingerprints:
                if fp.get("id") == best_id:
                    return fp
            return None

    def release_fingerprint(self, fp_id: str) -> None:
        with self.lock:
            entry = self.usage.get(fp_id)
            if entry:
                entry["assigned_to"] = None
                # Do not reset counts until cooldown

    def get_current_status(self) -> dict:
        with self.lock:
            total = len(self.fingerprints)
            available = sum(1 for fid in self.usage if self._is_fingerprint_ready(fid))
            in_use = sum(1 for fid in self.usage if self.usage[fid]["assigned_to"] is not None)
            return {
                "total_fingerprints": total,
                "available": available,
                "in_use": in_use,
                "exhausted": total - available,
                "details": [
                    {"id": fid, "used_count": self.usage[fid]["used_count"],
                     "last_used": self.usage[fid]["last_used"],
                     "assigned_to": self.usage[fid]["assigned_to"]}
                    for fid in self.usage
                ]
            }

# ── HTTP API for scrapers ────────────────────────────────────────────────────
class FingerprintHandler(BaseHTTPRequestHandler):
    pool: FingerprintPool = None
    auth_token: str = ""

    def _set_headers(self, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        if not self.auth_token or self.headers.get("Authorization") != f"Bearer {self.auth_token}":
            self._set_headers(401)
            self.wfile.write(json.dumps({"error": "unauthorized"}).encode())
            return

        if self.path == "/fingerprint/acquire":
            scraper = self.headers.get("X-Scraper-ID", "unknown")
            fp = self.pool.get_available_fingerprint(scraper)
            if fp:
                self._set_headers(200)
                self.wfile.write(json.dumps(fp).encode())
            else:
                self._set_headers(503)
                self.wfile.write(json.dumps({"error": "no available fingerprints"}).encode())
        elif self.path.startswith("/fingerprint/release/"):
            fp_id = self.path.split("/fingerprint/release/")[1].strip("/")
            self.pool.release_fingerprint(fp_id)
            self._set_headers(200)
            self.wfile.write(json.dumps({"status": "released"}).encode())
        else:
            self._set_headers(404)
            self.wfile.write(b"{}")

    def log_message(self, *args):
        pass

def start_http_api(pool: FingerprintPool, config: dict):
    api_cfg = config.get("http_api", {})
    if not api_cfg.get("enabled"):
        return
    port = api_cfg.get("port", 9123)
    auth = api_cfg.get("auth_token", "")
    FingerprintHandler.pool = pool
    FingerprintHandler.auth_token = auth
    server = HTTPServer(("0.0.0.0", port), FingerprintHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Fingerprint HTTP API started on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    _post("Fingerprint Rotation Bot online")
    global Pool

    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        pool_file = config.get("pool_file", "fingerprints.json")
        try:
            with open(pool_file, "r") as f:
                fingerprints = json.load(f)
        except Exception as e:
            _post(f"Failed to load fingerprint pool: {e}", "error")
            time.sleep(60)
            continue

        # Initialize pool (rebuild each cycle to reflect config changes)
        global Pool
        Pool = FingerprintPool(fingerprints, config)
        start_http_api(Pool, config)

        scan_interval = int(config.get("scan_interval", 300))

        while True:
            status = Pool.get_current_status()
            if status["exhausted"] > 0:
                _post(f"Fingerprint pool exhausted: {status['exhausted']}/{status['total_fingerprints']} unavailable",
                      "warning", status)
            elif status["available"] < max(1, status["total_fingerprints"] // 2):
                _post(f"Fingerprint pool low: {status['available']}/{status['total_fingerprints']}",
                      "warning", status)
            else:
                _post(f"Fingerprint pool OK: {status['available']}/{status['total_fingerprints']} available",
                      "info", status)

            _heartbeat()
            time.sleep(scan_interval)

if __name__ == "__main__":
    Pool = None
    main()
