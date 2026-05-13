#!/usr/bin/env python3
"""
rate_limit_enforcer_bot.py — Rate Limit Enforcer Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Centralized token bucket that ensures no single bot
exceeds global API rate limits. Other bots request a
token before making an API call via the built-in HTTP
API.

Attachable to the Nazgul BotController (http://localhost:8765).

Configuration
─────────────
Place `rate_limiter_config.json` in the same directory:

{
  "limits": [
    {
      "bot_id": "momentum_chaser_bot",
      "endpoint": "yahoo_finance",
      "max_rps": 2.0,
      "burst": 4
    },
    {
      "bot_id": "*",
      "endpoint": "yahoo_finance",
      "max_rps": 0.5,
      "burst": 1
    }
  ],
  "default_burst": 1,
  "http_port": 9229,
  "auth_token": null,
  "report_interval": 60
}

Requirements
────────────
    pip install requests
"""

import json
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

HUB      = "http://localhost:8765"
BOT_ID   = "rate_limit_enforcer_bot"
BOT_NAME = "Rate Limit Enforcer"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "rate_limiter_config.json"
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

# ── Token bucket implementation ─────────────────────────────────────────────
class TokenBucket:
    def __init__(self, rate: float, burst: int):
        self.rate = rate          # tokens per second
        self.burst = burst        # max tokens
        self.tokens = float(burst)
        self.last_update = time.monotonic()

    def acquire(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_update = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

# ── Rate limiter state ──────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, config: dict):
        self.lock = threading.Lock()
        self.buckets: dict[str, TokenBucket] = {}  # key "bot_id:endpoint"
        self.limits = config.get("limits", [])
        self.default_burst = config.get("default_burst", 1)
        self.stats_requests = defaultdict(int)     # total acquire attempts
        self.stats_allowed = defaultdict(int)
        self.stats_denied = defaultdict(int)

    def _bucket_key(self, bot_id: str, endpoint: str) -> str:
        return f"{bot_id}:{endpoint}"

    def is_allowed(self, bot_id: str, endpoint: str = "default") -> bool:
        """Check if bot can make a call. Returns True/False."""
        key = self._bucket_key(bot_id, endpoint)
        with self.lock:
            # Get or create bucket based on matching rules
            bucket = self.buckets.get(key)
            if not bucket:
                # Find applicable rule: first exact bot match, then wildcard
                rule = None
                for r in self.limits:
                    if r.get("bot_id") == bot_id and r.get("endpoint") == endpoint:
                        rule = r
                        break
                if not rule:
                    for r in self.limits:
                        if r.get("bot_id") == "*" and r.get("endpoint") == endpoint:
                            rule = r
                            break
                rate = float(rule["max_rps"]) if rule else 1.0
                burst = int(rule.get("burst", self.default_burst)) if rule else self.default_burst
                bucket = TokenBucket(rate, burst)
                self.buckets[key] = bucket

            self.stats_requests[key] += 1
            if bucket.acquire():
                self.stats_allowed[key] += 1
                return True
            else:
                self.stats_denied[key] += 1
                return False

    def get_stats(self) -> dict:
        with self.lock:
            stats = {}
            for key, bucket in self.buckets.items():
                bot_id, endpoint = key.split(":", 1)
                stats[key] = {
                    "bot_id": bot_id,
                    "endpoint": endpoint,
                    "requests": self.stats_requests.get(key, 0),
                    "allowed": self.stats_allowed.get(key, 0),
                    "denied": self.stats_denied.get(key, 0),
                    "tokens_available": bucket.tokens,
                    "burst": bucket.burst,
                    "rate": bucket.rate
                }
            return stats

# ── HTTP API for bots to check rate limits ──────────────────────────────────
class RateLimitHandler(BaseHTTPRequestHandler):
    limiter: RateLimiter = None
    auth_token: str = None

    def _set_headers(self, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        if self.path == "/acquire":
            bot_id = self.headers.get("X-Bot-ID", "unknown")
            endpoint = self.headers.get("X-Endpoint", "default")
            allowed = self.limiter.is_allowed(bot_id, endpoint)
            self._set_headers(200)
            self.wfile.write(json.dumps({"allowed": allowed}).encode())
        elif self.path == "/stats":
            # Optional auth
            if self.auth_token and self.headers.get("Authorization") != f"Bearer {self.auth_token}":
                self._set_headers(401)
                self.wfile.write(json.dumps({"error": "unauthorized"}).encode())
                return
            stats = self.limiter.get_stats()
            self._set_headers(200)
            self.wfile.write(json.dumps(stats, default=str).encode())
        else:
            self._set_headers(404)
            self.wfile.write(b"{}")

    def log_message(self, *args):
        pass

def start_http_api(limiter: RateLimiter, config: dict):
    port = config.get("http_port", 9229)
    auth = config.get("auth_token")
    RateLimitHandler.limiter = limiter
    RateLimitHandler.auth_token = auth
    server = HTTPServer(("0.0.0.0", port), RateLimitHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Rate limit HTTP API started on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    _post("Rate Limit Enforcer Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        # Recreate limiter to apply config changes
        limiter = RateLimiter(config)
        start_http_api(limiter, config)
        report_interval = int(config.get("report_interval", 60))

        while True:
            stats = limiter.get_stats()
            denied_count = sum(1 for v in stats.values() if v["denied"] > 0)
            total_allowed = sum(v["allowed"] for v in stats.values())
            _post(f"Rate limiter stats: {total_allowed} allowed calls, {denied_count} keys with denials",
                  "info", {"stats": stats})
            _heartbeat()
            time.sleep(report_interval)

if __name__ == "__main__":
    main()
