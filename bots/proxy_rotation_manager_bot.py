#!/usr/bin/env python3
"""
proxy_rotation_manager_bot.py — Proxy Rotation Manager Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors residential proxy health, bans, and latency.
Dynamically ranks proxies based on performance and reports
the best available endpoints to the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `proxy_config.json` in the same directory as this script:

{
  "proxy_provider": "static",
  "proxies": [
    "http://user:pass@res01.example.com:8080",
    "socks5://res02.example.com:1080",
    "http://username:pass@gate.abc.net:9999"
  ],
  "test_url": "https://httpbin.org/ip",
  "test_interval": 60,
  "latency_threshold_ms": 2000,
  "success_threshold_percent": 75,
  "max_consecutive_failures": 3,
  "ban_patterns": ["access denied", "blocked", "captcha"]
}
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "proxy_rotation_manager_bot"
BOT_NAME = "Proxy Rotation Manager"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

# ── Configuration path ────────────────────────────────────────────────────────
CONFIG_NAME = "proxy_config.json"
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

# ── Proxy state tracking ──────────────────────────────────────────────────────
class ProxyPool:
    def __init__(self):
        self.proxies: Dict[str, dict] = {}   # proxy_url => stats
        self.config = {}

    def load_proxies(self, provider: str, config: dict) -> List[str]:
        if provider == "static":
            return config.get("proxies", [])
        # Future: implement other providers (e.g. webshare API)
        return []

    def init_pool(self, config: dict):
        self.config = config
        provider = config.get("proxy_provider", "static")
        urls = self.load_proxies(provider, config)
        for url in urls:
            if url not in self.proxies:
                self.proxies[url] = {
                    "enabled": True,
                    "success_count": 0,
                    "fail_count": 0,
                    "consecutive_fails": 0,
                    "last_latency_ms": None,
                    "last_checked": None,
                    "banned": False,
                    "ban_reason": ""
                }

    def test_proxy(self, proxy_url: str, test_url: str, ban_patterns: List[str]) -> Tuple[bool, float, bool, str]:
        """Return (success, latency_ms, is_banned, ban_detail)"""
        proxies = {"http": proxy_url, "https": proxy_url}
        try:
            start = time.perf_counter()
            resp = requests.get(test_url, proxies=proxies, timeout=10, allow_redirects=False)
            elapsed = (time.perf_counter() - start) * 1000
        except Exception as e:
            # Connection error / timeout -> fail
            return False, 0.0, False, str(e)

        # Check for ban signals
        try:
            body = resp.text.lower()
        except Exception:
            body = ""
        banned = False
        reason = ""
        if resp.status_code in (403, 407, 503):
            banned = True
            reason = f"HTTP {resp.status_code}"
        else:
            for pattern in ban_patterns:
                if pattern.lower() in body:
                    banned = True
                    reason = f"Body matched '{pattern}'"
                    break

        success = resp.status_code == 200 and not banned
        return success, elapsed, banned, reason

    def update_proxy_stats(self, url: str, success: bool, latency: float, banned: bool, ban_reason: str):
        entry = self.proxies[url]
        entry["last_checked"] = datetime.now(timezone.utc).isoformat()
        entry["last_latency_ms"] = round(latency, 2)
        if banned:
            entry["banned"] = True
            entry["ban_reason"] = ban_reason
            entry["enabled"] = False
        else:
            entry["banned"] = False
            entry["ban_reason"] = ""
            entry["enabled"] = True

        if success:
            entry["success_count"] += 1
            entry["consecutive_fails"] = 0
        else:
            entry["fail_count"] += 1
            entry["consecutive_fails"] += 1

    def prune_disabled(self):
        # Optionally remove proxies that are permanently disabled based on config
        pass

    def get_best_proxies(self, max_count: int = 5) -> List[dict]:
        """Return top proxies sorted by health (enabled, low latency, high success)."""
        candidates = []
        for url, entry in self.proxies.items():
            if not entry["enabled"]:
                continue
            latency = entry["last_latency_ms"] or 9999
            candidates.append((latency, url, entry))
        candidates.sort(key=lambda x: x[0])
        return [{"url": url, "latency_ms": entry["last_latency_ms"],
                 "success_count": entry["success_count"],
                 "fail_count": entry["fail_count"]}
                for _, url, entry in candidates[:max_count]]

    def summary(self) -> dict:
        total = len(self.proxies)
        active = sum(1 for e in self.proxies.values() if e["enabled"])
        banned = sum(1 for e in self.proxies.values() if e["banned"])
        return {
            "total": total,
            "active": active,
            "banned": banned,
            "proxies": [
                {
                    "url": url,
                    "enabled": e["enabled"],
                    "banned": e["banned"],
                    "latency_ms": e["last_latency_ms"],
                    "success_count": e["success_count"],
                    "fail_count": e["fail_count"]
                } for url, e in self.proxies.items()
            ]
        }

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _post("Proxy Rotation Manager Bot online")
    pool = ProxyPool()

    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        # Initialize or update pool
        pool.init_pool(config)

        test_url = config.get("test_url", "https://httpbin.org/ip")
        ban_patterns = config.get("ban_patterns", [])
        latency_threshold = float(config.get("latency_threshold_ms", 2000))
        max_consec_fails = int(config.get("max_consecutive_failures", 3))

        # Test all proxies
        for proxy_url in pool.proxies:
            success, latency, banned, ban_reason = pool.test_proxy(proxy_url, test_url, ban_patterns)
            pool.update_proxy_stats(proxy_url, success, latency, banned, ban_reason)

        # Alert on critical failures
        for url, entry in pool.proxies.items():
            if entry["banned"]:
                _post(f"Proxy BANNED: {url} ({entry['ban_reason']})", "error", {"proxy": url})
            elif entry["consecutive_fails"] >= max_consec_fails:
                _post(f"Proxy DEGRADED: {url} {entry['consecutive_fails']} consecutive failures", "warning", {"proxy": url})
            elif entry["last_latency_ms"] and entry["last_latency_ms"] > latency_threshold:
                _post(f"Proxy HIGH LATENCY: {url} ({entry['last_latency_ms']:.0f} ms)", "warning", {"proxy": url})

        # Post a periodic summary
        summary = pool.summary()
        best = pool.get_best_proxies(5)
        _post(f"Proxy pool status: {summary['active']}/{summary['total']} active, {summary['banned']} banned. "
              f"Top proxy: {best[0]['url'] if best else 'none'} ({best[0]['latency_ms']:.0f} ms)" if best else "No active proxies",
              "info", {"summary": summary, "best_proxies": best})

        _heartbeat()
        time.sleep(int(config.get("test_interval", 60)))

if __name__ == "__main__":
    main()
