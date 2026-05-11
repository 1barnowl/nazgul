#!/usr/bin/env python3
"""
apple_vision_pro_bot.py — Apple Vision Pro Launch Day Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Cycles through multiple Apple IDs.
2. Adds the top‑tier Vision Pro to bag.
3. Selects in‑store pickup & books a time slot.
4. Completes checkout with stored payment.

✦ THIS IS FOR RESEARCH / EDUCATIONAL PURPOSES ONLY.
✦ Apple employs strong anti‑bot measures — do not use
  against the live store without explicit permission.

SETUP
─────
1. Install dependencies:
      pip install playwright requests
      python -m playwright install chromium

2. Export 2Captcha API key:
      export CAPTCHA_API_KEY="your-key"

3. Create `apple_vision_pro_config.json` (example at bottom).
   Each account must include shipping/payment and store preference.

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
BOT_ID   = "apple_vision_pro_bot"
BOT_NAME = "Apple Vision Pro Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "apple_vision_pro_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "apple_vision_pro_state.json")

SCAN_INTERVAL      = 15    # seconds between attempts per account
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
    default = {
        "product": {
            "url": "https://www.apple.com/shop/buy-vision/apple-vision-pro",
            "storage_option": "1TB",  # select dropdown text
            "quantity": 1
        },
        "accounts": [
            {
                "apple_id": "user1@icloud.com",
                "password": "app-id-password",
                "first_name": "Tim",
                "last_name": "Cook",
                "address": "1 Apple Park Way",
                "city": "Cupertino",
                "state": "CA",
                "zip": "95014",
                "phone": "4089961010",
                "card_number": "4111111111111111",
                "card_expiry": "12/28",
                "card_cvv": "123",
                "preferred_store": "Apple Park Visitor Center"  # or store number
            }
        ],
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"}
    }
    if not os.path.exists(CONFIG_FILE):
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

# ── Apple Store automation ────────────────────────────────────────────────────
def attempt_purchase(account):
    """Try to purchase a Vision Pro with the given account details."""
    product = CFG["product"]
    proxy = next_proxy()
    launch_options = {"headless": False}  # headed recommended for anti‑bot
    if proxy:
        launch_options["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_options)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Navigate to product page
            _post(f"Navigating to {product['url']} with account {account['apple_id']}", "info")
            page.goto(product["url"], wait_until="networkidle", timeout=30000)

            # 2. Handle country/language selection if any
            if page.locator("text=Select your country/region").count():
                page.click("text=United States")

            # 3. Select highest storage option
            storage_option = product.get("storage_option", "1TB")
            # Apple uses radio buttons with labels
            storage_radio = page.locator(f"label:has-text('{storage_option}') input")
            if storage_radio.count():
                storage_radio.check()
                _post(f"Selected {storage_option}.", "info")
            else:
                # Try dropdown
                dropdown = page.locator("select[data-autom='storage']")
                if dropdown.count():
                    dropdown.select_option(label=storage_option)

            # 4. Click "Add to Bag" (button text: "Add to Bag" or "Continue")
            add_btn = page.locator("button:has-text('Add to Bag'), button[name='add-to-cart']")
            if not add_btn.is_visible():
                _post("Add to Bag not visible. Product not available?", "error")
                return False
            add_btn.click()
            _post("Added to bag.", "info")

            # 5. Proceed to checkout (Bag page)
            page.goto("https://www.apple.com/shop/bag", wait_until="networkidle", timeout=30000)
            # Continue through bag to Checkout
            page.click("button:has-text('Checkout')")
            page.wait_for_load_state("networkidle")

            # 6. Sign in if not already
            if page.url.startswith("https://appleid.apple.com"):
                _post("Signing into Apple ID...", "info")
                page.fill("input#account_name_text_field", account["apple_id"])
                page.click("button[type='submit']")
                # Wait for password field
                page.wait_for_selector("input#password_text_field", timeout=10000)
                page.fill("input#password_text_field", account["password"])
                page.click("button[type='submit']")
                page.wait_for_load_state("networkidle")
                # Handle 2FA? (if required, would need manual intervention or an SMS API)
                if "verify" in page.url.lower():
                    _post("2FA required – cannot automate. Skipping account.", "error")
                    return False

            # 7. Choose delivery method: "I'll pick it up" or similar
            pickup_option = page.locator("input#delivery-method-pickup, label:has-text('Pick up')")
            if pickup_option.count():
                pickup_option.check()
                _post("Selected in‑store pickup.", "info")
            else:
                _post("Pickup option not available.", "error")
                return False

            # 8. Select store (preferred store from config)
            store_name = account.get("preferred_store", "")
            if store_name:
                page.fill("input[name='store-search'], input[placeholder='Enter ZIP or City']", store_name)
                time.sleep(2)
                # Pick first matching store
                page.click(".store-list-item:first-child, button:has-text('Select')")
                page.wait_for_timeout(2000)

            # 9. Pick appointment time slot – Apple shows a calendar/time picker
            if page.locator(".time-slot-grid, .appointment-time").count():
                # Try to pick the earliest available time
                available_slots = page.locator("button.time-slot, .time-slot-item:not(.unavailable)")
                if available_slots.count():
                    available_slots.first().click()
                    _post("Selected earliest appointment.", "info")
                else:
                    _post("No available time slots.", "error")
                    return False
            else:
                # Might be automatic or not required
                pass

            # 10. Fill shipping / billing details if not already on file
            # (Apple often stores payment, but we fill if fields appear)
            if page.locator("input#addressLine1").is_visible():
                _post("Filling shipping details.", "info")
                page.fill("input#firstName", account["first_name"])
                page.fill("input#lastName", account["last_name"])
                page.fill("input#addressLine1", account["address"])
                page.fill("input#city", account["city"])
                page.select_option("select#state", account["state"])
                page.fill("input#zipCode", account["zip"])
                page.fill("input#phone", account["phone"])
                page.click("button:has-text('Continue')")

            # Payment card
            if page.locator("input#cardNumber").is_visible():
                _post("Filling credit card.", "info")
                page.fill("input#cardNumber", account["card_number"])
                page.fill("input[name='expirationDate']", account["card_expiry"])
                page.fill("input#cvv", account["card_cvv"])

            # 11. Handle CAPTCHA if needed (Apple uses Apple’s own sometimes)
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                solve_captcha(page)

            # 12. Place Order
            order_btn = page.locator("button:has-text('Place Order'), button[name='placeOrder']")
            if order_btn.is_visible():
                order_btn.click()
                _post("Order submitted!", "info")
            else:
                _post("Cannot find Place Order button.", "error")
                return False

            page.wait_for_timeout(8000)
            if "thank you" in page.content().lower() or "order number" in page.content().lower():
                _post(f"Purchase successful with {account['apple_id']}!", "info")
                return True
            else:
                _post("Order may not have completed.", "warning")
                return False
        except Exception as e:
            _post(f"Purchase attempt error: {e}", "error")
            return False
        finally:
            browser.close()

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    _post("Apple Vision Pro Bot online. Cycling through accounts.", "info")
    state = load_state()

    while True:
        for account in CFG["accounts"]:
            # Skip if already purchased with this account (manually tracked)
            if account["apple_id"] in state.get("purchased", []):
                continue
            _post(f"Trying account: {account['apple_id']}", "info")
            success = attempt_purchase(account)
            if success:
                state.setdefault("purchased", []).append(account["apple_id"])
                save_state(state)
            # Small delay between accounts
            time.sleep(SCAN_INTERVAL)
        _heartbeat()
        # After cycling all accounts, wait a bit longer
        time.sleep(600)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `apple_vision_pro_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "product": {
    "url": "https://www.apple.com/shop/buy-vision/apple-vision-pro",
    "storage_option": "1TB",
    "quantity": 1
  },
  "accounts": [
    {
      "apple_id": "user1@icloud.com",
      "password": "app-id-password",
      "first_name": "Tim",
      "last_name": "Cook",
      "address": "1 Apple Park Way",
      "city": "Cupertino",
      "state": "CA",
      "zip": "95014",
      "phone": "4089961010",
      "card_number": "4111111111111111",
      "card_expiry": "12/28",
      "card_cvv": "123",
      "preferred_store": "Apple Park Visitor Center"
    },
    {
      "apple_id": "user2@gmail.com",
      "password": "another-password",
      "first_name": "Craig",
      "last_name": "Federighi",
      "address": "2 Infinite Loop",
      "city": "Cupertino",
      "state": "CA",
      "zip": "95014",
      "phone": "4085551212",
      "card_number": "5555555555554444",
      "card_expiry": "11/27",
      "card_cvv": "321",
      "preferred_store": "Apple Valley Fair"
    }
  ],
  "proxies": {
    "list": []
  },
  "captcha": {
    "api_key": "YOUR_2CAPTCHA_KEY"
  }
}
"""
