#!/usr/bin/env python3
"""
restock_monitor_bot.py — Restock Monitor Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Polls product URLs, checks stock status, and alerts
the moment an "Out of Stock" item becomes available.

SETUP
─────
1. Install deps:
      pip install requests beautifulsoup4
   (Optional for JS sites: pip install playwright && python -m playwright install)

2. Create a config file named `restock_config.json` next to this script.
   Example at the bottom.

3. Attach to BotController.
"""

import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "restock_monitor_bot"
BOT_NAME = "Restock Monitor"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "restock_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "restock_state.json")

SCAN_INTERVAL      = 300  # 5 minutes
HEARTBEAT_INTERVAL = 20
_last_hb = 0.0

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
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME, "status": "online"
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

def _wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Config & state handling ────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        _post("Config file not found. Create restock_config.json.", "error")
        return []
    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
    return cfg.get("products", [])

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Stock status detection ─────────────────────────────────────────────────────
def check_stock_status(html, product):
    """
    Determine if product is "in_stock" or "out_of_stock" based on
    user‑provided rules. Returns (status, detail_text) e.g. ("in_stock", "In Stock").
    """
    # 1. Custom selector for stock element
    selector = product.get("stock_selector")
    soup = BeautifulSoup(html, "html.parser")
    if selector:
        elem = soup.select_one(selector)
        if elem:
            text = elem.get_text(strip=True).lower()
            if any(phrase in text for phrase in ["out of stock", "unavailable", "sold out", "not available", "coming soon"]):
                return "out_of_stock", text
            else:
                return "in_stock", text

    # 2. Custom regex for availability
    pattern = product.get("stock_regex")
    if pattern:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            text = match.group(0)
            return "in_stock", text
        # If regex is given and no match, assume out of stock
        return "out_of_stock", "no match"

    # 3. Out‑of‑stock keywords in the whole page
    body = soup.get_text().lower()
    if any(kw in body for kw in ["out of stock", "currently unavailable", "sold out",
                                  "not available", "this item cannot be added",
                                  "unavailable", "sign up to be notified",
                                  "temporarily out of stock"]):
        return "out_of_stock", "found out-of-stock phrase"

    # 4. In‑stock indicators like "Add to Cart", "Buy Now", "In Stock"
    if any(kw in body for kw in ["add to cart", "buy now", "in stock", "available",
                                  "add to basket"]):
        return "in_stock", "found in-stock phrase"

    # Default: ambiguous, treat as unknown (no alert)
    return "unknown", "no clear indicator"

def fetch_page(url, use_js=False):
    if use_js and HAS_PLAYWRIGHT:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            content = page.content()
            browser.close()
            return content
    else:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            _post(f"Request error for {url}: {e}", "warning")
            return None

# ── Main scan ─────────────────────────────────────────────────────────────────
def scan(products, previous_state):
    new_state = {}
    now = datetime.utcnow().isoformat() + "Z"

    for product in products:
        url = product.get("url")
        name = product.get("name", url)
        use_js = product.get("use_playwright", False) and HAS_PLAYWRIGHT

        html = fetch_page(url, use_js)
        if html is None:
            _post(f"{name}: page fetch failed", "warning")
            new_state[url] = previous_state.get(url, {})  # keep old
            continue

        status, detail = check_stock_status(html, product)
        new_state[url] = {
            "status": status,
            "detail": detail,
            "last_checked": now
        }

        old = previous_state.get(url, {})
        old_status = old.get("status", "unknown")

        if status == "out_of_stock":
            if old_status != "out_of_stock":
                _post(f"{name}: ❌ Out of Stock ({detail})", "info", {"product": name, "url": url, "status": status})
            continue

        if status == "in_stock":
            if old_status != "in_stock":
                level = "error"   # big alert for restock
                msg = f"🔥 RESTOCK! {name} is now available ({detail})"
                _post(msg, level, {"product": name, "url": url, "status": status, "previous": old_status})
            else:
                # Still in stock – just a heartbeat
                _post(f"{name}: Still in stock ({detail})", "info", {"product": name, "url": url, "status": status})
            continue

        # unknown
        if old_status != "unknown":
            _post(f"{name}: stock status unclear ({detail})", "warning", {"product": name, "url": url, "status": status})

    _heartbeat()
    save_state(new_state)

def main():
    _wait_for_hub()
    products = load_config()
    if not products:
        _post("No products configured. Bot idle.", "error")
        while True:
            _heartbeat()
            time.sleep(60)

    previous_state = load_state()
    _post(f"Restock Monitor online — watching {len(products)} items.", "info")

    while True:
        scan(products, previous_state)
        previous_state = load_state()  # reload after scan
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `restock_config.json` (place next to the bot)
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "products": [
    {
      "name": "PlayStation 5 Console",
      "url": "https://www.amazon.com/dp/B0BCNKKZ91",
      "stock_selector": "#availability span",   // e.g. the element containing "In Stock"
      "stock_regex": null,
      "use_playwright": false
    },
    {
      "name": "NVIDIA RTX 4090",
      "url": "https://www.bestbuy.com/site/nvidia-geforce-rtx-4090/6521432.p",
      "stock_selector": ".add-to-cart-button",
      "stock_regex": null,
      "use_playwright": false
    }
  ]
}
"""
