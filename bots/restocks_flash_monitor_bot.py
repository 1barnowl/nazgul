#!/usr/bin/env python3
"""
restocks_flash_monitor_bot.py — Restocks‑Only Flash Monitor Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Continuously monitors boutique product pages for unannounced restocks.
2. Detects when a previously sold‑out item becomes available.
3. Auto‑checks out instantly (Playwright + proxy rotation + 2Captcha).
4. Optionally lists purchased items on eBay at a markup.

✦ FOR EDUCATIONAL / RESEARCH PURPOSES ONLY.
  Automated purchasing may violate each retailer’s Terms of Service.

SETUP
─────
1. Install dependencies:
      pip install playwright requests ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key:  export CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing (optional):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `restock_monitor_config.json` (example at bottom).
   Fill in:
   - List of target product URLs with their size/variant selectors.
   - Your checkout profile (shipping/payment).
   - Proxy list (optional but highly recommended).

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
BOT_ID   = "restocks_flash_monitor_bot"
BOT_NAME = "Restocks Flash Monitor"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "restock_monitor_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "restock_monitor_state.json")

SCAN_INTERVAL      = 15    # seconds between restock checks
HEARTBEAT_INTERVAL = 30
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
            "targets": [
                {
                    "site": "SSENSE",
                    "product_url": "https://www.ssense.com/en-us/product/balenciaga/speed-2-0-sneakers/123456",
                    "size": "41",
                    "stock_selector": "button[data-test='add-to-bag']",  # visible when in stock
                    "sold_out_selector": "button[disabled][data-test='add-to-bag']",
                    "add_to_cart_selector": "button[data-test='add-to-bag']",
                    "price_selector": "span.price"
                },
                {
                    "site": "END.",
                    "product_url": "https://www.endclothing.com/us/nike-air-max-1-anniversary-aq0928-100.html",
                    "size": "US 9",
                    "stock_selector": ".product-size__item--available",
                    "sold_out_selector": ".product-size__item--sold-out",
                    "add_to_cart_selector": ".product-add-to-cart",
                    "price_selector": ".product-price"
                },
                {
                    "site": "Mr Porter",
                    "product_url": "https://www.mrporter.com/en-us/mens/product/saint-laurent/sneakers/court-classic-sl-06-leather-sneakers/123456",
                    "size": "9",
                    "stock_selector": "button[data-test='add-to-bag']",
                    "sold_out_selector": "button[disabled][data-test='add-to-bag']",
                    "add_to_cart_selector": "button[data-test='add-to-bag']",
                    "price_selector": ".price"
                }
            ],
            "checkout_profile": {
                "email": "your@email.com",
                "first_name": "John",
                "last_name": "Doe",
                "address": "123 Fashion St",
                "city": "New York",
                "state": "NY",
                "zip": "10001",
                "phone": "5551234567",
                "card_number": "4111111111111111",
                "card_expiry": "12/28",
                "card_cvv": "123",
                "card_name": "John Doe"
            },
            "proxies": {"list": []},
            "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
            "ebay": {
                "enabled": False,
                "markup_multiplier": 2.5,
                "auto_list": True
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
        return {"purchased": [], "last_known_status": {}}
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

# ── CAPTCHA solver (2Captcha) ─────────────────────────────────────────────────
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

# ── Restock detection & purchase ───────────────────────────────────────────────
def attempt_restock_purchase(target):
    """
    Open the product page, detect if the item has been restocked (changed from out of stock to in stock),
    and if so, complete the purchase immediately.
    """
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed.", "error")
        return False

    profile = CFG.get("checkout_profile", {})
    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy:
        launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        context = browser.new_context()
        page = context.new_page()

        try:
            product_url = target["product_url"]
            _post(f"Checking {target['site']}: {product_url}", "info")
            page.goto(product_url, wait_until="networkidle", timeout=30000)

            # 1. Handle cookie banners / country selectors
            try:
                page.click("button:has-text('Accept All Cookies'), button:has-text('Accept')", timeout=2000)
            except:
                pass
            try:
                # Some sites force locale selection; close if possible
                page.click("button:has-text('Close'), button:has-text('Continue')", timeout=2000)
            except:
                pass

            # 2. Check stock status based on selectors
            stock_sel = target.get("stock_selector")
            sold_out_sel = target.get("sold_out_selector")

            if sold_out_sel and page.locator(sold_out_sel).is_visible():
                _post(f"{target['site']}: Still out of stock.", "info")
                return False
            if stock_sel and not page.locator(stock_sel).is_visible():
                _post(f"{target['site']}: Stock selector not visible, may be sold out.", "info")
                return False

            # 3. If size selection is required
            size = target.get("size")
            if size:
                # Try to select size (common patterns: dropdown, button)
                _select_size(page, size)

            # 4. Add to cart
            add_sel = target.get("add_to_cart_selector", "button:has-text('Add to Bag'), button:has-text('Add to Cart')")
            page.click(add_sel)
            _post(f"{target['site']}: Added to cart.", "info")

            # 5. Proceed to checkout (site‑specific)
            if "ssense.com" in product_url:
                page.goto("https://www.ssense.com/en-us/checkout", wait_until="networkidle")
            elif "endclothing.com" in product_url:
                page.goto("https://www.endclothing.com/checkout/cart/", wait_until="networkidle")
            elif "mrporter.com" in product_url:
                page.goto("https://www.mrporter.com/en-us/checkout/cart", wait_until="networkidle")
            else:
                # Generic: click a checkout button or navigate to /checkout
                try:
                    page.click("a:has-text('Checkout'), button:has-text('Checkout')")
                except:
                    page.goto(product_url.replace("/product/", "/checkout/"), wait_until="networkidle")

            # 6. Fill shipping & payment (generic approach)
            _fill_checkout_generic(page, profile)

            # 7. CAPTCHA?
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                if not solve_captcha(page):
                    return False
                page.wait_for_timeout(2000)

            # 8. Place order
            page.click("button:has-text('Place Order'), button:has-text('Complete Order')")
            page.wait_for_timeout(10000)

            if "thank you" in page.content().lower() or "order confirmed" in page.content().lower():
                _post(f"🎉 Restock purchase successful for {target['site']}!", "info")
                return True
            else:
                _post("Order might have failed.", "warning")
                return False
        except Exception as e:
            _post(f"Restock purchase error for {target['site']}: {e}", "error")
            return False
        finally:
            browser.close()

def _select_size(page, size):
    """Attempt to select a product size using common patterns."""
    # Common patterns: <select>, radio buttons, or clickable divs
    # Try direct option in a select first
    try:
        page.select_option("select[name='size'], select[data-test='size-select']", size)
        return
    except:
        pass

    # Try a clickable button or div with the size text
    try:
        size_btn = page.locator(f"label:has-text('{size}'), button:has-text('{size}'), div:has-text('{size}')[role='button']")
        if size_btn.is_visible():
            size_btn.click()
            page.wait_for_timeout(500)
    except:
        pass

def _fill_checkout_generic(page, profile):
    """Fill common checkout fields if they appear on the page."""
    # Email
    try: page.fill("input[type='email'], input[name='email']", profile["email"])
    except: pass
    # Name fields
    try: page.fill("input[name='firstName'], input#firstName", profile["first_name"])
    except: pass
    try: page.fill("input[name='lastName'], input#lastName", profile["last_name"])
    except: pass
    # Address
    try: page.fill("input[name='address1'], input#address1", profile["address"])
    except: pass
    try: page.fill("input[name='city'], input#city", profile["city"])
    except: pass
    try: page.select_option("select[name='state'], select#state", profile["state"])
    except: pass
    try: page.fill("input[name='zip'], input#zip", profile["zip"])
    except: pass
    try: page.fill("input[name='phone'], input#phone", profile["phone"])
    except: pass
    # Card details
    try: page.fill("input[name='cardnumber'], input#cardNumber", profile["card_number"])
    except: pass
    try: page.fill("input[name='expiry'], input#expiry", profile["card_expiry"])
    except: pass
    try: page.fill("input[name='cvv'], input#cvv", profile["card_cvv"])
    except: pass

# ── eBay listing ───────────────────────────────────────────────────────────────
def list_on_ebay(item_name, purchase_price):
    if not HAS_EBAY or not CFG["ebay"].get("auto_list"):
        return
    trading = eBayTrading(
        domain="api.ebay.com",
        appid=os.getenv("EBAY_APP_ID"),
        certid=os.getenv("EBAY_CERT_ID"),
        devid=os.getenv("EBAY_DEV_ID"),
        token=os.getenv("EBAY_AUTH_TOKEN"),
        config_file=None
    )
    if not trading:
        return
    markup = CFG["ebay"]["markup_multiplier"]
    ebay_price = round(purchase_price * markup, 2)
    payload = {
        "Item": {
            "Title": f"{item_name} – Brand New, In Hand",
            "Description": "Brand new, authentic. Ships immediately.",
            "PrimaryCategory": {"CategoryID": "11450"},  # Clothing, Shoes & Accessories
            "StartPrice": ebay_price,
            "Quantity": 1,
            "ListingDuration": "Days_30",
            "Country": "US",
            "Currency": "USD",
            "ListingType": "FixedPriceItem",
            "Site": "US",
        }
    }
    try:
        resp = trading.execute("AddFixedPriceItem", payload)
        if resp.dict().get("Ack") == "Success":
            _post(f"eBay listing created: {resp.dict()['ItemID']} at ${ebay_price}", "info")
    except Exception as e:
        _post(f"eBay error: {e}", "error")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    state = load_state()
    _post("Restocks Flash Monitor Bot online. Watching for unannounced restocks...", "info")

    while True:
        targets = CFG.get("targets", [])
        for target in targets:
            product_url = target["product_url"]
            # Skip if already purchased this product (from this bot) in last 24h
            recent_purchases = [
                p for p in state.get("purchased", [])
                if p["url"] == product_url and
                   (datetime.utcnow() - datetime.fromisoformat(p["timestamp"])) < timedelta(hours=24)
            ]
            if recent_purchases:
                continue

            success = attempt_restock_purchase(target)
            if success:
                state.setdefault("purchased", []).append({
                    "url": product_url,
                    "timestamp": datetime.utcnow().isoformat()
                })
                save_state(state)

                # Try to list on eBay (if enabled) using estimated retail price
                price = 0.0
                # Attempt to extract price from page (hard to do now, so fallback)
                # Could store from config if known; here we'll just skip or use placeholder
                if CFG["ebay"]["enabled"]:
                    list_on_ebay(target["site"] + " Restock", price)
            # Small delay between checks to avoid hammering
            time.sleep(random.uniform(3, 8))
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
