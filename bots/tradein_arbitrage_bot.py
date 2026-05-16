#!/usr/bin/env python3
"""
tradein_arbitrage_bot.py — Trade‑In / Buyback Arbitrage Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans listings on user‑to‑user marketplaces (e.g. Swappa)
for electronics priced below the current eBay market value.
Calculates profit after all fees and alerts via Nazgul.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install requests beautifulsoup4

Configuration
─────────────
Place `tradein_arbitrage_config.json` in the same directory:

{
  "sources": [
    {
      "name": "Swappa iPhone 14",
      "url": "https://swappa.com/buy/apple-iphone-14",
      "items_selector": "div.listing-card",
      "title_selector": ".listing-title",
      "price_selector": ".price",
      "link_selector": "a.listing-link",
      "next_page_selector": "a.next"
    }
  ],
  "ebay_app_id": "YOUR_EBAY_APP_ID",
  "fees": {
    "ebay_final_value_pct": 12.9,
    "payment_processing_pct": 2.9,
    "fixed_payment_fee": 0.30,
    "shipping_cost_estimate": 8.00
  },
  "target_profit_pct": 20,
  "poll_interval_minutes": 60,
  "state_file": "tradein_arbitrage_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "tradein_arbitrage_bot"
BOT_NAME = "Trade‑In / Buyback Arbitrage"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "tradein_arbitrage_config.json"
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

# ── State management ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"seen_listings": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

def listing_fingerprint(listing: dict) -> str:
    """Unique identifier from title + price + link."""
    raw = f"{listing['title']}|{listing['price']}|{listing['link']}"
    return hashlib.sha256(raw.encode()).hexdigest()

# ── eBay sold price lookup (Finding API) ─────────────────────────
def get_ebay_market_price(keywords: str, app_id: str) -> Optional[float]:
    """Fetch median sold price from eBay for given search terms."""
    url = "https://svcs.ebay.com/services/search/FindingService/v1"
    params = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.0.0",
        "SECURITY-APPNAME": app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "",
        "keywords": keywords[:350],
        "itemFilter(0).name": "SoldItemsOnly",
        "itemFilter(0).value": "true",
        "paginationInput.entriesPerPage": "10",
        "sortOrder": "EndTimeSoonest"
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = data.get("findCompletedItemsResponse", [{}])[0].get("searchResult", {}).get("item", [])
        if not items:
            return None
        prices = []
        for item in items:
            price = item.get("sellingStatus", {}).get("currentPrice", {}).get("__value__")
            if price:
                prices.append(float(price))
        if not prices:
            return None
        prices.sort()
        n = len(prices)
        if n % 2 == 0:
            median = (prices[n//2 - 1] + prices[n//2]) / 2
        else:
            median = prices[n//2]
        return round(median, 2)
    except Exception as e:
        _post(f"eBay API error for '{keywords[:50]}...': {e}", "warning")
        return None

# ── Profit calculator ────────────────────────────────────────────
def calculate_profit(buy_price: float, resale_price: float, fees: dict) -> Dict:
    ebay_pct = fees.get("ebay_final_value_pct", 12.9) / 100.0
    paypal_pct = fees.get("payment_processing_pct", 2.9) / 100.0
    fixed_fee = fees.get("fixed_payment_fee", 0.30)
    shipping = fees.get("shipping_cost_estimate", 5.00)

    ebay_fee = resale_price * ebay_pct
    paypal_fee = (resale_price * paypal_pct) + fixed_fee
    total_fees = ebay_fee + paypal_fee + shipping
    net_profit = resale_price - buy_price - total_fees
    margin_pct = (net_profit / resale_price) * 100.0 if resale_price > 0 else 0.0
    return {
        "buy_price": round(buy_price, 2),
        "resale_price": round(resale_price, 2),
        "ebay_fee": round(ebay_fee, 2),
        "payment_fee": round(paypal_fee, 2),
        "shipping": shipping,
        "total_fees": round(total_fees, 2),
        "net_profit": round(net_profit, 2),
        "profit_margin_pct": round(margin_pct, 2)
    }

# ── Scraping engine ──────────────────────────────────────────────
def scrape_listings(source: dict) -> List[Dict]:
    """Extract listings from a single source page (handles pagination)."""
    results = []
    url = source["url"]
    items_selector = source["items_selector"]
    title_selector = source["title_selector"]
    price_selector = source["price_selector"]
    link_selector = source["link_selector"]
    next_selector = source.get("next_page_selector")

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    while url:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            _post(f"Failed to fetch {url}: {e}", "warning")
            break

        soup = BeautifulSoup(resp.text, "lxml")
        items = soup.select(items_selector)
        if not items:
            break

        for item_el in items:
            title_el = item_el.select_one(title_selector)
            price_el = item_el.select_one(price_selector)
            if not title_el or not price_el:
                continue
            title = title_el.get_text(strip=True)
            price_text = price_el.get_text(strip=True)
            try:
                price = float(price_text.replace("$", "").replace(",", "").strip())
            except ValueError:
                continue
            link = ""
            if link_selector:
                link_el = item_el.select_one(link_selector)
                if link_el and link_el.get("href"):
                    link = link_el["href"]
                    if link.startswith("/"):
                        from urllib.parse import urljoin
                        link = urljoin(url, link)

            results.append({
                "title": title,
                "price": price,
                "link": link,
                "source_name": source["name"]
            })

        # Pagination
        if next_selector:
            next_el = soup.select_one(next_selector)
            if next_el and next_el.get("href"):
                url = next_el["href"]
                if url.startswith("/"):
                    url = urljoin(resp.url, url)
            else:
                url = None
        else:
            url = None

        time.sleep(1.2)  # polite crawling

    return results

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Trade‑In / Buyback Arbitrage Bot online")
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

        fees = config.get("fees", {})
        target_pct = float(config.get("target_profit_pct", 20))
        poll_minutes = int(config.get("poll_interval_minutes", 60))
        state_file = config.get("state_file", "tradein_arbitrage_state.json")
        state = load_state(state_file)
        seen = set(state.get("seen_listings", []))

        sources = config.get("sources", [])
        for source in sources:
            listings = scrape_listings(source)
            for listing in listings:
                fp = listing_fingerprint(listing)
                if fp in seen:
                    continue
                seen.add(fp)

                # eBay market price – use listing title as search keywords
                market_price = get_ebay_market_price(listing["title"], app_id)
                if not market_price:
                    _post(f"No eBay sold data for '{listing['title'][:60]}...'", "info")
                    continue

                profit_info = calculate_profit(listing["price"], market_price, fees)
                if profit_info["profit_margin_pct"] >= target_pct:
                    summary = (
                        f"{source['name']}: {listing['title'][:70]} "
                        f"${listing['price']:.2f} → eBay ${market_price:.2f} "
                        f"Profit ${profit_info['net_profit']:.2f} ({profit_info['profit_margin_pct']}%)"
                    )
                    payload = {**listing, **profit_info, "market_price": market_price}
                    _post(summary, "warning", payload)  # warning = opportunity alert
                else:
                    _post(
                        f"{source['name']}: {listing['title'][:60]} "
                        f"low margin ({profit_info['profit_margin_pct']}%)",
                        "info"
                    )

                time.sleep(1.0)  # respect eBay rate limits

        state["seen_listings"] = list(seen)[-500:]  # keep last 500
        save_state(state_file, state)
        _heartbeat()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
