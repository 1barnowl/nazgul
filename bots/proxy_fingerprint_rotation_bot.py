#!/usr/bin/env python3
"""
proxy_fingerprint_rotation_bot.py — Proxy & Residential IP Rotation Manager
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Maintains a pool of residential / datacenter proxies, health‑checks them,
and exposes a local API that returns a fresh IP + unique browser fingerprint
on demand.  Designed to be used by other bots (e.g., purchase bots) to avoid
cart jailing and fingerprint‑based detection.

SETUP
─────
1. Install dependencies:
      pip install requests

2. (Optional) For built‑in residential proxy rotation,
   set up a provider like BrightData or Oxylabs and export:
      RESI_PROXY_URL="http://user:pass@gate.provider.com:port"
      RESI_MAX_REQUESTS=30   # rotate after N uses
      RESI_COUNTRY="us"      # optional

3. Add static proxies to the config file `proxy_pool_config.json`
   (example at bottom).

4. Attach to BotController.  The manager will start an internal
   HTTP server on port 8898 for other bots to query.

API (for other bots)
─────────────────────
   GET http://localhost:8898/identity
       → JSON: {
           "proxy": "http://user:pass@ip:port",
           "fingerprint": { "user_agent": "...", "viewport": {...}, ... }
         }
   GET http://localhost:8898/status
       → JSON: { "pool_size": 12, "healthy": 11 }
"""

import json
import os
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import requests

# ── BotController hub connection ────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "proxy_fingerprint_rotation_bot"
BOT_NAME = "Proxy & Fingerprint Manager"

CONFIG_FILE = "proxy_pool_config.json"

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 300   # health‑check every 5 minutes
_last_hb = 0.0
_lock = threading.Lock()

def _post(summary, level="info", payload=None):
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID, "bot_name": BOT_NAME,
            "summary": summary, "level": level, "payload": payload or {}
        }, timeout=5)
    except Exception:
        pass

def _heartbeat():
    global _last_hb
    with _lock:
        now = time.time()
        if now - _last_hb < HEARTBEAT_INTERVAL:
            return
        _last_hb = now
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME, "status": "online"
        }, timeout=3)
    except Exception:
        pass

def wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).ok:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Configuration ───────────────────────────────────────────────────────────
def load_config():
    default = {
        "static_proxies": [
            "http://user:pass@192.168.1.1:8080",
            "socks5://user:pass@192.168.1.2:1080"
        ],
        "residential": {
            "enabled": False,
            "gateway_url": os.getenv("RESI_PROXY_URL", ""),
            "max_uses_per_session": 30,
            "country": os.getenv("RESI_COUNTRY", "")
        },
        "fingerprint": {
            "user_agents": [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ],
            "viewports": [
                {"width": 1920, "height": 1080},
                {"width": 1440, "height": 900},
                {"width": 1366, "height": 768},
                {"width": 1280, "height": 720}
            ],
            "accept_languages": [
                "en-US,en;q=0.9",
                "en-GB,en;q=0.8",
                "en-US,en;q=0.9,fr;q=0.8"
            ]
        }
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
    # merge defaults with loaded config
    for key in default:
        if key not in cfg:
            cfg[key] = default[key]
    return cfg

CFG = load_config()

# ── Proxy pool ─────────────────────────────────────────────────────────────
class ProxyPool:
    def __init__(self):
        self.proxies = list(CFG.get("static_proxies", []))
        self.healthy = set(self.proxies)  # start as all healthy
        self.lock = threading.Lock()
        # Residential gateway rotation state
        self.resi_enabled = CFG.get("residential", {}).get("enabled", False)
        self.resi_gateway = CFG.get("residential", {}).get("gateway_url", "")
        self.resi_uses_left = 0
        self.resi_max_uses = CFG.get("residential", {}).get("max_uses_per_session", 30)
        self.resi_current_ip = None  # currently rotated IP (for sticky sessions)
        if self.resi_gateway and self.resi_enabled:
            _post("Residential gateway proxy enabled. Will rotate IPs via provider.", "info")

    def add_proxy(self, proxy_str):
        with self.lock:
            if proxy_str not in self.proxies:
                self.proxies.append(proxy_str)
                self.healthy.add(proxy_str)

    def remove_proxy(self, proxy_str):
        with self.lock:
            if proxy_str in self.proxies:
                self.proxies.remove(proxy_str)
            self.healthy.discard(proxy_str)

    def mark_dead(self, proxy_str):
        with self.lock:
            self.healthy.discard(proxy_str)
            _post(f"Proxy marked dead: {proxy_str[:60]}...", "warning")

    def mark_alive(self, proxy_str):
        with self.lock:
            self.healthy.add(proxy_str)

    def get_fresh_proxy(self, force_new_resi=False):
        """
        Return a proxy string. If residential is enabled, rotates it
        (or returns a new session). Else returns a random healthy static proxy.
        """
        if self.resi_enabled and self.resi_gateway:
            # If we already have an active residential IP and haven't exceeded uses, return it.
            if not force_new_resi and self.resi_current_ip and self.resi_uses_left > 0:
                self.resi_uses_left -= 1
                return self.resi_current_ip
            else:
                # Rotate: request a new IP from gateway (the URL itself stays same,
                # but residential proxy providers change IP on each new request cluster.
                # For sticky sessions, we can add session parameter; we'll just return gateway URL.
                # In reality, you'd keep the same gateway and the provider rotates the IP.
                # We reset uses count.
                _post("Rotating residential IP...", "info")
                self.resi_uses_left = self.resi_max_uses
                # Append a unique session string to force new IP (vendor‑dependent)
                session_id = random.randint(10000000, 99999999)
                url = self.resi_gateway
                if "?" in url:
                    url = f"{url}&session={session_id}"
                else:
                    url = f"{url}?session={session_id}"
                self.resi_current_ip = url
                return url
        # Static proxy selection
        with self.lock:
            if not self.healthy:
                # Fallback to all proxies if none are healthy
                if self.proxies:
                    _post("No healthy proxies – using a random one from the pool.", "warning")
                    return random.choice(self.proxies)
                return None
            return random.choice(list(self.healthy))

    def health_check(self):
        """Test all proxies by fetching a simple IP checker."""
        test_url = "http://httpbin.org/ip"
        all_proxies = list(self.proxies)
        for p in all_proxies:
            proxies = {"http": p, "https": p}
            try:
                resp = requests.get(test_url, proxies=proxies, timeout=10)
                if resp.ok:
                    self.mark_alive(p)
                else:
                    self.mark_dead(p)
            except Exception:
                self.mark_dead(p)
        # Also test residential gateway by fetching its external IP
        if self.resi_enabled and self.resi_gateway:
            try:
                resp = requests.get(test_url, proxies={"http": self.resi_gateway, "https": self.resi_gateway}, timeout=10)
                if resp.ok:
                    ip = resp.json().get("origin", "unknown")
                    _post(f"Residential IP check: {ip}", "info")
                else:
                    _post("Residential gateway health check failed.", "warning")
            except Exception as e:
                _post(f"Residential gateway error: {e}", "error")

pool = ProxyPool()

# ── Fingerprint generator ──────────────────────────────────────────────────
def generate_fingerprint():
    """Create a random browser fingerprint dict."""
    fp_cfg = CFG.get("fingerprint", {})
    ua = random.choice(fp_cfg.get("user_agents", ["Mozilla/5.0"]))
    viewport = random.choice(fp_cfg.get("viewports", [{"width": 1920, "height": 1080}]))
    lang = random.choice(fp_cfg.get("accept_languages", ["en-US,en;q=0.9"]))
    return {
        "user_agent": ua,
        "viewport": viewport,
        "accept_language": lang,
        "platform": random.choice(["Win32", "MacIntel", "Linux x86_64"]),
        "timezone": random.choice(["America/New_York", "America/Los_Angeles", "Europe/London"]),
        "hardware_concurrency": random.choice([4, 8, 16])
    }

# ── Local HTTP API server ──────────────────────────────────────────────────
class IdentityHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/identity":
            proxy = pool.get_fresh_proxy()
            fingerprint = generate_fingerprint()
            response = {
                "proxy": proxy,
                "fingerprint": fingerprint
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        elif self.path == "/status":
            status = {
                "pool_size": len(pool.proxies),
                "healthy": len(pool.healthy),
                "residential_active": bool(pool.resi_current_ip)
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

def start_api_server(port=8898):
    server = HTTPServer(("127.0.0.1", port), IdentityHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Identity API started on http://localhost:{port}", "info")
    return server

# ── Background tasks ───────────────────────────────────────────────────────
def health_check_loop():
    while True:
        _post("Running proxy health check...", "info")
        pool.health_check()
        time.sleep(SCAN_INTERVAL)

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    wait_for_hub()
    _post("Proxy & Fingerprint Rotation Manager online.", "info")
    # Start the local API server for other bots
    start_api_server(8898)
    # Start health check thread
    threading.Thread(target=health_check_loop, daemon=True).start()

    # Main heartbeat loop
    while True:
        _heartbeat()
        time.sleep(10)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `proxy_pool_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "static_proxies": [
    "http://user:pass@192.168.1.1:8080",
    "socks5://user:pass@192.168.1.2:1080"
  ],
  "residential": {
    "enabled": false,
    "gateway_url": "",
    "max_uses_per_session": 30,
    "country": "us"
  },
  "fingerprint": {
    "user_agents": [
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ..."
    ],
    "viewports": [ {"width": 1920, "height": 1080} ]
  }
}
"""
