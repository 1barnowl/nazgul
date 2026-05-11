#!/usr/bin/env python3
"""
pokemon_card_bot.py — Pokémon Card Auto‑Buy & Resale Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors retailer product pages for stock.
2. Auto‑checks out when restock detected (Playwright).
3. Rotates proxies.
4. Solves CAPTCHAs via 2Captcha.
5. Lists purchased items on eBay at a markup.

SETUP
─────
1. Install dependencies:
      pip install requests playwright beautifulsoup4 ebaysdk
      python -m playwright install chromium

2. Get a 2Captcha API key (https://2captcha.com/) for CAPTCHA solving.
   Export:  CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing, get eBay API credentials and export:
      EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `pokemon_config.json` (example at bottom). Fill in:
   - Retailer product URLs and CSS selectors.
   - Payment / shipping details for checkout.
   - Proxy list (optional).

5. Attach to BotController.
"""

import json
import os
import re
import time
import random
import threading
import requests
from datetime import datetime, timedelta
from urllib.parse import urljoin
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from ebaysdk.trading import Connection as eBayTrading
    HAS_EBAY = True
except ImportError:
    HAS_EBAY = False

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "pokemon_card_bot"
BOT_NAME = "Pokémon Card Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "pokemon_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "pokemon_state.json")

SCAN_INTERVAL      = 30    # seconds between stock checks (per product)
HEARTBEAT_INTERVAL = 20
_last_hb = 0.0
_last_hb_lock = threading.Lock()

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
            "retailers": [
                {
                    "name": "Pokemon Center",
                    "product_url": "https://www.pokemoncenter.com/product/699-15360/",
                    "stock_selector": "button.add-to-cart",      # element that exists only when in stock
                    "sold_out_selector": "span.sold-out-text",   # or check for "Out of Stock" text
                    "add_to_cart_selector": "button.add-to-cart",
                    "checkout": {
                        "checkout_url": "https://www.pokemoncenter.com/checkout",
                        "email_selector": "input#email",
                        "first_name_selector": "input#firstName",
                        "last_name_selector": "input#lastName",
                        "address_selector": "input#address1",
                        "city_selector": "input#city",
                        "state_selector": "select#state",
                        "zip_selector": "input#postalCode",
                        "card_number_selector": "input#cardNumber",
                        "card_expiry_selector": "input#cardExpiry",
                        "card_cvv_selector": "input#cardCvv",
                        "place_order_selector": "button#placeOrder",
                        "user_data": {
                            "email": "your@email.com",
                            "first_name": "Ash",
                            "last_name": "Ketchum",
                            "address": "123 Pallet St",
                            "city": "Viridian",
                            "state": "CA",
                            "zip": "90001",
                            "card_number": "4111111111111111",
                            "card_expiry": "12/26",
                            "card_cvv": "123"
                        }
                    }
                }
            ],
            "proxies": {
                "list": []  # ["http://user:pass@ip:port", "http://ip:port", ...]
            },
            "captcha": {
                "api_key": os.getenv("CAPTCHA_API_KEY", ""),
                "service": "2captcha"
            },
            "ebay": {
                "enabled": True,
                "markup_multiplier": 2.0,
                "auto_list": True,
                "listing_duration_days": 30
            }
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

CFG = load_config()

# ── State ──────────────────────────────────────────────────────────────────────
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"purchased": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Proxy rotation ──────────────────────────────────────────────────────────────
_proxy_list = CFG.get("proxies", {}).get("list", [])
_proxy_index = 0
_proxy_lock = threading.Lock()

def next_proxy():
    global _proxy_index
    if not _proxy_list:
        return None
    with _proxy_lock:
        proxy = _proxy_list[_proxy_index % len(_proxy_list)]
        _proxy_index += 1
        return proxy

# ── CAPTCHA solver (2Captcha) ──────────────────────────────────────────────────
CAPTCHA_API_KEY = CFG.get("captcha", {}).get("api_key", "").strip()

def solve_captcha(page, sitekey=None, url=None):
    """Solves a reCAPTCHA v2/v3 on a page using 2Captcha."""
    if not CAPTCHA_API_KEY:
        _post("No 2Captcha API key. Cannot solve CAPTCHA.", "error")
        return False
    try:
        # Get sitekey from page if not provided
        if not sitekey:
            sitekey_elem = page.locator("[data-sitekey]")
            sitekey = sitekey_elem.get_attribute("data-sitekey") if sitekey_elem.count() else None
        if not sitekey:
            _post("Could not find reCAPTCHA sitekey.", "error")
            return False

        url = page.url
        # Submit to 2Captcha
        resp = requests.get("http://2captcha.com/in.php", params={
            "key": CAPTCHA_API_KEY,
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": url,
            "json": 1
        }, timeout=15)
        data = resp.json()
        if data.get("status") != 1:
            _post(f"2Captcha submission failed: {data.get('request')}", "error")
            return False

        captcha_id = data["request"]
        _post(f"Captcha submitted. ID: {captcha_id}. Waiting for solve...", "info")
        # Poll for result
        for _ in range(24):  # up to 2 minutes
            time.sleep(5)
            result_resp = requests.get("http://2captcha.com/res.php", params={
                "key": CAPTCHA_API_KEY,
                "action": "get",
                "id": captcha_id,
                "json": 1
            })
            result = result_resp.json()
            if result.get("status") == 1:
                solution = result["request"]
                # Inject solution into page
                page.evaluate(f"""document.getElementById('g-recaptcha-response').innerHTML = '{solution}';""")
                # Also call callback if exists
                page.evaluate("""if (typeof ___grecaptcha_cfg !== 'undefined') { Object.keys(___grecaptcha_cfg.clients).forEach(function(id) { ___grecaptcha_cfg.clients[id].W.O.callback(solution); }); }""")
                _post("Captcha solved!", "info")
                return True
            if result.get("request") != "CAPCHA_NOT_READY":
                _post(f"Captcha error: {result.get('request')}", "error")
                return False
        _post("Captcha solving timeout.", "error")
        return False
    except Exception as e:
        _post(f"Captcha solving error: {e}", "error")
        return False

# ── Browser automation for auto‑checkout ──────────────────────────────────────
def auto_checkout(retailer_cfg):
    """
    Launches headful or headless browser, adds product to cart, fills details,
    handles captcha, and places order. Returns True if successful.
    """
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed. Cannot do browser checkout.", "error")
        return False

    product_url = retailer_cfg["product_url"]
    add_to_cart = retailer_cfg.get("add_to_cart_selector", "button.add-to-cart")
    checkout_cfg = retailer_cfg.get("checkout", {})
    user = checkout_cfg.get("user_data", {})

    proxy = next_proxy()
    launch_args = {"headless": False}  # For checkout, headless might be blocked; use headed and keepalive
    if proxy:
        launch_args["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Go to product page
            _post(f"Navigating to {product_url}", "info")
            page.goto(product_url, wait_until="networkidle", timeout=30000)

            # 2. Click "Add to Cart"
            page.wait_for_selector(add_to_cart, timeout=10000)
            page.click(add_to_cart)
            _post("Added to cart.", "info")

            # 3. Proceed to checkout
            if checkout_cfg.get("checkout_url"):
                page.goto(checkout_cfg["checkout_url"], wait_until="networkidle", timeout=30000)
            else:
                # Try clicking a "Checkout" button
                possible_btns = ["text=Checkout", "text=Proceed to Checkout", "a:has-text('Checkout')"]
                clicked = False
                for selector in possible_btns:
                    if page.locator(selector).is_visible():
                        page.click(selector)
                        clicked = True
                        break
                if not clicked:
                    _post("Could not find checkout button.", "error")
                    return False
            page.wait_for_load_state("networkidle")

            # 4. Fill checkout form
            field_map = {
                "email": checkout_cfg.get("email_selector"),
                "first_name": checkout_cfg.get("first_name_selector"),
                "last_name": checkout_cfg.get("last_name_selector"),
                "address": checkout_cfg.get("address_selector"),
                "city": checkout_cfg.get("city_selector"),
                "state": checkout_cfg.get("state_selector"),
                "zip": checkout_cfg.get("zip_selector"),
                "card_number": checkout_cfg.get("card_number_selector"),
                "card_expiry": checkout_cfg.get("card_expiry_selector"),
                "card_cvv": checkout_cfg.get("card_cvv_selector"),
            }
            for key, selector in field_map.items():
                if selector:
                    value = user.get(key, "")
                    if value:
                        if key == "state" and selector.startswith("select"):
                            page.select_option(selector, value)
                        else:
                            page.fill(selector, value)

            # 5. Handle CAPTCHA
            if page.locator("iframe[title*='captcha']").is_visible() or page.locator("[data-sitekey]").is_visible():
                _post("CAPTCHA detected. Attempting solve...", "info")
                if not solve_captcha(page):
                    _post("CAPTCHA solve failed. Aborting checkout.", "error")
                    return False
                time.sleep(2)

            # 6. Place order
            place_btn = checkout_cfg.get("place_order_selector", "button#placeOrder")
            page.wait_for_selector(place_btn, timeout=10000)
            page.click(place_btn)
            page.wait_for_timeout(5000)

            # 7. Check for order confirmation
            if "thank you" in page.content().lower() or "order confirmed" in page.content().lower():
                _post(f"Order placed successfully for {product_url}", "info")
                return True
            else:
                _post(f"Order may not have succeeded – check manually.", "warning")
                return False
        except Exception as e:
            _post(f"Checkout automation error: {e}", "error")
            return False
        finally:
            browser.close()

# ── eBay listing ───────────────────────────────────────────────────────────────
def list_on_ebay(product_name, purchase_price):
    if not HAS_EBAY:
        return None
    trading = eBayTrading(
        domain="api.ebay.com",
        appid=os.getenv("EBAY_APP_ID"),
        certid=os.getenv("EBAY_CERT_ID"),
        devid=os.getenv("EBAY_DEV_ID"),
        token=os.getenv("EBAY_AUTH_TOKEN"),
        config_file=None
    )
    if not trading:
        return None

    markup = CFG["ebay"]["markup_multiplier"]
    ebay_price = round(purchase_price * markup, 2)
    title = f"{product_name} Pokémon Card – New & Sealed"
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": f"Brand new Pokémon card: {product_name}.",
            "PrimaryCategory": {"CategoryID": "183454"},  # CCG Individual Cards
            "StartPrice": ebay_price,
            "Quantity": 1,
            "ListingDuration": f"Days_{CFG['ebay']['listing_duration_days']}",
            "Country": "US",
            "Currency": "USD",
            "ListingType": "FixedPriceItem",
            "Site": "US",
            "ConditionID": "1000",  # New
        }
    }
    try:
        response = trading.execute("AddFixedPriceItem", payload)
        if response.dict().get("Ack") == "Success":
            item_id = response.dict()["ItemID"]
            _post(f"Listed '{title}' on eBay as item {item_id} at ${ebay_price}", "info")
            return item_id
    except Exception as e:
        _post(f"eBay listing error: {e}", "error")
    return None

# ── Monitor and act ────────────────────────────────────────────────────────────
def monitor_products():
    state = load_state()
    for retailer in CFG.get("retailers", []):
        name = retailer.get("name", "Unknown")
        url = retailer["product_url"]
        stock_sel = retailer["stock_selector"]
        sold_out_sel = retailer.get("sold_out_selector", "")

        # Quick HTTP check first (faster)
        try:
            proxy = next_proxy()
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, proxies=proxies)
            if resp.status_code != 200:
                _post(f"{name} HTTP {resp.status_code}", "warning")
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            in_stock = False
            if stock_sel:
                in_stock = bool(soup.select_one(stock_sel))
            # Also ensure not sold out
            if sold_out_sel and soup.select_one(sold_out_sel):
                in_stock = False

            if in_stock:
                _post(f"{name} – IN STOCK! Attempting checkout...", "warning")
                # To avoid duplicate purchases: check if we already bought this URL recently
                recent = [p for p in state.get("purchased", []) if p["url"] == url]
                if recent:
                    last = datetime.fromisoformat(recent[-1]["timestamp"])
                    if (datetime.utcnow() - last) < timedelta(hours=1):
                        _post(f"{name} already purchased recently. Skipping.", "info")
                        continue

                success = auto_checkout(retailer)
                if success:
                    # Record purchase
                    purchase_data = {
                        "url": url,
                        "name": name,
                        "price": "unknown",  # you could extract from page
                        "timestamp": datetime.utcnow().isoformat()
                    }
                    state.setdefault("purchased", []).append(purchase_data)
                    save_state(state)

                    # Auto-list on eBay
                    if CFG["ebay"].get("enabled") and CFG["ebay"].get("auto_list"):
                        list_on_ebay(name, 20.0)  # placeholder price; ideally scrape actual price
            else:
                _post(f"{name} – out of stock.", "info")
        except Exception as e:
            _post(f"Monitor error for {name}: {e}", "warning")

def main():
    _wait_for_hub()
    _post("Pokémon Card Bot online. Monitoring retailers and ready to buy.", "info")
    while True:
        monitor_products()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `pokemon_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "retailers": [
    {
      "name": "Pokemon Center - Elite Trainer Box",
      "product_url": "https://www.pokemoncenter.com/product/699-15360/",
      "stock_selector": "button.add-to-cart",
      "sold_out_selector": "span.sold-out-text",
      "add_to_cart_selector": "button.add-to-cart",
      "checkout": {
        "checkout_url": "https://www.pokemoncenter.com/checkout",
        "email_selector": "input#email",
        "first_name_selector": "input#firstName",
        "last_name_selector": "input#lastName",
        "address_selector": "input#address1",
        "city_selector": "input#city",
        "state_selector": "select#state",
        "zip_selector": "input#postalCode",
        "card_number_selector": "input#cardNumber",
        "card_expiry_selector": "input#cardExpiry",
        "card_cvv_selector": "input#cardCvv",
        "place_order_selector": "button#placeOrder",
        "user_data": {
          "email": "you@domain.com",
          "first_name": "Ash",
          "last_name": "Ketchum",
          "address": "123 Pallet St",
          "city": "Viridian",
          "state": "CA",
          "zip": "90001",
          "card_number": "4111111111111111",
          "card_expiry": "12/26",
          "card_cvv": "123"
        }
      }
    }
  ],
  "proxies": {
    "list": [
      "http://user:pass@proxy1:8080",
      "http://user:pass@proxy2:8080"
    ]
  },
  "captcha": {
    "api_key": "YOUR_2CAPTCHA_API_KEY"
  },
  "ebay": {
    "enabled": true,
    "markup_multiplier": 2.5,
    "auto_list": true,
    "listing_duration_days": 30
  }
}
"""
