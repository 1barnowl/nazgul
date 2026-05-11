#!/usr/bin/env python3
"""
raspberry_pi5_scalp_bot.py — Raspberry Pi 5 Auto‑Buy & Resell Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Scrapes Adafruit, SparkFun, PiShop.us for Pi5 boards.
2. Auto‑checks out when stock appears (Playwright).
3. Rotates proxies and solves CAPTCHAs via 2Captcha.
4. Lists purchased items on eBay at 2× (or your markup).

SETUP
─────
1. Install dependencies:
      pip install requests playwright beautifulsoup4 ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key:
      export CAPTCHA_API_KEY="your-key"

3. Set eBay Trading API credentials (optional for auto‑listing):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `pi5_config.json` (example at bottom).
   Provide:
   - Retailers with URLs, selectors, checkout details.
   - Your payment and shipping info.
   - Proxy list.

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
BOT_ID   = "raspberry_pi5_scalp_bot"
BOT_NAME = "Raspberry Pi 5 Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "pi5_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "pi5_state.json")

SCAN_INTERVAL      = 30    # seconds between stock checks
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

# ── Config ─────────────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {
            "retailers": [
                {
                    "site": "Adafruit",
                    "url": "https://www.adafruit.com/product/XXXX",
                    "stock_selector": ".product-sales-point:not(.sold-out)",
                    "sold_out_selector": ".sold-out",
                    "add_to_cart_selector": ".add-to-cart-btn",
                    "max_qty": 2,
                    "price_selector": ".product-price"
                },
                {
                    "site": "SparkFun",
                    "url": "https://www.sparkfun.com/products/XXXXX",
                    "stock_selector": ".product-availability--in-stock",
                    "sold_out_selector": ".product-availability--out-of-stock",
                    "add_to_cart_selector": ".btn-add-to-cart",
                    "max_qty": 2,
                    "price_selector": ".price"
                },
                {
                    "site": "PiShop.us",
                    "url": "https://www.pishop.us/product/raspberry-pi-5/",
                    "stock_selector": ".out-of-stock",  # invert
                    "sold_out_selector": ".out-of-stock",
                    "add_to_cart_selector": ".single_add_to_cart_button",
                    "max_qty": 1,
                    "price_selector": ".price"
                }
            ],
            "checkout_profile": {
                "email": "your@email.com",
                "first_name": "Eben",
                "last_name": "Upton",
                "address": "1 Pi Tower",
                "city": "Cambridge",
                "state": "MA",
                "zip": "02139",
                "card_number": "4111111111111111",
                "card_expiry": "12/28",
                "card_cvv": "123",
                "phone": "5551234567"
            },
            "proxies": {"list": []},
            "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
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

# ── Proxy rotation ─────────────────────────────────────────────────────────────
_proxy_list = CFG.get("proxies", {}).get("list", [])
_proxy_index = 0
_proxy_lock = threading.Lock()

def next_proxy():
    if not _proxy_list:
        return None
    global _proxy_index
    with _proxy_lock:
        proxy = _proxy_list[_proxy_index % len(_proxy_list)]
        _proxy_index += 1
        return proxy

# ── CAPTCHA solving ────────────────────────────────────────────────────────────
CAPTCHA_API_KEY = CFG.get("captcha", {}).get("api_key", "").strip()

def solve_captcha(page, sitekey=None):
    if not CAPTCHA_API_KEY:
        _post("No 2Captcha API key.", "error")
        return False
    try:
        if not sitekey:
            sitekey_elem = page.locator("[data-sitekey]")
            if sitekey_elem.count():
                sitekey = sitekey_elem.get_attribute("data-sitekey")
            else:
                _post("No sitekey found.", "error")
                return False
        url = page.url
        resp = requests.get("http://2captcha.com/in.php", params={
            "key": CAPTCHA_API_KEY, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": url, "json": 1
        }, timeout=15).json()
        if resp.get("status") != 1:
            _post(f"2Captcha submission failed: {resp.get('request')}", "error")
            return False
        captcha_id = resp["request"]
        _post(f"Solving CAPTCHA {captcha_id}...", "info")
        for _ in range(30):
            time.sleep(5)
            result = requests.get("http://2captcha.com/res.php", params={
                "key": CAPTCHA_API_KEY, "action": "get",
                "id": captcha_id, "json": 1
            }).json()
            if result.get("status") == 1:
                token = result["request"]
                page.evaluate(f"""
                    document.getElementById('g-recaptcha-response').innerHTML = '{token}';
                    if (typeof ___grecaptcha_cfg !== 'undefined') {{
                        Object.keys(___grecaptcha_cfg.clients).forEach(id =>
                            ___grecaptcha_cfg.clients[id].W.O.callback(token));
                    }}
                """)
                _post("CAPTCHA solved!", "info")
                return True
            if result.get("request") != "CAPCHA_NOT_READY":
                break
        _post("CAPTCHA solving timed out.", "error")
        return False
    except Exception as e:
        _post(f"CAPTCHA error: {e}", "error")
        return False

# ── Generic checkout (Playwright) ──────────────────────────────────────────────
def perform_checkout(retailer_cfg):
    """
    Launches Playwright browser, adds Pi5 to cart, fills checkout using
    global profile, handles CAPTCHA, and places order.
    Returns True if order succeeded.
    """
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed.", "error")
        return False

    profile = CFG.get("checkout_profile", {})
    proxy = next_proxy()
    launch_options = {"headless": False}
    if proxy:
        launch_options["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_options)
        page = browser.new_page()
        try:
            url = retailer_cfg["url"]
            _post(f"Visiting {url}", "info")
            page.goto(url, wait_until="networkidle", timeout=30000)

            # Check stock again (safety)
            if page.locator(retailer_cfg["sold_out_selector"]).count():
                _post("Product now sold out.", "warning")
                return False

            # Click Add to Cart
            add_btn = retailer_cfg["add_to_cart_selector"]
            page.click(add_btn)
            _post("Added to cart.", "info")

            # Go to checkout (most stores redirect automatically)
            # Try navigating to /cart or /checkout
            page.goto(url.replace("/product/", "/cart"), wait_until="networkidle", timeout=30000)
            # Look for a "Checkout" link/button
            if page.locator("a:has-text('Checkout')").count():
                page.click("a:has-text('Checkout')")
            elif page.locator("button:has-text('Checkout')").count():
                page.click("button:has-text('Checkout')")

            # Fill generic checkout form
            _fill_generic_checkout(page, profile)

            # CAPTCHA?
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                if not solve_captcha(page):
                    return False
                page.wait_for_timeout(2000)

            # Place order
            order_btn = "button:has-text('Place Order'), button[type='submit'], #place_order"
            page.click(order_btn)
            page.wait_for_timeout(8000)

            if any(txt in page.content().lower() for txt in ["thank you", "order confirmed", "order #"]):
                _post("Order placed successfully!", "info")
                return True
            else:
                _post("Order may not have gone through.", "warning")
                return False
        except Exception as e:
            _post(f"Checkout error: {e}", "error")
            return False
        finally:
            browser.close()

def _fill_generic_checkout(page, profile):
    """Attempt to fill common checkout fields."""
    fields = {
        "email": "#email, input[type='email']",
        "first_name": "#first-name, #firstName, input[name='firstname']",
        "last_name": "#last-name, #lastName, input[name='lastname']",
        "address": "#address1, #address-line1, input[name='address1']",
        "city": "#city, input[name='city']",
        "state": "#state, select[name='state']",
        "zip": "#postal-code, #zip, input[name='zip']",
        "card_number": "#card-number, #cc-number, input[name='cardnumber']",
        "card_expiry": "#expiry, #exp-date, input[name='exp-date']",
        "card_cvv": "#cvv, #card-cvc, input[name='cvv']",
        "phone": "#phone, input[name='phone']"
    }
    for key, selector_list in fields.items():
        value = profile.get(key, "")
        if not value:
            continue
        for sel in selector_list.split(", "):
            try:
                if sel.startswith("select"):
                    page.select_option(sel, value)
                    break
                else:
                    page.fill(sel, value)
                    break
            except:
                continue

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
    title = f"Raspberry Pi 5 – {product_name} – Brand New In Hand"
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": "Brand new Raspberry Pi 5 board. Ready to ship.",
            "PrimaryCategory": {"CategoryID": "181698"},  # Computers/Tablets & Networking > Computer Components & Parts > Single Board Computers
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
            _post(f"eBay listing created: {item_id} at ${ebay_price}", "info")
            return item_id
    except Exception as e:
        _post(f"eBay listing error: {e}", "error")
    return None

# ── Main scanning loop ────────────────────────────────────────────────────────
def scan_retailers():
    state = load_state()
    for retailer in CFG["retailers"]:
        site = retailer["site"]
        url = retailer["url"]
        _post(f"Checking {site}...", "info")

        # Quick HTTP check
        proxy = next_proxy()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, proxies=proxies)
            soup = BeautifulSoup(resp.text, "html.parser")
            in_stock = True
            sold_out_sel = retailer.get("sold_out_selector")
            if sold_out_sel:
                if soup.select_one(sold_out_sel):
                    in_stock = False
            # Additionally check for positive stock selector
            stock_sel = retailer.get("stock_selector")
            if stock_sel and in_stock:
                if not soup.select_one(stock_sel):
                    in_stock = False
        except Exception as e:
            _post(f"{site} pre-check error: {e}", "warning")
            continue

        if not in_stock:
            _post(f"{site} – out of stock.", "info")
            continue

        # Avoid duplicate purchase within 24h
        already = any(
            p["url"] == url and
            (datetime.utcnow() - datetime.fromisoformat(p["timestamp"])) < timedelta(hours=24)
            for p in state.get("purchased", [])
        )
        if already:
            _post(f"{site} – already purchased today.", "info")
            continue

        _post(f"{site} IN STOCK! Attempting purchase...", "error")
        price = None
        try:
            price_elem = soup.select_one(retailer.get("price_selector"))
            if price_elem:
                price_text = price_elem.get_text(strip=True)
                price = float(re.sub(r'[^\d.]', '', price_text))
        except:
            price = 60.0  # fallback

        success = perform_checkout(retailer)
        if success:
            state.setdefault("purchased", []).append({
                "url": url,
                "site": site,
                "timestamp": datetime.utcnow().isoformat(),
                "price_paid": price
            })
            save_state(state)

            # Auto-list on eBay
            if CFG["ebay"].get("auto_list"):
                list_on_ebay(f"Raspberry Pi 5 from {site}", price)

        # Delay between retailers to avoid rate limiting
        time.sleep(random.uniform(10, 20))

def main():
    _wait_for_hub()
    _post("Raspberry Pi 5 Scalp Bot online. Monitoring retailers.", "info")

    while True:
        scan_retailers()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `pi5_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "retailers": [
    {
      "site": "Adafruit",
      "url": "https://www.adafruit.com/product/XXXX",
      "stock_selector": ".product-sales-point:not(.sold-out)",
      "sold_out_selector": ".sold-out",
      "add_to_cart_selector": ".add-to-cart-btn",
      "max_qty": 2,
      "price_selector": ".product-price"
    },
    {
      "site": "SparkFun",
      "url": "https://www.sparkfun.com/products/XXXXX",
      "stock_selector": ".product-availability--in-stock",
      "sold_out_selector": ".product-availability--out-of-stock",
      "add_to_cart_selector": ".btn-add-to-cart",
      "max_qty": 2,
      "price_selector": ".price"
    },
    {
      "site": "PiShop.us",
      "url": "https://www.pishop.us/product/raspberry-pi-5/",
      "stock_selector": ".out-of-stock",
      "sold_out_selector": ".out-of-stock",
      "add_to_cart_selector": ".single_add_to_cart_button",
      "max_qty": 1,
      "price_selector": ".price"
    }
  ],
  "checkout_profile": {
    "email": "your@email.com",
    "first_name": "Eben",
    "last_name": "Upton",
    "address": "1 Pi Tower",
    "city": "Cambridge",
    "state": "MA",
    "zip": "02139",
    "card_number": "4111111111111111",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "phone": "5551234567"
  },
  "proxies": { "list": [] },
  "captcha": { "api_key": "YOUR_2CAPTCHA_API_KEY" },
  "ebay": {
    "enabled": true,
    "markup_multiplier": 2.0,
    "auto_list": true,
    "listing_duration_days": 30
  }
}
"""
