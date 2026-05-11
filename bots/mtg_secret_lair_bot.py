#!/usr/bin/env python3
"""
mtg_secret_lair_bot.py — MTG Secret Lair Auto‑Buy Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors Secret Lair drops for launch.
2. Automatically logs in, adds multiple bundles to cart.
3. Checks out via Shopify with proxy rotation + 2Captcha.

SETUP
─────
1. Install dependencies:
      pip install requests playwright beautifulsoup4
      python -m playwright install chromium

2. Get a 2Captcha API key (https://2captcha.com/) and export:
      export CAPTCHA_API_KEY="your-key"

3. Create `secret_lair_config.json` (example at bottom).
   Provide:
   - Secret Lair shop URL and drop name.
   - Login credentials (Wizards account).
   - Checkout details (address, credit card).
   - Desired quantity (up to per‑account limit).
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
BOT_ID   = "mtg_secret_lair_bot"
BOT_NAME = "MTG Secret Lair Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "secret_lair_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "secret_lair_state.json")

SCAN_INTERVAL      = 15    # seconds between availability checks (tight around launch)
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
            "drops": [
                {
                    "name": "Secret Lair x Hatsune Miku",
                    "url": "https://secretlair.wizards.com/us/product/...",
                    "launch_time_utc": "2026-06-01T16:00:00Z",   # when the sale starts
                    "quantity": 3,                                # how many bundles to buy
                    "max_per_cart": 10,                           # Shopify limit (optional)
                    "variant": None                               # if multiple options, specify
                }
            ],
            "account": {
                "email": "your_wizards_account@email.com",
                "password": "yourpassword"
            },
            "checkout": {
                "email": "same_or_different@email.com",
                "first_name": "Jace",
                "last_name": "Beleren",
                "address": "123 Ravnica Way",
                "city": "Vryn",
                "state": "NY",
                "zip": "10001",
                "country": "US",
                "card_number": "4111111111111111",
                "card_name": "Jace Beleren",
                "card_expiry": "12/28",
                "card_cvv": "123",
                "phone": "+15551234567"
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

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"purchased_drops": {}}
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

# ── CAPTCHA solving (2Captcha) ─────────────────────────────────────────────────
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

# ── Secret Lair checkout (Shopify‑based) ──────────────────────────────────────
def perform_secret_lair_purchase(drop_cfg):
    """
    Logs into Wizards account, adds the Secret Lair product to cart
    in the desired quantity, fills Shopify checkout, solves captcha,
    and completes purchase.
    """
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed.", "error")
        return False

    account = CFG.get("account", {})
    checkout = CFG.get("checkout", {})
    proxy = next_proxy()
    launch_options = {"headless": False}  # headless may get blocked
    if proxy:
        launch_options["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_options)
        context = browser.new_context()
        page = context.new_page()
        try:
            # 1. Navigate to the product page
            _post(f"Navigating to {drop_cfg['url']}", "info")
            page.goto(drop_cfg["url"], wait_until="networkidle", timeout=30000)

            # Sometimes Secret Lair redirects to a countdown page if not live.
            # If we see a countdown or "Coming Soon", we wait or abort.
            if page.locator("text=Coming Soon").count() > 0 or page.locator("text=Release Date").count() > 0:
                _post("Drop not yet live. Waiting...", "info")
                # We could poll, but the main scan loop will retry.
                return False

            # 2. If variant selection exists (e.g., foil vs non‑foil), select it.
            variant = drop_cfg.get("variant")
            if variant:
                # Try to find a label/option with the variant name
                page.locator(f"label:has-text('{variant}')").click()
                time.sleep(1)

            # 3. Set quantity to desired number (if allowed by Shopify).
            qty = drop_cfg.get("quantity", 1)
            max_per_cart = drop_cfg.get("max_per_cart", 10)
            qty = min(qty, max_per_cart)

            # Shopify quantity input often: input[name='quantity'], input[type='number']
            qty_input = page.locator("input[name='quantity'], input[type='number'].product-form__input")
            if qty_input.count():
                qty_input.fill(str(qty))

            # 4. Click "Add to Cart" (Shopify). The button often has id 'AddToCart' or similar.
            add_to_cart_btn = page.locator("button[name='add'], button#AddToCart, button:has-text('Add to Cart')")
            page.click(add_to_cart_btn)
            _post(f"Added {qty} to cart.", "info")

            # 5. Checkout trigger: Shopify cart page /checkout
            page.goto("https://secretlair.wizards.com/checkout", wait_until="networkidle", timeout=30000)
            # If redirected to login, we need to authenticate via Wizards account.
            if page.url.startswith("https://accounts.wizards.com/"):
                _post("Logging into Wizards account...", "info")
                page.fill("input#email", account["email"])
                page.fill("input#password", account["password"])
                page.click("button[type='submit'], button:has-text('Sign In')")
                page.wait_for_load_state("networkidle")
                # After login, we should be back to checkout.

            # 6. Shopify checkout is step‑based. Fill contact info.
            _fill_shopify_checkout(page, checkout)

            # 7. Handle possible CAPTCHA on checkout (Shopify uses reCAPTCHA sometimes).
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                _post("CAPTCHA on checkout.", "info")
                if not solve_captcha(page):
                    return False
                time.sleep(2)

            # 8. Final "Pay now" / "Complete order" button
            pay_btn = page.locator("button:has-text('Pay now'), button:has-text('Complete order'), button#checkout-pay-button")
            page.click(pay_btn)
            page.wait_for_timeout(7000)

            # 9. Check for order confirmed page / thank you page
            if "thank you" in page.content().lower() or "order confirmed" in page.content().lower():
                _post(f"Order placed successfully for {drop_cfg['name']} x{qty}!", "info")
                return True
            else:
                _post("Order might not have succeeded – check manually.", "warning")
                return False
        except Exception as e:
            _post(f"Checkout error: {e}", "error")
            return False
        finally:
            browser.close()

def _fill_shopify_checkout(page, checkout):
    """Fill typical Shopify checkout fields."""
    # Shipping address
    try:
        page.fill("input[name='checkout[shipping_address][first_name]']", checkout.get("first_name", ""))
        page.fill("input[name='checkout[shipping_address][last_name]']", checkout.get("last_name", ""))
        page.fill("input[name='checkout[shipping_address][address1]']", checkout.get("address", ""))
        page.fill("input[name='checkout[shipping_address][city]']", checkout.get("city", ""))
        page.select_option("select[name='checkout[shipping_address][country]']", checkout.get("country", "US"))
        page.fill("input[name='checkout[shipping_address][zip]']", checkout.get("zip", ""))
        # Wait for state/province dropdown to load
        time.sleep(1)
        if checkout.get("state"):
            page.select_option("select[name='checkout[shipping_address][province]']", checkout["state"])
        page.fill("input[name='checkout[shipping_address][phone]']", checkout.get("phone", ""))
        # Continue to shipping method
        page.click("button:has-text('Continue to shipping')")
        page.wait_for_load_state("networkidle")

        # Shipping method (just click first available)
        page.click("button:has-text('Continue to payment')")
        page.wait_for_load_state("networkidle")
    except Exception as e:
        _post(f"Shipping form error: {e}", "warning")

    # Payment method (credit card)
    try:
        # Card number iframe? Shopify uses iframes for card details.
        # We'll try direct input if not iframes.
        card_number = page.locator("input[name='checkout[credit_card][number]'], input#number")
        if card_number.count():
            card_number.fill(checkout.get("card_number", ""))
        else:
            # Try iframe approach (common in Shopify)
            card_iframe = page.frame_locator("iframe.card-number-iframe, iframe[title='Secure card number input frame']")
            card_iframe.locator("input[name='cardnumber'], input#number").fill(checkout.get("card_number", ""))

        # Name on card
        name_input = page.locator("input[name='checkout[credit_card][name]'], input#name")
        if name_input.count():
            name_input.fill(checkout.get("card_name", ""))

        # Expiry (MM/YY or MM YYYY). Shopify often splits into two inputs.
        exp_fields = page.locator("input[name='checkout[credit_card][expiry]'], input#expiry")
        if exp_fields.count():
            exp_fields.fill(checkout.get("card_expiry", ""))
        else:
            expiry_iframe = page.frame_locator("iframe[title='Secure expiration date input frame']")
            expiry_iframe.locator("input[name='expdate'], input#expiry").fill(checkout.get("card_expiry", ""))

        # CVV
        cvv_input = page.locator("input[name='checkout[credit_card][cvv]'], input#cvv")
        if cvv_input.count():
            cvv_input.fill(checkout.get("card_cvv", ""))
        else:
            cvv_iframe = page.frame_locator("iframe[title='Secure CVC input frame']")
            cvv_iframe.locator("input[name='cvc'], input#cvv").fill(checkout.get("card_cvv", ""))

    except Exception as e:
        _post(f"Payment form error: {e}", "warning")

# ── Scheduling & monitoring ───────────────────────────────────────────────────
def check_and_buy():
    state = load_state()
    now = datetime.utcnow()
    drops = CFG.get("drops", [])

    for drop in drops:
        launch_time_str = drop.get("launch_time_utc")
        if launch_time_str:
            launch_time = datetime.fromisoformat(launch_time_str.replace("Z", "+00:00"))
            if now < launch_time:
                _post(f"{drop['name']} drops at {launch_time.isoformat()}. Waiting...", "info")
                continue

        # Prevent duplicate purchase per drop name
        drop_id = drop["name"]
        if state.get("purchased_drops", {}).get(drop_id):
            continue

        _post(f"Attempting to buy {drop['name']} x{drop.get('quantity',1)}...", "error")

        success = perform_secret_lair_purchase(drop)
        if success:
            state.setdefault("purchased_drops", {})[drop_id] = datetime.utcnow().isoformat()
            save_state(state)
        time.sleep(5)  # small pause between drops

def main():
    _wait_for_hub()
    _post("MTG Secret Lair Bot online. Monitoring drops and ready to purchase.", "info")

    while True:
        check_and_buy()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `secret_lair_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "drops": [
    {
      "name": "Secret Lair x Hatsune Miku",
      "url": "https://secretlair.wizards.com/us/product/...",
      "launch_time_utc": "2026-06-01T16:00:00Z",
      "quantity": 3,
      "max_per_cart": 10,
      "variant": "Foil Edition"
    },
    {
      "name": "Secret Lair: Showcase: Phyrexian Praetors",
      "url": "https://secretlair.wizards.com/us/product/...",
      "launch_time_utc": "2026-07-15T16:00:00Z",
      "quantity": 2
    }
  ],
  "account": {
    "email": "you@domain.com",
    "password": "your-password"
  },
  "checkout": {
    "email": "same_or_diff@domain.com",
    "first_name": "Jace",
    "last_name": "Beleren",
    "address": "123 Ravnica Way",
    "city": "Vryn",
    "state": "NY",
    "zip": "10001",
    "country": "US",
    "card_number": "4111111111111111",
    "card_name": "Jace Beleren",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "phone": "+15551234567"
  },
  "proxies": {
    "list": []
  },
  "captcha": {
    "api_key": "YOUR_2CAPTCHA_API_KEY"
  }
}
"""
