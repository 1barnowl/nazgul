#!/usr/bin/env python3
"""
website_visitor_intel_bot.py — Website Visitor Intel Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Parses server logs (e.g., nginx) to identify companies
visiting your site by mapping IP addresses to company
names via IP‑API.com or MaxMind GeoLite2‑ASN.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests geoip2  (if using MaxMind local)

Configuration
─────────────
Place `visitor_intel_config.json` in the same directory:

{
  "log": {
    "path": "/var/log/nginx/access.log",
    "format": "combined"            // "combined" or custom regex
  },
  "ip_lookup": {
    "provider": "ipapi",           // "ipapi" or "maxmind"
    "ipapi_url": "http://ip-api.com/json/{ip}?fields=org",
    "maxmind_db": "/usr/share/GeoIP/GeoLite2-ASN.mmdb"
  },
  "poll_interval_seconds": 300,
  "state_file": "visitor_intel_state.json",
  "min_hits_to_report": 3
}
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "website_visitor_intel_bot"
BOT_NAME = "Website Visitor Intel"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "visitor_intel_config.json"
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

# ── State persistence ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_position": 0, "last_inode": None}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── IP lookup providers ──────────────────────────────────────────
class IPLookup:
    def lookup(self, ip: str) -> str:
        raise NotImplementedError

class IPApiLookup(IPLookup):
    def __init__(self, url_template: str):
        self.url_template = url_template
        self._last_request = 0.0
        self._rate_limit = 1.5  # 40 req/min = ~1.5 sec between

    def lookup(self, ip: str) -> str:
        # Rate limit
        elapsed = time.time() - self._last_request
        if elapsed < self._rate_limit:
            time.sleep(self._rate_limit - elapsed)
        self._last_request = time.time()

        url = self.url_template.format(ip=ip)
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("org", "Unknown")
        except Exception:
            pass
        return "Unknown"

class MaxMindLookup(IPLookup):
    def __init__(self, db_path: str):
        import geoip2.database
        self.reader = geoip2.database.Reader(db_path)

    def lookup(self, ip: str) -> str:
        try:
            response = self.reader.asn(ip)
            return response.autonomous_system_organization or "Unknown"
        except Exception:
            return "Unknown"

# ── Log parsing ──────────────────────────────────────────────────
# Combined format regex: $remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent"
COMBINED_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[.*?\] ".*?" \d+ \d+ ".*?" "(?P<ua>.*?)"'
)

def extract_ip(line: str, log_format: str) -> str:
    """Extract IP address from a log line."""
    if log_format == "combined":
        m = COMBINED_RE.match(line)
        if m:
            return m.group("ip")
    else:
        # Fallback: just the first whitespace-separated token
        return line.split()[0] if line.split() else ""
    return ""

# ── File tailing with rotation handling ─────────────────────────
def read_new_lines(filepath: str, state: dict) -> list[str]:
    """Return new lines since last read, handle log rotation."""
    try:
        stat = os.stat(filepath)
        current_size = stat.st_size
        current_inode = stat.st_ino
    except FileNotFoundError:
        return []

    # Detect rotation: file smaller than previous position or inode changed
    last_pos = state.get("last_position", 0)
    last_inode = state.get("last_inode")

    if last_inode != current_inode or current_size < last_pos:
        last_pos = 0  # rotation, start from beginning

    if current_size <= last_pos:
        return []

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        f.seek(last_pos)
        new_data = f.read()
        last_pos = f.tell()  # update position

    state["last_position"] = last_pos
    state["last_inode"] = current_inode

    return new_data.splitlines()

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Website Visitor Intel Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        log_cfg = config.get("log", {})
        log_path = log_cfg.get("path")
        log_format = log_cfg.get("format", "combined")
        if not log_path or not os.path.exists(log_path):
            _post(f"Log file not found: {log_path}", "error")
            time.sleep(60)
            continue

        # IP lookup setup
        lookup_cfg = config.get("ip_lookup", {})
        provider_name = lookup_cfg.get("provider", "ipapi")
        lookup = None
        if provider_name == "ipapi":
            url_template = lookup_cfg.get("ipapi_url", "http://ip-api.com/json/{ip}?fields=org")
            lookup = IPApiLookup(url_template)
        elif provider_name == "maxmind":
            db_path = lookup_cfg.get("maxmind_db")
            if not db_path or not os.path.exists(db_path):
                _post("MaxMind DB not found", "error")
                time.sleep(60)
                continue
            lookup = MaxMindLookup(db_path)
        else:
            _post(f"Unsupported IP lookup provider: {provider_name}", "error")
            time.sleep(60)
            continue

        poll_interval = int(config.get("poll_interval_seconds", 300))
        state_file = config.get("state_file", "visitor_intel_state.json")
        min_hits = int(config.get("min_hits_to_report", 3))
        state = load_state(state_file)

        while True:
            lines = read_new_lines(log_path, state)
            if lines:
                company_counts = {}
                for line in lines:
                    ip = extract_ip(line, log_format)
                    if not ip:
                        continue
                    # Simple cache to avoid duplicate lookups within the same batch
                    company = company_counts.get(ip)
                    if company is None:
                        company = lookup.lookup(ip)
                    company_counts[company] = company_counts.get(company, 0) + 1

                # Report each company that exceeds min_hits
                for company, count in company_counts.items():
                    if count >= min_hits:
                        _post(
                            f"Company '{company}' visited {count} times",
                            "info" if count < 10 else "warning",
                            {"company": company, "hits": count}
                        )
                save_state(state_file, state)
            _heartbeat()
            time.sleep(poll_interval)

if __name__ == "__main__":
    main()
