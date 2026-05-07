#!/usr/bin/env python3
"""
online_arbitrage_bot.py — Online Arbitrage Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scrapes clearance products from a source site, looks up
the resale price on a target marketplace (e.g., Amazon),
and alerts when a profitable flip is detected.

Requires Python 3.8+.
Install dependencies:
    pip install requests beautifulsoup4
    (optional) pip install playwright && python -m playwright install

SETUP
─────
1. Create a config file named `online_arbitrage_config.json`
   next to this script. See the example at the bottom.
2. Attach to BotController as usual.
"""

import json
import os
import re
import time
import hashlib
import urllib.parse
from datetime import datetime
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "online_arbitrage_bot"
BOT_NAME = "Online Arbitrage Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "online_arbitrage_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "online_arbitrage_state.json")

SCAN_INTERVAL      = 1800   # 30 minutes between full scans
HEARTBEAT_INTERVAL = 20
_last_hb = 0.0

# ── Hub helpers ────────────────────────────────────────────────────────────────
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

# ── Config & state ─────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        _post("Config file missing. Create online_arbitrage_config.json.", "error")
        return []
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Web helpers ───────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

def get_page(url, use_js=False, timeout=15):
    """Fetch page HTML via requests, or Playwright for JS pages."""
    if use_js and HAS_PLAYWRIGHT:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            html = page.content()
            browser.close()
            return html
    else:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            _post(f"HTTP error for {url}: {e}", "warning")
            return None

def clean_price(text):
    """Extract a float price from a string like '$ 19.99' or '19.99'."""
    if not text:
        return None
    # Remove any non‑numeric except '.' and possibly a leading '-'
    nums = re.findall(r'\d[\d,]*\.?\d{0,2}', text.replace(",", ""))
    if not nums:
        return None
    try:
        return float(nums[0])
    except ValueError:
        return None

# ── Product matching ───────────────────────────────────────────────────────────
def title_similarity(a, b):
    """Simple token set similarity between two product titles."""
    a_tok = set(re.sub(r'[^\w\s]', '', a.lower()).split())
    b_tok = set(re.sub(r'[^\w\s]', '', b.lower()).split())
    if not a_tok or not b_tok:
        return 0.0
    intersection = a_tok.intersection(b_tok)
    union = a_tok.union(b_tok)
    return len(intersection) / len(union)

# ── Site‑specific scrapers ────────────────────────────────────────────────────
def scrape_source(source_config):
    """
    Returns a list of product dicts from the source clearance page.
    Required keys in source_config:
        url, product_selector, title_selector, price_selector, link_selector (optional)
    """
    url = source_config["url"]
    use_js = source_config.get("use_playwright", False)
    html = get_page(url, use_js)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    product_cards = soup.select(source_config["product_selector"])
    results = []
    for card in product_cards:
        title_elem = (card.select_one(source_config["title_selector"])
                      if "title_selector" in source_config else None)
        price_elem = (card.select_one(source_config["price_selector"])
                      if "price_selector" in source_config else None)
        link_elem  = (card.select_one(source_config.get("link_selector", "a"))
                      if "link_selector" in source_config else None)
        title = title_elem.get_text(strip=True) if title_elem else ""
        price_text = price_elem.get_text(strip=True) if price_elem else ""
        price = clean_price(price_text)
        link = link_elem.get("href") if link_elem else ""
        if not title or price is None:
            continue
        # Normalize link
        if link and not link.startswith("http"):
            link = urllib.parse.urljoin(url, link)
        results.append({
            "title": title,
            "price": price,
            "url": link or url,
            "source": source_config.get("name", url)
        })
    return results

def lookup_target(product_title, target_config):
    """
    Search product on target marketplace and return price of best match.
    target_config must define search_url_pattern (with {query}) and
    selectors for search results (item_selector, title_selector, price_selector).
    """
    search_url = target_config["search_url_pattern"].format(
        query=urllib.parse.quote(product_title)
    )
    use_js = target_config.get("use_playwright", False)
    html = get_page(search_url, use_js, timeout=20)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(target_config["item_selector"])
    best_price = None
    for item in items:
        title_elem = item.select_one(target_config["title_selector"])
        price_elem = item.select_one(target_config["price_selector"])
        if not title_elem:
            continue
        title = title_elem.get_text(strip=True)
        if title_similarity(product_title, title) < 0.6:
            continue
        price_text = price_elem.get_text(strip=True) if price_elem else ""
        price = clean_price(price_text)
        if price is not None and (best_price is None or price < best_price):
            best_price = price
    return best_price

# ── Main scan ─────────────────────────────────────────────────────────────────
def scan():
    cfg = load_config()
    if not cfg:
        return

    sources = cfg.get("sources", [])
    target  = cfg.get("target", {})
    fee_pct = float(cfg.get("fee_percent", 15.0)) / 100.0
    fixed_fee = float(cfg.get("fixed_fee", 3.0))   # e.g. shipping
    min_profit = float(cfg.get("min_profit", 5.0))

    state = load_state()

    for src in sources:
        clearance = scrape_source(src)
        if not clearance:
            _post(f"No products scraped from {src.get('name', src['url'])}", "warning")
            continue

        for prod in clearance:
            # Dedup: skip if we already processed this exact product (by URL)
            pid = hashlib.md5(prod["url"].encode()).hexdigest()
            if pid in state.get("seen", {}):
                continue

            # Look up resale price
            resale_price = lookup_target(prod["title"], target)
            if resale_price is None:
                # Failure to find target price is common – don't spam
                _post(f"{prod['title']}: no match found on target", "info")
                continue

            # Profit calculation
            revenue = resale_price
            cost = prod["price"] + (revenue * fee_pct) + fixed_fee
            profit = revenue - cost

            payload = {
                "product": prod["title"],
                "source_price": prod["price"],
                "target_price": resale_price,
                "profit": round(profit, 2),
                "source_url": prod["url"],
                "search_url": target["search_url_pattern"].format(query=urllib.parse.quote(prod["title"]))
            }

            if profit >= min_profit:
                level = "warning" if profit >= 10 else "info"
                _post(f"💰 {prod['title']}: Buy ${prod['price']:.2f} → Sell ${resale_price:.2f} (net +${profit:.2f})",
                      level, payload)
            else:
                _post(f"{prod['title']}: too thin (profit ${profit:.2f})", "info", payload)

            # Mark seen
            state.setdefault("seen", {})[pid] = datetime.utcnow().isoformat()
        _heartbeat()
        time.sleep(5)  # be kind between source scrapes

    save_state(state)

def main():
    _wait_for_hub()
    _post("Online Arbitrage Bot started — scanning for clearance flips.", "info")
    while True:
        try:
            scan()
        except Exception as e:
            _post(f"Scan error: {e}", "error")
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `online_arbitrage_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "target": {
    "name": "Amazon",
    "search_url_pattern": "https://www.amazon.com/s?k={query}",
    "item_selector": "[data-component-type='s-search-result']",
    "title_selector": "h2 a span",
    "price_selector": ".a-price .a-offscreen",
    "use_playwright": false
  },
  "sources": [
    {
      "name": "Walmart Clearance",
      "url": "https://www.walmart.com/browse/clearance/0/0",
      "product_selector": "[data-testid='list-view'] div[aria-label]",
      "title_selector": "span.w_iUH7",
      "price_selector": "[data-automation-id='product-price']",
      "use_playwright": false
    }
  ],
  "fee_percent": 15.0,
  "fixed_fee": 3.0,
  "min_profit": 5.0
}
"""
