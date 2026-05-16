#!/usr/bin/env python3
"""
retail_arbitrage_finder_bot.py — High‑Margin Retail Arbitrage Finder Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans clearance sections of major retailers, extracts product titles and
prices, then checks eBay sold listings to calculate potential profit
after all fees. Reports opportunities via the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install beautifulsoup4 requests

Configuration
─────────────
Place `arbitrage_finder_config.json` in the same directory:

{
  "clearance_sources": [
    {
      "name": "Walmart Clearance",
      "url": "https://www.walmart.com/browse/clearance/...",
      "use_playwright": false,
      "items_selector": "div[data-testid='item']",
      "title_selector": "span[data-automation-id='product-title']",
      "price_selector": "span[data-automation-id='product-price']",
      "link_selector": "a[data-automation-id='product-link']"
    }
  ],
  "ebay_app_id": "YOUR_EBAY_APP_ID",
  "fees": {
    "ebay_final_value_pct": 12.9,
    "payment_processing_pct": 2.9,
    "fixed_payment_fee": 0.30,
    "shipping_cost_estimate": 5.00
  },
  "target_profit_margin_pct": 25,
  "poll_interval_hours": 12,
  "state_file": "arbitrage_state.json"
}
"""

import json
import os
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "retail_arbitrage_finder_bot"
BOT_NAME = "Retail Arbitrage Finder"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "arbitrage_finder_config.json"
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
        return {"seen_products": []}   # list of product hashes

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

def product_hash(item: dict) -> str:
    """Unique identifier for a product from its title and source URL."""
    raw = f"{item.get('source_url', '')}||{item.get('title', '')}||{item.get('price', 0.0)}"
    return hashlib.sha256(raw.encode()).hexdigest()

# ── Scraper ──────────────────────────────────────────────────────
def scrape_source(source: dict) -> List[Dict]:
    """Extract products from a single clearance page."""
    url = source.get("url")
    items_selector = source.get("items_selector")
    title_selector = source.get("title_selector")
    price_selector = source.get("price_selector")
    link_selector = source.get("link_selector")

    if not all([url, items_selector, title_selector, price_selector]):
        _post(f"Source {source.get('name')} missing selectors", "warning")
        return []

    # Fetch page
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        _post(f"Failed to fetch {url}: {e}", "warning")
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    product_elements = soup.select(items_selector)
    if not product_elements:
        _post(f"No items found at {url} with selector '{items_selector}'", "info")
        return []

    products = []
    for elem in product_elements:
        title_elem = elem.select_one(title_selector)
        price_elem = elem.select_one(price_selector)
        if not title_elem or not price_elem:
            continue
        title = title_elem.get_text(strip=True)
        price_text = price_elem.get_text(strip=True)
        try:
            # Remove currency symbols and commas
            price = float(price_text.replace("$", "").replace(",", "").strip())
        except ValueError:
            continue
        link = ""
        if link_selector:
            link_elem = elem.select_one(link_selector)
            if link_elem and link_elem.get("href"):
                link = link_elem["href"]
                if link.startswith("/"):
                    link = url.rstrip("/") + "/" + link.lstrip("/")

        products.append({
            "title": title,
            "price": price,
            "source_url": url,
            "product_link": link,
            "source_name": source.get("name")
        })
    return products

# ── eBay sold price lookup (Finding API) ─────────────────────────
def get_ebay_sold_price(title: str, app_id: str) -> Optional[float]:
    """Use eBay Finding API to find completed sold listings and return median price."""
    base_url = "https://svcs.ebay.com/services/search/FindingService/v1"
    params = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.0.0",
        "SECURITY-APPNAME": app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "",
        "keywords": title[:350],          # eBay limits keywords
        "itemFilter(0).name": "SoldItemsOnly",
        "itemFilter(0).value": "true",
        "paginationInput.entriesPerPage": "5",
        "sortOrder": "EndTimeSoonest"
    }
    try:
        resp = requests.get(base_url, params=params, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = data.get("findCompletedItemsResponse", [{}])[0].get("searchResult", {}).get("item", [])
        if not items:
            return None
        prices = []
        for item in items:
            selling_status = item.get("sellingStatus", {})
            current_price = selling_status.get("currentPrice", {}).get("__value__", 0)
            try:
                prices.append(float(current_price))
            except ValueError:
                pass
        if not prices:
            return None
        prices.sort()
        # Use median to ignore outliers
        n = len(prices)
        if n % 2 == 0:
            median = (prices[n//2 - 1] + prices[n//2]) / 2
        else:
            median = prices[n//2]
        return median
    except Exception as e:
        _post(f"eBay API error for '{title[:50]}...': {e}", "warning")
        return None

# ── Profit calculation ──────────────────────────────────────────
def calculate_profit(cost_price: float, resale_price: float, fees: dict) -> Dict:
    """Return profit breakdown."""
    ebay_pct = fees.get("ebay_final_value_pct", 12.9) / 100.0
    paypal_pct = fees.get("payment_processing_pct", 2.9) / 100.0
    fixed_fee = fees.get("fixed_payment_fee", 0.30)
    shipping = fees.get("shipping_cost_estimate", 5.00)

    # Fees on resale price
    ebay_fee = resale_price * ebay_pct
    paypal_fee = (resale_price * paypal_pct) + fixed_fee

    total_fees = ebay_fee + paypal_fee + shipping
    net_profit = resale_price - cost_price - total_fees
    margin_pct = (net_profit / resale_price) * 100.0 if resale_price > 0 else 0.0
    return {
        "cost_price": round(cost_price, 2),
        "resale_price": round(resale_price, 2),
        "ebay_fee": round(ebay_fee, 2),
        "payment_fee": round(paypal_fee, 2),
        "shipping": shipping,
        "total_fees": round(total_fees, 2),
        "net_profit": round(net_profit, 2),
        "profit_margin_pct": round(margin_pct, 2)
    }

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Retail Arbitrage Finder Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        app_id = config.get("ebay_app_id")
        if not app_id:
            _post("eBay App ID missing", "error")
            time.sleep(3600)
            continue

        clearance_sources = config.get("clearance_sources", [])
        fees = config.get("fees", {})
        target_margin = float(config.get("target_profit_margin_pct", 25))
        interval_hours = float(config.get("poll_interval_hours", 12))
        state_file = config.get("state_file", "arbitrage_state.json")
        state = load_state(state_file)
        seen = set(state.get("seen_products", []))

        new_seen = set(seen)

        for source in clearance_sources:
            name = source.get("name", "Unnamed")
            products = scrape_source(source)
            for prod in products:
                pid = product_hash(prod)
                if pid in seen:
                    continue
                new_seen.add(pid)

                # Delay to respect eBay rate limits (5 req/sec for free tier)
                time.sleep(1.2)

                # Fetch eBay resale price
                resale_price = get_ebay_sold_price(prod["title"], app_id)
                if not resale_price:
                    continue

                profit_info = calculate_profit(prod["price"], resale_price, fees)
                if profit_info["profit_margin_pct"] >= target_margin:
                    summary = (f"{name}: {prod['title'][:80]} "
                               f"Cost ${prod['price']:.2f} -> eBay ${resale_price:.2f} "
                               f"Profit ${profit_info['net_profit']:.2f} ({profit_info['profit_margin_pct']}%)")
                    payload = {**prod, **profit_info}
                    _post(summary, "warning", payload)  # warning = high alert
                else:
                    _post(f"{name}: {prod['title'][:80]} low margin ({profit_info['profit_margin_pct']}%)", "info")

        state["seen_products"] = list(new_seen)
        save_state(state_file, state)
        _heartbeat()
        time.sleep(interval_hours * 3600)

if __name__ == "__main__":
    main()
