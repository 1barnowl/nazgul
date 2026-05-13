#!/usr/bin/env python3
"""
broken_link_finder_bot.py — Broken Link Finder Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans high‑authority sites for 404 links to use in
link reclamation campaigns. Finds broken outgoing
links that could be replaced with your own content.

Attachable to the Nazgul BotController.

Requirements
────────────
    pip install requests beautifulsoup4

Configuration
─────────────
Place `broken_link_config.json` in the same directory:

{
  "target_pages": [
    "https://example.com/resources",
    "https://authority-site.com/blog"
  ],
  "check_interval_hours": 24,
  "max_links_per_page": 200,
  "request_delay_seconds": 1.2,
  "user_agent": "Mozilla/5.0 (compatible; LinkFinder/1.0)",
  "http_timeout": 15,
  "state_file": "broken_link_state.json",
  "report_cooldown_hours": 72
}
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Set

import requests
from bs4 import BeautifulSoup

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "broken_link_finder_bot"
BOT_NAME = "Broken Link Finder"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "broken_link_config.json"
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
        return {"last_reported": {}}   # key = "source_url|broken_url", value = ISO timestamp

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Page fetching and link extraction ────────────────────────────
def fetch_page(url: str, user_agent: str, timeout: int) -> str:
    headers = {"User-Agent": user_agent}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        _post(f"Failed to fetch {url}: {e}", "warning")
        return ""

def extract_external_links(html: str, base_url: str, max_links: int) -> List[str]:
    """Extract absolute external links from HTML, limited to max_links."""
    soup = BeautifulSoup(html, "lxml")
    links = []
    from urllib.parse import urljoin, urlparse
    base_domain = urlparse(base_url).netloc.lower()
    seen = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        # Build absolute URL
        absolute = urljoin(base_url, href)
        # Ensure it's HTTP(S) and not same domain (external)
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https"):
            domain = parsed.netloc.lower()
            if domain != base_domain and domain:  # external
                if absolute not in seen:
                    seen.add(absolute)
                    links.append(absolute)
                    if len(links) >= max_links:
                        break
    return links

# ── Link checking ────────────────────────────────────────────────
def is_link_broken(url: str, user_agent: str, timeout: int) -> bool:
    """Check if a URL returns a 404 or other error. Uses HEAD first, then GET as fallback."""
    headers = {"User-Agent": user_agent}
    try:
        # Try HEAD first
        resp = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code == 404:
            return True
        elif resp.status_code == 405:  # Method Not Allowed, fallback to GET
            resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
            resp.close()  # don't download body
            return resp.status_code == 404
        # Other 4xx/5xx also considered broken? Usually link reclamation focuses on 404. We'll stick to 404.
        return False
    except requests.exceptions.ConnectionError:
        # DNS failure / connection refused – effectively broken
        return True
    except Exception:
        return False

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Broken Link Finder Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        target_pages = config.get("target_pages", [])
        max_links = int(config.get("max_links_per_page", 200))
        delay = float(config.get("request_delay_seconds", 1.2))
        user_agent = config.get("user_agent", "Mozilla/5.0 (compatible; LinkFinder/1.0)")
        timeout = int(config.get("http_timeout", 15))
        state_file = config.get("state_file", "broken_link_state.json")
        cooldown_hours = int(config.get("report_cooldown_hours", 72))
        interval_hours = float(config.get("check_interval_hours", 24))

        state = load_state(state_file)
        now = datetime.now(timezone.utc)

        for page_url in target_pages:
            html = fetch_page(page_url, user_agent, timeout)
            if not html:
                continue
            links = extract_external_links(html, page_url, max_links)
            if not links:
                _post(f"No external links found on {page_url}", "info")
                continue

            new_broken = 0
            for link in links:
                # Check if we've reported this broken link recently (avoid spam)
                report_key = f"{page_url}|{link}"
                last_reported_str = state.get("last_reported", {}).get(report_key)
                if last_reported_str:
                    last_reported = datetime.fromisoformat(last_reported_str)
                    if now - last_reported < timedelta(hours=cooldown_hours):
                        continue  # too soon

                if is_link_broken(link, user_agent, timeout):
                    state.setdefault("last_reported", {})[report_key] = now.isoformat()
                    new_broken += 1
                    _post(
                        f"Broken link found on {page_url}: {link}",
                        "warning",
                        {
                            "source_page": page_url,
                            "broken_url": link
                        }
                    )
                time.sleep(delay)  # polite crawling

            _post(f"Scan of {page_url} complete. {new_broken} new broken links.", "info")

        save_state(state_file, state)
        _heartbeat()
        time.sleep(interval_hours * 3600)

if __name__ == "__main__":
    main()
