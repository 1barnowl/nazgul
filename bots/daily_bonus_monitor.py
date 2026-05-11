#!/usr/bin/env python3
"""
daily_bonus_monitor.py — Social Casino Bonus Monitor
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Checks public promotional pages for daily bonus amounts
at various social casinos.  Does NOT log in, collect,
or interact with games — purely informational.

SETUP
─────
1. Install dependencies:
      pip install requests beautifulsoup4

2. Configure casinos in the CASINOS list below.

3. Attach to BotController.
"""

import requests
from bs4 import BeautifulSoup
import time
import threading

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "daily_bonus_monitor_bot"
BOT_NAME = "Daily Bonus Monitor"

SCAN_INTERVAL      = 14400   # 4 hours — no need to check constantly
HEARTBEAT_INTERVAL = 300     # 5 minutes

_last_hb = 0.0
_last_hb_lock = threading.Lock()

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
    with _last_hb_lock:
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

def _wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Casino definitions ─────────────────────────────────────────────────────────
# Each entry: name, URL to check, a CSS selector to find the bonus text, and a regex pattern to extract number.
# You must update these manually as sites change.
CASINOS = [
    {
        "name": "Chumba Casino",
        "url": "https://www.chumbacasino.com/promotions",
        "selector": "div.promo-item",
        "extract_regex": r"(\d+[\.,]?\d*)\s*(?:SC|Sweeps Coins)"
    },
    {
        "name": "LuckyLand Slots",
        "url": "https://www.luckylandslots.com/promotions",
        "selector": "div.promo-offer",
        "extract_regex": r"(\d+[\.,]?\d*)\s*(?:SC|Sweeps Coins)"
    },
    # Add more social casinos here if their promotions are public
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def check_casino(casino):
    try:
        resp = requests.get(casino["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        elements = soup.select(casino["selector"])

        if not elements:
            _post(f"{casino['name']}: No bonus elements found. (selector may need updating)", "warning")
            return

        for elem in elements[:3]:  # first few matches
            text = elem.get_text(strip=True)
            import re
            match = re.search(casino["extract_regex"], text, re.IGNORECASE)
            if match:
                amount = match.group(1)
                _post(f"{casino['name']} bonus: {amount} SC found in '{text[:60]}'", "info")
            else:
                _post(f"{casino['name']} element text: '{text[:80]}'", "info")

    except requests.RequestException as e:
        _post(f"{casino['name']} fetch error: {e}", "warning")

def main():
    _wait_for_hub()
    _post("Daily Bonus Monitor online. Checking public promotion pages.", "info")

    while True:
        for casino in CASINOS:
            check_casino(casino)
            time.sleep(5)   # be polite to servers
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
