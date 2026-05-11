#!/usr/bin/env python3
"""
ps5_anniversary_bot.py — PS5 Anniversary Edition Auto‑Buy Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors PlayStation Direct for the 30th Anniversary bundle.
2. Enters the queue automatically and waits.
3. When it's your turn, logs into PSN, adds to cart, checks out.
4. Proxies + 2Captcha for anti‑bot countermeasures.

SETUP
─────
1. Install dependencies:
      pip install playwright requests beautifulsoup4
      python -m playwright install chromium

2. Set 2Captcha API key:
      export CAPTCHA_API_KEY="your-key"

3. Create `ps5_anniversary_config.json` (example at bottom).
   Provide:
   - PlayStation Direct product URL (once available).
   - Your PSN account email and password.
   - Checkout details (address, credit card).

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
BOT_ID   = "ps5_anniversary_bot"
BOT_NAME = "PS5 Anniversary Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "ps5_anniversary_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "ps5_anniversary_state.json")

SCAN_INTERVAL      = 10    # seconds between availability checks
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
                "name": "PlayStation 5 Pro 30th Anniversary Limited Edition Bundle",
                "url": "https://direct.playstation.com/en-us/buy-consoles/ps5-pro-30th-anniversary-bundle",
                "quantity": 1,
                "max_price": 999.99
            },
            "psn_account": {
                "email": "your_psn@email.com",
                "password": "your_password"
            },
            "checkout": {
                "first_name": "Player",
                "last_name": "One",
                "address": "100 PlayStation Way",
                "city": "San Mateo",
                "state": "CA",
                "zip": "94404",
                "card_number": "4111111111111111",
                "card_expiry": "12/28",
                "card_cvv": "123",
                "phone": "4155551234"
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
        return {"purchased": False}
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

# ── PlayStation Direct checkout ────────────────────────────────────────────────
def attempt_ps5_purchase():
    product = CFG["product"]
    account = CFG["psn_account"]
    checkout = CFG["checkout"]
    proxy = next_proxy()
    launch_options = {"headless": False}  # headed may be more reliable for queue
    if proxy:
        launch_options["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_options)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Navigate to product page (likely redirects to queue)
            _post(f"Navigating to {product['url']}", "info")
            page.goto(product["url"], wait_until="domcontentloaded", timeout=30000)

            # 2. Queue handling – PlayStation Direct uses a “queue‑it‑like” waiting room.
            # Check for queue redirect or page content.
            start = datetime.utcnow()
            while True:
                # If still on a queue page (e.g., url contains 'queue')
                if "queue" in page.url.lower() or "waiting-room" in page.url.lower():
                    _post("In queue... waiting.", "info")
                    time.sleep(5)
                    # Refresh product page to see if we are out
                    page.goto(product["url"], wait_until="domcontentloaded", timeout=30000)
                    continue

                # Look for Add to Cart button (PlayStation Direct uses class .add-to-cart, or data‑test)
                add_btn = page.locator("button.add-to-cart, [data-test='add-to-cart'], button:has-text('Add to Cart')")
                if add_btn.is_visible() and add_btn.is_enabled():
                    _post("Out of queue! Add to Cart visible.", "info")
                    break

                # Check sold out
                if page.locator("text=Sold Out").count():
                    _post("Bundle is sold out.", "error")
                    return False

                if (datetime.utcnow() - start).seconds > 600:
                    _post("Queue timeout. Likely sold out.", "warning")
                    return False
                time.sleep(2)

            # 3. Click Add to Cart
            add_btn.click()
            _post("Added to cart.", "info")

            # 4. Checkout – usually redirects to checkout page
            page.goto("https://direct.playstation.com/en-us/checkout", wait_until="networkidle", timeout=30000)

            # 5. Login if required
            if page.url.startswith("https://auth.api.sonyentertainmentnetwork.com") or "signin" in page.url.lower():
                _post("Logging into PSN account...", "info")
                page.fill("input#signin-email, input[name='email']", account["email"])
                page.fill("input#signin-password, input[name='password']", account["password"])
                page.click("button[type='submit'], button:has-text('Sign In')")
                page.wait_for_load_state("networkidle")

            # 6. Fill shipping/payment
            _fill_playstation_checkout(page, checkout)

            # 7. CAPTCHA?
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                solve_captcha(page)
                time.sleep(2)

            # 8. Place order
            order_btn = page.locator("button:has-text('Place Order'), button[data-test='placeOrder'], button#checkout-submit")
            if order_btn.is_visible():
                order_btn.click()
                _post("Order submitted", "info")
            else:
                _post("Cannot find Place Order button.", "error")
                return False

            page.wait_for_timeout(8000)
            if "thank you" in page.content().lower() or "order confirmed" in page.content().lower():
                _post("Purchase successful!", "info")
                return True
            else:
                _post("Order may not have succeeded.", "warning")
                return False
        except Exception as e:
            _post(f"Checkout error: {e}", "error")
            return False
        finally:
            browser.close()

def _fill_playstation_checkout(page, checkout):
    """Fill typical PlayStation Direct checkout fields."""
    try:
        page.fill("input#firstName, input[name='firstName']", checkout.get("first_name", ""))
        page.fill("input#lastName, input[name='lastName']", checkout.get("last_name", ""))
        page.fill("input#addressLine1, input[name='addressLine1']", checkout.get("address", ""))
        page.fill("input#city, input[name='city']", checkout.get("city", ""))
        page.select_option("select#state, select[name='state']", checkout.get("state", ""))
        page.fill("input#zipCode, input[name='zipCode']", checkout.get("zip", ""))
        page.fill("input#phone, input[name='phone']", checkout.get("phone", ""))

        # Continue to payment
        page.click("button:has-text('Continue')")
        page.wait_for_timeout(2000)

        # Payment details
        page.fill("input#cardNumber, input[name='cardNumber']", checkout.get("card_number", ""))
        page.fill("input[name='expirationDate']", checkout.get("card_expiry", ""))
        page.fill("input#cvv, input[name='cvv']", checkout.get("card_cvv", ""))
    except Exception as e:
        _post(f"Checkout form fill error: {e}", "warning")

# ── Monitoring loop ────────────────────────────────────────────────────────────
def monitor():
    state = load_state()
    if state.get("purchased"):
        _post("Already purchased. Stopping to avoid duplicate order.", "info")
        return

    _post("Attempting to buy PS5 30th Anniversary bundle...", "error")
    success = attempt_ps5_purchase()
    if success:
        state["purchased"] = True
        save_state(state)

def main():
    _wait_for_hub()
    _post("PS5 Anniversary Edition Bot online. Ready to purchase.", "info")

    while True:
        monitor()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `ps5_anniversary_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "product": {
    "name": "PS5 Pro 30th Anniversary Bundle",
    "url": "https://direct.playstation.com/en-us/buy-consoles/ps5-pro-30th-anniversary-bundle",
    "quantity": 1,
    "max_price": 999.99
  },
  "psn_account": {
    "email": "your_psn@email.com",
    "password": "your_password"
  },
  "checkout": {
    "first_name": "Player",
    "last_name": "One",
    "address": "100 PlayStation Way",
    "city": "San Mateo",
    "state": "CA",
    "zip": "94404",
    "card_number": "4111111111111111",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "phone": "4155551234"
  },
  "proxies": {
    "list": []
  },
  "captcha": {
    "api_key": "YOUR_2CAPTCHA_KEY"
  }
}
"""
