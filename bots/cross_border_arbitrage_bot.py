#!/usr/bin/env python3
"""
cross_border_arbitrage_bot.py — Cross‑Border E‑commerce Arbitrage Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Scrapes region‑exclusive products from a configurable source.
2. Lists them on eBay US at a calculated markup.
3. Monitors eBay orders and automatically drop‑ships from the source.

SETUP
─────
1. Install dependencies:
      pip install requests beautifulsoup4 ebaysdk

2. Get eBay API credentials (eBay Developer Program) and export:
      export EBAY_APP_ID="your_app_id"
      export EBAY_CERT_ID="your_cert_id"
      export EBAY_DEV_ID="your_dev_id"
      export EBAY_AUTH_TOKEN="your_user_token"          (for Trading API)
      export EBAY_SHOP_SUBSCRIBERS="true"  (optional, for subscriber features)

3. Create a config file named `cross_border_config.json`.
   See the example at the bottom of this script.

4. Attach to BotController.
"""

import json
import os
import re
import time
import threading
import requests
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup

try:
    from ebaysdk.trading import Connection as eBayTrading
    from ebaysdk.finding import Connection as eBayFinding
    HAS_EBAY = True
except ImportError:
    HAS_EBAY = False

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "cross_border_arbitrage_bot"
BOT_NAME = "Cross‑Border Arb Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "cross_border_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "cross_border_state.json")

SCAN_INTERVAL      = 3600   # 1 hour between full product scans
ORDER_CHECK_INTERVAL = 300  # 5 minutes between order checks
HEARTBEAT_INTERVAL = 20
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
        default = {
            "source": {
                "name": "Japanese Candy Shop",
                "base_url": "https://www.example-japan-candy.com",
                "product_listing_url": "https://www.example-japan-candy.com/rare-kitkats",
                "listing_selector": "div.product-item",
                "title_selector": "h2.product-title",
                "price_selector": "span.price",
                "image_selector": "img.product-image",
                "product_url_selector": "a.product-link",
                "stock_selector": "span.stock-status",
                "add_to_cart_url": "/cart/add",   # POST target
                "shipping_international": True
            },
            "ebay": {
                "markup_multiplier": 3.0,
                "fixed_markup_usd": 0.0,
                "listing_duration_days": 30,
                "auto_list": True
            },
            "dropshipping": {
                "auto_fulfill": False,   # set to True to actually place source orders
                "buyer_message_template": "Thank you for your order! Your item will be shipped directly from Japan."
            }
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

CFG = load_config()

# ── State file (tracks which products are already listed) ──────────────────────
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"listed_items": {}}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── eBay API clients ───────────────────────────────────────────────────────────
def get_trading_api():
    if not HAS_EBAY:
        return None
    token = os.getenv("EBAY_AUTH_TOKEN")
    if not token:
        _post("eBay Trading API token missing. Set EBAY_AUTH_TOKEN env var.", "error")
        return None
    return eBayTrading(
        domain="api.ebay.com",
        appid=os.getenv("EBAY_APP_ID"),
        certid=os.getenv("EBAY_CERT_ID"),
        devid=os.getenv("EBAY_DEV_ID"),
        token=token,
        config_file=None
    )

def get_finding_api():
    if not HAS_EBAY:
        return None
    app_id = os.getenv("EBAY_APP_ID")
    if not app_id:
        return None
    return eBayFinding(appid=app_id, config_file=None)

# ── Source scraper ─────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def scrape_source_products():
    """Fetch all product listings from the configured source page."""
    source = CFG["source"]
    url = source["product_listing_url"]
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        _post(f"Source scrape failed: {e}", "error")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select(source["listing_selector"])
    products = []
    for item in items:
        title_elem   = item.select_one(source["title_selector"])
        price_elem   = item.select_one(source["price_selector"])
        img_elem     = item.select_one(source.get("image_selector"))
        link_elem    = item.select_one(source.get("product_url_selector"))
        stock_elem   = item.select_one(source.get("stock_selector")) if source.get("stock_selector") else None

        if not title_elem or not price_elem:
            continue

        title = title_elem.get_text(strip=True)
        price_text = price_elem.get_text(strip=True)
        price = _extract_price(price_text)
        image_url = img_elem.get("src") if img_elem else None
        product_url = link_elem.get("href") if link_elem else None
        if product_url and not product_url.startswith("http"):
            product_url = urljoin(source["base_url"], product_url)
        in_stock = True
        if stock_elem:
            stock_text = stock_elem.get_text(strip=True).lower()
            if "out of stock" in stock_text or "sold out" in stock_text:
                in_stock = False

        if price is None or not in_stock:
            continue

        products.append({
            "title":       title,
            "price_source": price,
            "currency":    "USD",  # source site's currency; you may need to adapt
            "image_url":   image_url,
            "product_url": product_url,
            "source_id":   product_url or title   # unique identifier
        })
    return products

def _extract_price(text):
    """Extract a float from price string like '$12.99', '¥1,200', etc."""
    if not text:
        return None
    # Remove currency symbols, keep digits, comma, dot
    cleaned = re.sub(r'[^\d,\.]', '', text)
    try:
        # If multiple decimal candidates, take the first
        return float(cleaned.replace(',', ''))
    except ValueError:
        return None

# ── eBay listing creation ─────────────────────────────────────────────────────
def list_on_ebay(product):
    """Create a fixed‑price listing on eBay US."""
    trading = get_trading_api()
    if not trading:
        _post("eBay Trading API not available. Cannot list.", "warning")
        return None

    markup = CFG["ebay"]["markup_multiplier"]
    fixed  = CFG["ebay"]["fixed_markup_usd"]
    ebay_price = round(product["price_source"] * markup + fixed, 2)

    # Build item details
    title = product["title"][:80]  # eBay title max 80 chars
    description = f"Rare Japanese import: {product['title']}. Direct from Tokyo."
    payload = {
        "Item": {
            "Title": title,
            "Description": description,
            "PrimaryCategory": {"CategoryID": "14339"},  # Collectibles > International, or adjust
            "StartPrice": ebay_price,
            "Quantity": 1,
            "ListingDuration": f"Days_{CFG['ebay']['listing_duration_days']}",
            "Country": "US",
            "Currency": "USD",
            "ListingType": "FixedPriceItem",
            "Site": "US",
            "ConditionID": "1000",  # New
            "PictureDetails": {"PictureURL": product.get("image_url")} if product.get("image_url") else {},
            "ShippingDetails": {
                "ShippingType": "Flat",
                "ShippingServiceOptions": {
                    "ShippingService": "USPSMedia",
                    "ShippingServiceCost": 0.0,
                    "FreeShipping": True
                }
            },
        }
    }
    try:
        response = trading.execute("AddFixedPriceItem", payload)
        if response.dict().get("Ack") == "Success":
            item_id = response.dict()["ItemID"]
            _post(f"Listed '{title}' on eBay as item {item_id} at ${ebay_price}", "info")
            return item_id
        else:
            _post(f"eBay listing failed: {response.dict()}", "error")
            return None
    except Exception as e:
        _post(f"eBay API exception: {e}", "error")
        return None

# ── Order monitoring & drop‑ship fulfillment ──────────────────────────────────
def check_new_orders(state):
    """Fetch recent orders from eBay and fulfill those not yet shipped."""
    trading = get_trading_api()
    if not trading:
        _post("Trading API down. Cannot check orders.", "warning")
        return

    # Get orders created in the last few days
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=3)  # check last 3 days
    request = {
        "OrderRole": "Seller",
        "OrderStatus": "Completed",
        "CreateTimeFrom": start_time.isoformat() + "Z",
        "CreateTimeTo": end_time.isoformat() + "Z",
        "Pagination": {"EntriesPerPage": 25},
    }

    try:
        response = trading.execute("GetOrders", request)
        if response.dict().get("Ack") != "Success":
            _post(f"GetOrders error: {response.dict()}", "error")
            return
        orders = response.dict().get("OrderArray", {}).get("Order", [])
        if not orders:
            return
        if isinstance(orders, dict):  # single order
            orders = [orders]
    except Exception as e:
        _post(f"eBay order fetch exception: {e}", "error")
        return

    for order in orders:
        order_id = order["OrderID"]
        # Check if already fulfilled
        if order_id in state.get("fulfilled_orders", {}):
            continue
        # Look for line items that match our listings
        transactions = order.get("TransactionArray", {}).get("Transaction", [])
        if isinstance(transactions, dict):
            transactions = [transactions]
        for tx in transactions:
            item_id = tx.get("Item", {}).get("ItemID")
            if item_id in state.get("listed_items", {}):
                # This is our item! Fulfill from source.
                item_data = state["listed_items"][item_id]
                buyer_address = order.get("ShippingAddress", {})
                buyer_name = buyer_address.get("Name", "Valued Customer")
                buyer_country = buyer_address.get("Country", "US")

                _post(f"New order {order_id} for item {item_id} – {item_data['title']}. Fulfilling...", "info")
                success = fulfill_order(item_data, buyer_address)
                if success:
                    state.setdefault("fulfilled_orders", {})[order_id] = {
                        "fulfilled_at": datetime.utcnow().isoformat(),
                        "item_id": item_id,
                        "source_cost": item_data["source_price"],
                        "ebay_price": item_data["ebay_price"]
                    }
                    save_state(state)
                    _post(f"Order {order_id} fulfilled successfully.", "info")
                else:
                    _post(f"Failed to fulfill order {order_id} – check source site.", "error")
                break  # assume one line item per order

def fulfill_order(item_data, buyer_address):
    """Place an order on the source website to ship to the buyer."""
    if not CFG["dropshipping"]["auto_fulfill"]:
        _post("Auto‑fulfill disabled in config. Manual action required.", "info")
        return False

    # Build a request to the source site's add‑to‑cart / purchase endpoint.
    # This is site‑specific and often requires a POST with form data or JSON.
    source = CFG["source"]
    add_to_cart_url = urljoin(source["base_url"], source["add_to_cart_url"])
    payload = {
        "product_id": item_data.get("source_id"),
        "quantity": 1,
        "shipping_name": buyer_address.get("Name", ""),
        "shipping_address": buyer_address.get("Street1", ""),
        "shipping_city": buyer_address.get("CityName", ""),
        "shipping_state": buyer_address.get("StateOrProvince", ""),
        "shipping_zip": buyer_address.get("PostalCode", ""),
        "shipping_country": buyer_address.get("Country", "US"),
    }
    try:
        resp = requests.post(add_to_cart_url, data=payload, headers=HEADERS, timeout=30)
        if resp.status_code in (200, 201, 302):
            # Ideally check confirmation message
            _post(f"Source order placed for {item_data['title']} to {buyer_address.get('Name')}.", "info")
            return True
        else:
            _post(f"Source order failed with status {resp.status_code}: {resp.text[:200]}", "error")
            return False
    except requests.RequestException as e:
        _post(f"Source order network error: {e}", "error")
        return False

# ── Main loops ────────────────────────────────────────────────────────────────
def product_scan_and_list():
    """Scan source and list new products on eBay."""
    state = load_state()
    products = scrape_source_products()
    if not products:
        _post("No products scraped from source.", "info")
        return

    for product in products:
        # Check if already listed (by source_id)
        source_id = product["source_id"]
        already_listed = any(
            info.get("source_id") == source_id
            for info in state.get("listed_items", {}).values()
        )
        if already_listed:
            continue

        # List on eBay (if auto‑list enabled)
        if CFG["ebay"]["auto_list"]:
            item_id = list_on_ebay(product)
            if item_id:
                state.setdefault("listed_items", {})[item_id] = {
                    "title":       product["title"],
                    "source_price": product["price_source"],
                    "ebay_price":  round(product["price_source"] * CFG["ebay"]["markup_multiplier"] + CFG["ebay"]["fixed_markup_usd"], 2),
                    "source_id":   source_id,
                    "product_url": product["product_url"],
                }
                save_state(state)
        else:
            # Just log that we would list it
            ebay_price = round(product["price_source"] * CFG["ebay"]["markup_multiplier"] + CFG["ebay"]["fixed_markup_usd"], 2)
            _post(f"Would list '{product['title']}' at ${ebay_price} (auto‑list disabled).", "info")

def order_check_loop():
    """Continuously check for new orders and fulfill them."""
    state = load_state()
    while True:
        check_new_orders(state)
        time.sleep(ORDER_CHECK_INTERVAL)

def main():
    _wait_for_hub()
    _post("Cross‑Border Arbitrage Bot online — scraping source & listing on eBay.", "info")

    # Start order checking thread
    threading.Thread(target=order_check_loop, daemon=True).start()

    while True:
        product_scan_and_list()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `cross_border_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "source": {
    "name": "Japanese Candy Store",
    "base_url": "https://www.example-japan-candy.com",
    "product_listing_url": "https://www.example-japan-candy.com/rare-kitkats",
    "listing_selector": "div.product-item",
    "title_selector": "h2.product-title",
    "price_selector": "span.price",
    "image_selector": "img.product-image",
    "product_url_selector": "a.product-link",
    "stock_selector": "span.stock-status",
    "add_to_cart_url": "/cart/add"
  },
  "ebay": {
    "markup_multiplier": 3.0,
    "fixed_markup_usd": 0.0,
    "listing_duration_days": 30,
    "auto_list": true
  },
  "dropshipping": {
    "auto_fulfill": false,
    "buyer_message_template": "Thank you for your order! Your item will be shipped directly from Japan."
  }
}
"""
