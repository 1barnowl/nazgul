#!/usr/bin/env python3
"""
nvidia_rtx_fe_bot.py — NVIDIA RTX Founders Edition Auto‑Buy Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors Best Buy product page for RTX FE drops.
2. Enters Queue‑it waiting room automatically.
3. Once queue passes, adds to cart and checks out.
4. Uses proxy rotation + 2Captcha.

SETUP
─────
1. Install dependencies:
      pip install playwright requests beautifulsoup4
      python -m playwright install chromium

2. Export 2Captcha API key:
      export CAPTCHA_API_KEY="your-key"

3. Create `nvidia_fe_config.json` (example at bottom).
   Provide:
   - Best Buy SKU / product URL for the RTX FE you want.
   - Best Buy account credentials (recommended).
   - Checkout details (address, credit card).
   - Desired quantity (usually limit 1).
   - Proxy list (optional).

4. Attach to BotController.
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

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "nvidia_rtx_fe_bot"
BOT_NAME = "NVIDIA RTX FE Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "nvidia_fe_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "nvidia_fe_state.json")

SCAN_INTERVAL      = 10    # seconds between product stock checks
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
            "product": {
                "name": "NVIDIA GeForce RTX 5090 Founders Edition",
                "sku": "6521434",   # alternate: product url
                "url": "https://www.bestbuy.com/site/nvidia-geforce-rtx-5090-founders-edition/6521434.p?skuId=6521434",
                "quantity": 1,
                "max_price": 1999.99
            },
            "bestbuy_account": {
                "email": "your_bestbuy_account@email.com",
                "password": "your_password"
            },
            "checkout": {
                "email": "your_bestbuy_account@email.com",
                "first_name": "Alan",
                "last_name": "Turing",
                "address": "1 Infinity Loop",
                "city": "Cupertino",
                "state": "CA",
                "zip": "95014",
                "card_number": "4111111111111111",
                "card_expiry": "12/28",
                "card_cvv": "123",
                "phone": "5551234567"
            },
            "proxies": {
                "list": []
            },
            "captcha": {
                "api_key": os.getenv("CAPTCHA_API_KEY", ""),
                "service": "2captcha"
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
        return {"last_purchase": None}
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

# ── Best Buy checkout automation ───────────────────────────────────────────────
def attempt_purchase():
    product = CFG["product"]
    account = CFG.get("bestbuy_account", {})
    checkout = CFG.get("checkout", {})
    proxy = next_proxy()
    launch_options = {"headless": False}  # some anti‑bot detection may require headed
    if proxy:
        launch_options["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_options)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Go to product page
            _post(f"Navigating to {product['url']}", "info")
            page.goto(product["url"], wait_until="domcontentloaded", timeout=30000)

            # 2. Handle Queue‑it waiting room
            # Best Buy uses Queue‑it. The page will redirect to a queue url.
            # We wait until the "Add to Cart" button becomes visible.
            # The waiting room might show a progress bar; we just poll the page.
            start_time = datetime.utcnow()
            while True:
                # Check if we are still on a queue page (selectors change)
                if page.url.startswith("https://queue.bestbuy.com") or "queue" in page.url:
                    _post("Still in queue... waiting.", "info")
                    time.sleep(5)
                    # Refresh the main product page maybe? Queue-it normally redirects back automatically.
                    # To be safe, we reload the original URL periodically to see if queue has passed.
                    page.goto(product["url"], wait_until="domcontentloaded", timeout=30000)
                    continue

                # Look for "Add to Cart" button (Best Buy uses class .add-to-cart-button)
                add_btn = page.locator("button.add-to-cart-button, .fulfillment-add-to-cart-button button")
                if add_btn.is_visible() and add_btn.is_enabled():
                    _post("Add to Cart button visible! Queue passed.", "info")
                    break

                # Also check if "Sold Out" appears
                if page.locator("button.sold-out, text=Sold Out").count():
                    _post("GPU is sold out.", "error")
                    return False

                # If queue is taking too long, abandon after 5 minutes
                if (datetime.utcnow() - start_time).seconds > 300:
                    _post("Queue timed out. GPU likely sold out before we got through.", "warning")
                    return False
                time.sleep(2)

            # 3. Click "Add to Cart"
            page.click("button.add-to-cart-button, .fulfillment-add-to-cart-button button")
            _post("Added to cart.", "info")

            # 4. Navigate to cart/checkout
            page.goto("https://www.bestbuy.com/cart", wait_until="networkidle", timeout=30000)
            if not page.locator("text=Checkout").is_visible():
                _post("Cart is empty or checkout not available.", "error")
                return False

            page.click("text=Checkout, button:has-text('Checkout')")
            page.wait_for_load_state("networkidle")

            # 5. If not logged in, log in now
            if page.url.startswith("https://www.bestbuy.com/identity/signin"):
                _post("Logging into Best Buy account...", "info")
                page.fill("input#fld-e", account["email"])
                page.fill("input#fld-p1", account["password"])
                page.click("button.cia-form__controls__submit, button:has-text('Sign In')")
                page.wait_for_load_state("networkidle")

            # 6. Fill shipping/payment (Best Buy checkout can be a single page or multi‑step)
            # Best Buy uses a “Checkout as Guest” or account‑linked checkout.
            # We fill in the form if it appears.
            _fill_bestbuy_checkout(page, checkout)

            # 7. Need to handle CAPTCHA possibly on checkout (less common, but can happen)
            if page.locator("iframe[title*='captcha']").count():
                solve_captcha(page)

            # 8. Place order (final button text: "Place Your Order")
            place_order_btn = page.locator("button:has-text('Place Your Order'), button.checkout__place-order-button")
            if place_order_btn.is_visible():
                place_order_btn.click()
                _post("Order submitted!", "info")
            else:
                _post("Cannot find Place Order button.", "error")
                return False

            page.wait_for_timeout(8000)

            if "thank you" in page.content().lower() or "order number" in page.content().lower():
                _post(f"Purchase of {product['name']} successful!", "info")
                return True
            else:
                _post("Order may not have gone through.", "warning")
                return False
        except Exception as e:
            _post(f"Purchase attempt error: {e}", "error")
            return False
        finally:
            browser.close()

def _fill_bestbuy_checkout(page, checkout):
    """Fill Best Buy checkout form fields. Selectors vary over time."""
    # Shipping address (if needed)
    try:
        page.fill("input#consolidatedAddresses_shippingAddress_1_firstName", checkout.get("first_name", ""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_lastName", checkout.get("last_name", ""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_address", checkout.get("address", ""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_city", checkout.get("city", ""))
        page.select_option("select#consolidatedAddresses_shippingAddress_1_state", checkout.get("state", ""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_zipcode", checkout.get("zip", ""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_phone", checkout.get("phone", ""))
    except Exception:
        pass

    # Continue to payment if needed
    try:
        page.click("button:has-text('Continue to Payment')")
        page.wait_for_load_state("networkidle")
    except:
        pass

    # Credit card details
    try:
        page.fill("input#optimized-cc-card-number", checkout.get("card_number", ""))
        page.fill("input[name='expirationDate']", checkout.get("card_expiry", ""))
        page.fill("input#credit-card-cvv", checkout.get("card_cvv", ""))
        page.fill("input[name='cvv']", checkout.get("card_cvv", ""))  # alternate
    except:
        pass

# ── Monitoring loop ────────────────────────────────────────────────────────────
def monitor():
    state = load_state()
    # Only attempt if not purchased in last day (to prevent double buying)
    last = state.get("last_purchase")
    if last:
        last_time = datetime.fromisoformat(last)
        if (datetime.utcnow() - last_time) < timedelta(hours=1):
            _post("Already purchased within the last hour. Skipping.", "info")
            return

    _post(f"Attempting to purchase {CFG['product']['name']}...", "error")
    success = attempt_purchase()
    if success:
        state["last_purchase"] = datetime.utcnow().isoformat()
        save_state(state)

def main():
    _wait_for_hub()
    _post("NVIDIA RTX FE Bot online. Monitoring Best Buy for Founders Edition drops.", "info")

    while True:
        monitor()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `nvidia_fe_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "product": {
    "name": "NVIDIA GeForce RTX 5090 Founders Edition",
    "sku": "6521434",
    "url": "https://www.bestbuy.com/site/nvidia-geforce-rtx-5090-founders-edition/6521434.p?skuId=6521434",
    "quantity": 1,
    "max_price": 1999.99
  },
  "bestbuy_account": {
    "email": "you@email.com",
    "password": "your-password"
  },
  "checkout": {
    "email": "you@email.com",
    "first_name": "Alan",
    "last_name": "Turing",
    "address": "1 Infinity Loop",
    "city": "Cupertino",
    "state": "CA",
    "zip": "95014",
    "card_number": "4111111111111111",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "phone": "5551234567"
  },
  "proxies": {
    "list": []
  },
  "captcha": {
    "api_key": "YOUR_2CAPTCHA_API_KEY"
  }
}
"""
