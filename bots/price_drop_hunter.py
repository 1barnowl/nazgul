#!/usr/bin/env python3
"""
price_drop_hunter.py — Price Drop Hunter
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors product prices from any URL and alerts
when the price drops below your threshold.

Uses requests + BeautifulSoup with configurable selectors.
For JavaScript‑heavy sites, a fallback to Playwright is available.

SETUP
─────
1. Install deps:
      pip install requests beautifulsoup4
   (optional for JS sites: pip install playwright && python -m playwright install)

2. Create a config file named `price_drop_config.json` next to this script:
   See the example at the bottom of this file.

3. Attach to BotController as usual.
"""

import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "price_drop_hunter"
BOT_NAME = "Price Drop Hunter"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "price_drop_config.json")

SCAN_INTERVAL      = 600  # 10 minutes between full scan cycles
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

# ── Config handling ────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        _post("Config file not found. Create price_drop_config.json.", "error")
        return []
    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
    return cfg.get("products", [])

# ── Price extraction ────────────────────────────────────────────────────────────
def extract_price_from_html(html, product):
    """Try to extract a numeric price from HTML using user‑supplied rules."""
    # 1. Try CSS selector if given
    selector = product.get("selector")
    if selector:
        soup = BeautifulSoup(html, "html.parser")
        elem = soup.select_one(selector)
        if elem:
            text = elem.get_text(strip=True)
            price = __parse_price(text)
            if price:
                return price

    # 2. Try regex pattern
    pattern = product.get("regex")
    if pattern:
        match = re.search(pattern, html)
        if match:
            text = match.group(1) if match.lastindex else match.group()
            price = __parse_price(text)
            if price:
                return price

    # 3. Fallback: common Amazon & general patterns
    #    Amazon often has <span class="a-price-whole">
    soup = BeautifulSoup(html, "html.parser")
    # Amazon style
    whole = soup.select_one("span.a-price-whole")
    fraction = soup.select_one("span.a-price-fraction")
    if whole:
        whole_text = whole.get_text(strip=True).replace(",", "")
        frac_text = fraction.get_text(strip=True) if fraction else "00"
        try:
            return float(whole_text + "." + frac_text)
        except ValueError:
            pass

    # Generic: find any element with a dollar amount pattern
    body = soup.get_text()
    return __parse_price(body)

def __parse_price(text):
    """Extract first dollar amount from a string. Handles $1,234.56 etc."""
    # Remove non‑numeric except '.' and '-'
    # Look for pattern: optional $, optional commas, digits, optional decimal
    match = re.search(r'(?:\$)?([\d,]+(?:\.\d{1,2})?)', text)
    if match:
        num_str = match.group(1).replace(",", "")
        try:
            return float(num_str)
        except ValueError:
            pass
    return None

def get_page_content(url, use_js=False):
    """Fetch page HTML. Falls back to Playwright if use_js is True and available."""
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
            _post(f"Request failed for {url}: {e}", "warning")
            return None

# ── Main scan ─────────────────────────────────────────────────────────────────
def scan(products):
    for product in products:
        url = product.get("url")
        name = product.get("name", url)
        threshold = float(product.get("threshold", 0))

        # Decide whether to use JS rendering (opt‑in per product)
        use_js = product.get("use_playwright", False) and HAS_PLAYWRIGHT
        html = get_page_content(url, use_js)
        if html is None:
            _post(f"{name}: could not fetch page", "warning", {"url": url})
            continue

        price = extract_price_from_html(html, product)
        if price is None:
            _post(f"{name}: price not found – check selectors", "warning",
                  {"url": url})
            continue

        payload = {
            "product": name,
            "url": url,
            "price": price,
            "threshold": threshold,
            "below": price <= threshold
        }

        level = "info"
        if price <= threshold:
            level = "error"
            _post(f"{name}: ${price:.2f}  BELOW target ${threshold:.2f}  – buy now!",
                  level, payload)
        else:
            diff = price - threshold
            _post(f"{name}: ${price:.2f}  (${diff:.2f} above threshold)",
                  "info", payload)

    _heartbeat()

def main():
    _wait_for_hub()
    products = load_config()
    if not products:
        _post("No products configured. Bot idle.", "error")
        while True:
            _heartbeat()
            time.sleep(60)

    _post(f"Price Drop Hunter online – monitoring {len(products)} products.", "info")

    while True:
        scan(products)
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `price_drop_config.json` (place next to the bot)
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "products": [
    {
      "name": "Sony WH-1000XM5",
      "url": "https://www.amazon.com/dp/B09XS7JWHH",
      "threshold": 299.00,
      "selector": "span.a-price-whole",   (optional)
      "regex": null,
      "use_playwright": false
    },
    {
      "name": "Nintendo Switch OLED",
      "url": "https://www.bestbuy.com/site/nintendo-switch-oled-model-neon/6467206.p",
      "threshold": 300.00,
      "selector": "[data-testid='price-now']",  (example Best Buy selector)
      "regex": null,
      "use_playwright": false
    }
  ]
}
"""
