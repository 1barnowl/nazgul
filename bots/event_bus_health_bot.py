#!/usr/bin/env python3
"""
event_bus_health_bot.py — Event Bus Health Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors latency and queue depth of a Redis-backed
message broker. Alerts when queues exceed thresholds
or when broker latency degrades.

Attachable to the Nazgul BotController. All metrics and
alerts are posted to the Hub (http://localhost:8765).

Configuration
─────────────
Place `redis_health_config.json` in the same directory as
this script:

{
  "redis": {
    "host": "localhost",
    "port": 6379,
    "db": 0,
    "password": null
  },
  "queues": [
    {
      "key": "tasks_queue",
      "max_length": 1000
    },
    {
      "key": "events_stream",
      "max_length": 500
    }
  ],
  "latency_threshold_ms": 50,
  "ping_interval": 30
}

Requirements
────────────
    pip install redis requests
"""

import json
import os
import time
from pathlib import Path

import redis
import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "event_bus_health_bot"
BOT_NAME = "Event Bus Health Bot"

# ── Intervals ─────────────────────────────────────────────────────────────────
SCAN_INTERVAL      = 60    # seconds between full health checks
HEARTBEAT_INTERVAL = 20

_last_hb = 0.0

# ── Configuration loading ─────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).with_name("redis_health_config.json")
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path("redis_health_config.json")

def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return None

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

# ── Redis client ──────────────────────────────────────────────────────────────
def get_redis_client(cfg):
    if not cfg:
        return None
    try:
        return redis.Redis(
            host=cfg.get("host", "localhost"),
            port=cfg.get("port", 6379),
            db=cfg.get("db", 0),
            password=cfg.get("password", None),
            socket_connect_timeout=3,
            socket_timeout=3
        )
    except Exception:
        return None

# ── Health checks ─────────────────────────────────────────────────────────────
def check_latency(r: redis.Redis, threshold_ms: float) -> float | None:
    """Measure roundtrip latency in milliseconds. Returns None on failure."""
    try:
        start = time.time()
        r.ping()
        elapsed = (time.time() - start) * 1000.0
        return elapsed
    except Exception:
        return None

def check_queue_depths(r: redis.Redis, queue_configs: list) -> list[dict]:
    """Return list of {key, depth, max_length}, depth = LLEN for Redis lists"""
    results = []
    for qc in queue_configs:
        key = qc.get("key")
        max_len = qc.get("max_length", 0)
        try:
            # Assume the queue is a Redis list
            depth = r.llen(key)
        except Exception as e:
            depth = -1  # indicates failure
        results.append({"key": key, "depth": depth, "max_length": max_len})
    return results

def scan_health():
    config = load_config()
    if not config:
        _post("Could not load redis_health_config.json", "warning")
        return

    redis_cfg = config.get("redis", {})
    r = get_redis_client(redis_cfg)
    if not r:
        _post("Cannot connect to Redis broker", "error")
        return

    latency_threshold = config.get("latency_threshold_ms", 100)
    queues = config.get("queues", [])

    # ── Latency ─────────────────────────────────────────────────────────────
    latency = check_latency(r, latency_threshold)
    if latency is None:
        _post("Redis PING failed – broker appears down", "error")
        return
    elif latency > latency_threshold:
        _post(f"High Redis latency: {latency:.2f} ms (threshold {latency_threshold} ms)",
              "warning", {"latency_ms": latency, "threshold": latency_threshold})
    else:
        _post(f"Redis latency OK: {latency:.2f} ms", "info", {"latency_ms": latency})

    # ── Queue depths ────────────────────────────────────────────────────────
    depths = check_queue_depths(r, queues)
    for d in depths:
        if d["depth"] == -1:
            _post(f"Failed to read queue length for {d['key']}", "warning", {"key": d["key"]})
        elif d["max_length"] > 0 and d["depth"] > d["max_length"]:
            backpressure = d["depth"] - d["max_length"]
            _post(f"Queue {d['key']} depth {d['depth']} exceeds limit {d['max_length']} (backpressure {backpressure})",
                  "error",
                  {"queue": d["key"], "depth": d["depth"], "limit": d["max_length"]})
        else:
            _post(f"Queue {d['key']} depth {d['depth']} (limit {d['max_length'] if d['max_length'] > 0 else 'none'})",
                  "info", {"queue": d["key"], "depth": d["depth"]})

def main():
    _post("Event Bus Health Bot online — monitoring Redis", "info")
    while True:
        try:
            scan_health()
        except Exception as e:
            _post(f"Unexpected error: {e}", "error")
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
