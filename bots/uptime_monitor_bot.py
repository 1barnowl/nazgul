#!/usr/bin/env python3
"""
uptime_monitor_bot.py — Uptime Monitor Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Checks if critical web endpoints or API gateways are
reachable and reports up/down status with response
times to the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `uptime_monitor_config.json` in the same directory:

{
  "endpoints": [
    {
      "url": "https://api.example.com/health",
      "method": "GET",
      "expected_status": 200,
      "timeout": 10,
      "name": "Main API Gateway"
    },
    {
      "url": "https://www.example.com",
      "method": "HEAD",
      "expected_status": [200, 301, 302],
      "timeout": 5,
      "name": "Website"
    }
  ],
  "check_interval": 60
}
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "uptime_monitor_bot"
BOT_NAME = "Uptime Monitor"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

# ── Configuration path ────────────────────────────────────────────────────────
CONFIG_NAME = "uptime_monitor_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ────────────────────────────────────────────────────────────────
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

# ── Endpoint checker ──────────────────────────────────────────────────────────
def check_endpoint(cfg: dict) -> dict:
    url = cfg["url"]
    method = cfg.get("method", "GET").upper()
    expected_status = cfg.get("expected_status", 200)
    timeout = cfg.get("timeout", 10)
    name = cfg.get("name", url)

    # Normalise expected status to a set of ints
    if isinstance(expected_status, int):
        expected_set = {expected_status}
    elif isinstance(expected_status, list):
        expected_set = set(expected_status)
    else:
        expected_set = {200}

    result = {
        "name": name,
        "url": url,
        "method": method,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        start = time.perf_counter()
        resp = requests.request(method, url, timeout=timeout, allow_redirects=True)
        elapsed = (time.perf_counter() - start) * 1000.0
        result["status_code"] = resp.status_code
        result["latency_ms"] = round(elapsed, 2)
        result["up"] = resp.status_code in expected_set
        result["error"] = None
    except requests.exceptions.Timeout:
        result["status_code"] = None
        result["latency_ms"] = None
        result["up"] = False
        result["error"] = "timeout"
    except requests.exceptions.ConnectionError as e:
        result["status_code"] = None
        result["latency_ms"] = None
        result["up"] = False
        result["error"] = f"connection_error: {str(e)[:100]}"
    except Exception as e:
        result["status_code"] = None
        result["latency_ms"] = None
        result["up"] = False
        result["error"] = f"exception: {str(e)[:100]}"

    return result

def main():
    _post("Uptime Monitor Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        endpoints = config.get("endpoints", [])
        check_interval = int(config.get("check_interval", 60))

        for ep_cfg in endpoints:
            result = check_endpoint(ep_cfg)
            name = result["name"]
            if result["up"]:
                summary = f"{name} UP — {result['latency_ms']:.0f} ms"
                level = "info"
            else:
                summary = f"{name} DOWN — {result.get('error', 'unknown error')}"
                level = "error"
            _post(summary, level, result)
            _heartbeat()

        time.sleep(check_interval)

if __name__ == "__main__":
    main()
