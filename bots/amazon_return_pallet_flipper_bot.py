#!/usr/bin/env python3
"""
amazon_return_pallet_flipper_bot.py — Pallet Flipper Arbitrage Engine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scrapes liquidation auctions, matches items to eBay resale prices,
and optionally lists items for sale / places bids.

SETUP
─────
1. Install dependencies:
      pip install requests beautifulsoup4 ebaysdk

2. Create a config file named `pallet_flipper_config.json`.
   Example at the bottom of this script.

3. For eBay auto‑listing (optional):
      export EBAY_APP_ID="your_app_id"
      export EBAY_CERT_ID="your_cert_id"
      export EBAY_DEV_ID="your_dev_id"
      export EBAY_AUTH_TOKEN="your_user_token"

4. For automated bidding on liquidation sites (optional):
      Set site‑specific credentials in the config file.
      (B‑Stock / Liquidation.com often use HTTP‑Basic or cookies.)

5. Attach to BotController.
"""

import os
import json
import re
import time
import threading
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

try:
    from ebaysdk.finding import Connection as eBayFinding
    from ebaysdk.trading import Connection as eBayTrading
    HAS_EBAY = True
except ImportError:
    HAS_EBAY = False

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "pallet_flipper_bot"
BOT_NAME = "Pallet Flipper Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "pallet_flipper_config.json")

HEARTBEAT_INTERVAL = 20
SCAN_INTERVAL      = 3600   # 1 hour between full scans
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

# ── Configuration ──────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        default_config = {
            "liquidation_sources": [
                {
                    "name": "B-Stock (Amazon Returns)",
                    "url": "https://bstock.com/amazon-returns",
                    "listing_selector": "div.auction-card",
                    "title_selector": "h3",
                    "manifest_selector": "div.manifest a",
                    "price_selector": "span.current-bid",
                    "auth": {"username": None, "password": None}  # for sites requiring login
                },
                {
                    "name": "Liquidation.com",
                    "url": "https://www.liquidation.com/",
                    "listing_selector": "div.listing",
                    "title_selector": "a.title",
                    "manifest_selector": "a.manifest",
                    "price_selector": "span.price",
                    "auth": None
                }
            ],
            "ebay": {
                "enabled": True,
                "min_profit_margin": 15.0,   # minimum % profit after fees to consider
                "shipping_cost_per_item": 5.0,
                "fvf_percent": 13.25,        # eBay Final Value Fee %
                "listing_duration_days": 30,
                "auto_list": False            # set True to actually create eBay listings
            },
            "bidding": {
                "auto_bid": False,
                "max_bid_percent_of_estimated_value": 40  # bid up to X% of eBay total
            }
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(default_config, f, indent=2)
        return default_config
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

CFG = load_config()

# ── eBay API clients (real) ────────────────────────────────────────────────────
def get_ebay_finding_api():
    if not HAS_EBAY:
        return None
    app_id = os.getenv("EBAY_APP_ID")
    if not app_id:
        return None
    return eBayFinding(appid=app_id, config_file=None)

def get_ebay_trading_api():
    token = os.getenv("EBAY_AUTH_TOKEN")
    if not token:
        return None
    return eBayTrading(domain="api.ebay.com", appid=os.getenv("EBAY_APP_ID"),
                       certid=os.getenv("EBAY_CERT_ID"), devid=os.getenv("EBAY_DEV_ID"),
                       token=token)

# ── Scraping module ─────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def fetch_page(url, auth=None):
    try:
        if auth and auth.get("username"):
            resp = requests.get(url, headers=HEADERS, auth=(auth["username"], auth["password"]), timeout=30)
        else:
            resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        _post(f"Failed to fetch {url}: {e}", "warning")
        return None

def scrape_pallets(source_cfg):
    """Return list of pallet dicts from a source."""
    html = fetch_page(source_cfg["url"], source_cfg.get("auth"))
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(source_cfg["listing_selector"])
    pallets = []
    for card in cards:
        title_elem   = card.select_one(source_cfg["title_selector"])
        price_elem   = card.select_one(source_cfg["price_selector"])
        manifest_elem = card.select_one(source_cfg["manifest_selector"])
        if not title_elem or not price_elem:
            continue
        title = title_elem.get_text(strip=True)
        price_text = price_elem.get_text(strip=True)
        price = _extract_price(price_text)
        manifest_url = None
        if manifest_elem and manifest_elem.get("href"):
            manifest_url = requests.compat.urljoin(source_cfg["url"], manifest_elem["href"])
        if price is None:
            continue
        pallets.append({
            "source":      source_cfg["name"],
            "title":       title,
            "current_bid": price,
            "manifest_url": manifest_url,
            "raw_card":    str(card)[:200]
        })
    return pallets

def fetch_manifest(manifest_url, auth=None):
    """Download and parse manifest CSV or HTML to extract item list."""
    if not manifest_url:
        return []
    html = fetch_page(manifest_url, auth)
    if not html:
        return []
    # Many liquidation sites provide a CSV; we'll try to parse as simple list
    # This is a basic parser — in practice you'll need to adapt to each site's format.
    soup = BeautifulSoup(html, "html.parser")
    items = []
    # Generic: look for tables or list items
    table = soup.find("table")
    if table:
        rows = table.find_all("tr")[1:]  # skip header
        for row in rows:
            cols = row.find_all("td")
            if len(cols) >= 2:
                item_name = cols[0].get_text(strip=True)
                qty = cols[1].get_text(strip=True) if len(cols) > 1 else "1"
                items.append({"name": item_name, "quantity": _parse_int(qty)})
    else:
        # Fallback: split plain text by newline and look for item patterns
        lines = soup.get_text().splitlines()
        for line in lines:
            if re.search(r'(?i)\b(laptop|phone|headphones|shoes|tablet|shirt|charger|cable)\b', line):
                items.append({"name": line.strip(), "quantity": 1})
    return items

def _extract_price(text):
    nums = re.findall(r'[\d,]+\.?\d{0,2}', text.replace(",", ""))
    if nums:
        return float(nums[0])
    return None

def _parse_int(text):
    try:
        return int(re.sub(r'[^0-9]', '', text))
    except:
        return 1

# ── eBay market research (real prices) ─────────────────────────────────────────
def get_ebay_completed_price(item_name):
    """
    Search eBay Completed Listings via Finding API to get average sold price.
    Falls back to a lightweight scrape if API unavailable (less reliable).
    """
    api = get_ebay_finding_api()
    if api:
        try:
            response = api.execute("findCompletedItems", {
                "keywords": item_name,
                "paginationInput": {"entriesPerPage": 3},
                "sortOrder": "BestMatch"
            })
            items = response.dict().get("searchResult", {}).get("item", [])
            prices = []
            for itm in items:
                selling_status = itm.get("sellingStatus", {})
                converted_price = selling_status.get("convertedCurrentPrice", {})
                price = float(converted_price.get("value", 0))
                if price > 0:
                    prices.append(price)
            if prices:
                avg = sum(prices) / len(prices)
                return avg, "API"
        except Exception as e:
            _post(f"eBay API error: {e}", "warning")

    # Fallback: a simple request to eBay's sold search (may get blocked without explicit consent)
    try:
        url = f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(item_name)}&LH_Sold=1&LH_Complete=1"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            prices = []
            for price_elem in soup.select(".s-item__price"):
                price = _extract_price(price_elem.get_text())
                if price:
                    prices.append(price)
            if prices:
                return sum(prices) / len(prices), "scrape"
    except Exception:
        pass
    return None, None

# ── Profit calculator ──────────────────────────────────────────────────────────
def calculate_pallet_profit(pallet, items, config):
    """Estimate total resale value, profit, and ROI."""
    total_est_value = 0.0
    priced_items = 0
    for item in items:
        name = item["name"]
        qty  = item["quantity"]
        price, method = get_ebay_completed_price(name)
        if price is not None:
            total_est_value += price * qty
            priced_items += 1
    if priced_items == 0:
        return None, 0, 0

    # Costs
    purchase_cost = pallet["current_bid"]
    num_items     = len(items)
    shipping_cost = config["ebay"]["shipping_cost_per_item"] * num_items
    total_cost    = purchase_cost + shipping_cost

    # eBay fees
    fvf_rate = config["ebay"]["fvf_percent"] / 100.0
    ebay_fees = total_est_value * fvf_rate
    net_revenue = total_est_value - ebay_fees

    profit = net_revenue - total_cost
    profit_pct = (profit / total_cost) * 100 if total_cost > 0 else 0
    return {
        "total_estimated_resale": total_est_value,
        "purchase_est": purchase_cost,
        "shipping": shipping_cost,
        "ebay_fees": ebay_fees,
        "net_profit": profit,
        "profit_pct": profit_pct,
        "items_priced": priced_items,
        "manifest_count": len(items)
    }, profit_pct, price

# ── Auto‑listing on eBay (real) ─────────────────────────────────────────────────
def list_item_on_ebay(item_name, price, qty=1):
    """Create a fixed‑price listing on eBay using Trading API."""
    api = get_ebay_trading_api()
    if not api:
        _post("eBay Trading API not configured. Skipping listing.", "warning")
        return False
    try:
        request = {
            "Item": {
                "Title": f"{item_name} - Like New",
                "Description": f"Liquidation item: {item_name}. Fully functional. See photos.",
                "PrimaryCategory": {"CategoryID": "9355"},  # generic
                "StartPrice": price,
                "Quantity": qty,
                "ListingDuration": "GTC",
                "Country": "US",
                "Currency": "USD",
                "ListingType": "FixedPriceItem",
                "Site": "US",
                "ConditionID": "3000",  # Used
            }
        }
        response = api.execute("AddFixedPriceItem", request)
        if response.dict().get("Ack") == "Success":
            item_id = response.dict()["ItemID"]
            _post(f"Listed {item_name} as eBay item {item_id} at ${price:.2f}", "info")
            return True
        else:
            _post(f"eBay listing failed: {response.dict()}", "error")
            return False
    except Exception as e:
        _post(f"eBay listing error: {e}", "error")
        return False

# ── Pallet bidding (stub) ──────────────────────────────────────────────────────
def place_bid(pallet_url, amount, auth):
    """Submit a bid on a liquidation auction. Highly site‑specific."""
    _post(f"Bidding ${amount:.2f} on {pallet_url} (auth required) — currently a manual step.", "info")
    # In practice, you'd send a POST request to the auction endpoint.
    # Because each platform has a unique API/nonce, we leave this as a placeholder
    # for you to implement with the specific site's form handling.
    return False

# ── Main scan loop ─────────────────────────────────────────────────────────────
def scan():
    cfg = load_config()
    opportunities = []

    for source in cfg.get("liquidation_sources", []):
        pallets = scrape_pallets(source)
        if not pallets:
            _post(f"No pallets found on {source['name']}.", "info")
            continue
        _post(f"Found {len(pallets)} pallets on {source['name']}.", "info")

        for pallet in pallets:
            # Fetch manifest
            items = fetch_manifest(pallet.get("manifest_url"), source.get("auth"))
            if not items:
                # If no manifest, skip (can't value)
                continue

            analysis, profit_pct, _ = calculate_pallet_profit(pallet, items, cfg)
            if not analysis:
                continue

            min_margin = cfg["ebay"]["min_profit_margin"]
            if profit_pct >= min_margin:
                opp = {
                    **pallet,
                    **analysis,
                    "items": items[:5]  # include first few for reference
                }
                opportunities.append(opp)

                level = "error" if profit_pct >= 30 else ("warning" if profit_pct >= 20 else "info")
                _post(
                    f"💰 PROFITABLE PALLET: {pallet['title'][:60]}\n"
                    f"  Bid: ${analysis['purchase_est']:.2f} | "
                    f"Est. resale: ${analysis['total_estimated_resale']:.2f}\n"
                    f"  Net profit: ${analysis['net_profit']:.2f} ({analysis['profit_pct']:.1f}%) | "
                    f"{analysis['items_priced']}/{analysis['manifest_count']} items priced",
                    level,
                    opp
                )

                # Auto-bid if enabled
                if cfg.get("bidding", {}).get("auto_bid"):
                    max_bid = analysis["total_estimated_resale"] * (cfg["bidding"]["max_bid_percent_of_estimated_value"] / 100)
                    if analysis["purchase_est"] < max_bid:
                        place_bid(pallet["manifest_url"], max_bid, source.get("auth"))

                # Auto-list items if eBay enabled
                if cfg["ebay"].get("auto_list"):
                    for item in items:
                        price, _ = get_ebay_completed_price(item["name"])
                        if price:
                            listing_price = round(price * 1.0, 2)  # list at avg sold price
                            list_item_on_ebay(item["name"], listing_price, item["quantity"])
                            time.sleep(0.5)  # respect API rate limits
            else:
                _post(f"Pallet '{pallet['title'][:40]}...' profit {profit_pct:.1f}% below threshold.", "info")

    _post(f"Scan complete. {len(opportunities)} profitable opportunities found.", "info")

def main():
    _wait_for_hub()
    _post("Pallet Flipper Bot online — scanning liquidation auctions and eBay market prices.", "info")

    while True:
        scan()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()

# ═══════════════════════════════════════════════════════════════════════════════
# Example `pallet_flipper_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "liquidation_sources": [
    {
      "name": "B-Stock Amazon Returns",
      "url": "https://bstock.com/amazon-returns",
      "listing_selector": "div.auction-card",
      "title_selector": "h3",
      "manifest_selector": "a.manifest-link",
      "price_selector": "span.current-bid",
      "auth": { "username": null, "password": null }
    },
    {
      "name": "DirectLiquidation",
      "url": "https://www.directliquidation.com/",
      "listing_selector": "div.product",
      "title_selector": "h2.title",
      "manifest_selector": "a.manifest-csv",
      "price_selector": "div.current-price",
      "auth": null
    }
  ],
  "ebay": {
    "enabled": true,
    "min_profit_margin": 15.0,
    "shipping_cost_per_item": 5.0,
    "fvf_percent": 13.25,
    "listing_duration_days": 30,
    "auto_list": false
  },
  "bidding": {
    "auto_bid": false,
    "max_bid_percent_of_estimated_value": 40
  }
}
"""
