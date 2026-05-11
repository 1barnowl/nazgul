#!/usr/bin/env python3
"""
disney_ap_bot.py — Disney World Annual Pass Auto‑Purchase Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors the Disney World Annual Pass sales page for when sales resume.
2. When the queue opens, automatically navigates the waiting room.
3. Logs into your Disney account, selects the desired pass type.
4. Completes checkout with stored payment.
5. Optionally lists the pass activation or account on eBay.

✦ FOR EDUCATIONAL / RESEARCH PURPOSES ONLY.
  Automated purchasing violates Disney’s Terms of Service and
  may result in permanent bans and loss of Annual Pass benefits.

SETUP
─────
1. Install dependencies:
      pip install playwright requests ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key:
      export CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing (optional):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `disney_ap_config.json` (example at bottom).
   Fill in:
   - Disney account credentials.
   - Desired pass type (e.g., "Incredi-Pass", "Sorcerer Pass").
   - Payment and shipping details.
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
BOT_ID   = "disney_ap_bot"
BOT_NAME = "Disney AP Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "disney_ap_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "disney_ap_state.json")

SCAN_INTERVAL      = 30    # seconds between checks for AP sales page
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
    default = {
        "disney_account": {
            "email": "your_disney@email.com",
            "password": "your_password"
        },
        "pass_type": "Disney Incredi-Pass",   # e.g., "Disney Incredi-Pass", "Disney Sorcerer Pass"
        "guest": {
            "first_name": "Mickey",
            "last_name": "Mouse",
            "birthdate": "1990-01-01",
            "address": "123 Main St",
            "city": "Orlando",
            "state": "FL",
            "zip": "32830",
            "phone": "4075551234"
        },
        "payment": {
            "card_number": "4111111111111111",
            "card_expiry": "12/28",
            "card_cvv": "123",
            "card_name": "Mickey Mouse",
            "billing_zip": "32830"
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
        "ebay": {
            "enabled": False,
            "markup_multiplier": 1.5,
            "listing_type": "activation"   # or "account"
        }
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
        return {"purchased": False}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# Proxy rotation, CAPTCHA solving, eBay listing (reuse standard implementation)
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

def list_on_ebay(item_name, price):
    if not HAS_EBAY or not CFG["ebay"].get("enabled"):
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
    ebay_price = round(price * markup, 2)
    listing_type = CFG["ebay"].get("listing_type", "activation")
    title = f"Disney World {item_name} – {listing_type.capitalize()} Slot – Guaranteed"
    desc = f"Authentic {item_name} {listing_type}. Will transfer immediately upon purchase."
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": desc,
            "PrimaryCategory": {"CategoryID": "170067"},  # Tickets & Experiences
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


# ── Main AP Purchase Flow ──────────────────────────────────────────────────────
def attempt_ap_purchase():
    """Monitor the AP sales page, handle queue, select pass, and checkout."""
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed.", "error")
        return False

    account = CFG["disney_account"]
    pass_type = CFG["pass_type"]
    guest = CFG["guest"]
    payment = CFG["payment"]

    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy:
        launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Open the main Annual Pass page (often redirects to a queue)
            ap_url = "https://disneyworld.disney.go.com/passes/"
            _post("Opening Annual Pass page...", "info")
            page.goto(ap_url, wait_until="domcontentloaded", timeout=30000)

            # 2. Handle queue / waiting room
            start_time = datetime.utcnow()
            while True:
                # If we are on a queue page (URL contains 'queue' or page shows "Please wait")
                if "queue" in page.url.lower() or page.locator("text=Please Wait").count():
                    _post("In queue... waiting.", "info")
                    time.sleep(5)
                    page.reload(wait_until="domcontentloaded")
                    continue

                # Check if we see "Add to Cart" or "Purchase" button
                purchase_btn = page.locator("a:has-text('Purchase'), button:has-text('Purchase')")
                if purchase_btn.is_visible():
                    _post("AP sales page is active!", "info")
                    break

                # If we see "Current Pass Sales Paused" or "Not Currently on Sale", we are out of luck
                if page.locator("text=Not Currently on Sale").count() or \
                   page.locator("text=Sales Paused").count():
                    _post("AP sales are currently paused. Waiting to check again...", "info")
                    return False

                # Timeout after 10 minutes
                if (datetime.utcnow() - start_time).seconds > 600:
                    _post("Timeout waiting for AP sales to open.", "warning")
                    return False
                time.sleep(2)

            # 3. If not logged in, log in now
            if page.locator("text=Sign In").is_visible():
                _post("Logging in...", "info")
                page.click("text=Sign In")
                page.wait_for_load_state("networkidle")
                page.fill("input#email", account["email"])
                page.fill("input#password", account["password"])
                page.click("button[type='submit']")
                page.wait_for_load_state("networkidle")
                if "verify" in page.url.lower() or "two-step" in page.content().lower():
                    _post("2FA required. Cannot automate.", "error")
                    return False

            # 4. Select pass type (if multiple options)
            pass_selector = f"label:has-text('{pass_type}') input, div:has-text('{pass_type}') input"
            pass_radio = page.locator(pass_selector)
            if pass_radio.count():
                pass_radio.check()
                _post(f"Selected {pass_type}.", "info")

            # 5. Click "Add to Cart" / "Continue"
            add_btn = page.locator("button:has-text('Add to Cart'), button:has-text('Continue')")
            if add_btn.count():
                add_btn.click()
                page.wait_for_load_state("networkidle")

            # 6. Guest information
            _post("Filling guest details...", "info")
            page.fill("input#firstName", guest["first_name"])
            page.fill("input#lastName", guest["last_name"])
            page.fill("input#birthDate", guest["birthdate"])
            page.fill("input#address1", guest["address"])
            page.fill("input#city", guest["city"])
            page.select_option("select#state", guest["state"])
            page.fill("input#zipCode", guest["zip"])
            page.fill("input#phone", guest["phone"])

            # 7. Payment
            if page.locator("input#cardNumber").is_visible():
                _post("Entering payment details...", "info")
                page.fill("input#cardNumber", payment["card_number"])
                page.fill("input#expiry", payment["card_expiry"])
                page.fill("input#cvv", payment["card_cvv"])
                page.fill("input#billingZipCode", payment["billing_zip"])

            # 8. CAPTCHA
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                if not solve_captcha(page):
                    return False
                time.sleep(2)

            # 9. Place Order
            order_btn = page.locator("button:has-text('Place Order')")
            if order_btn.is_visible():
                order_btn.click()
                _post("Order submitted.", "info")
            else:
                _post("Cannot find Place Order button.", "error")
                return False

            page.wait_for_timeout(8000)
            if "thank you" in page.content().lower() or "confirmation" in page.content().lower():
                _post(f"🎉 {pass_type} purchase successful!", "info")
                return True
            else:
                _post("Purchase may have failed.", "warning")
                return False
        except Exception as e:
            _post(f"AP purchase error: {e}", "error")
            return False
        finally:
            browser.close()

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    state = load_state()
    _post("Disney AP Bot online. Monitoring for Annual Pass sales...", "info")

    while True:
        if state.get("purchased"):
            _post("AP already purchased. Stopping.", "info")
            break
        success = attempt_ap_purchase()
        if success:
            state["purchased"] = True
            save_state(state)
            if CFG["ebay"].get("enabled"):
                price = 800.0  # approximate price for Incredi-Pass; configurable
                list_on_ebay(CFG["pass_type"], price)
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `disney_ap_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "disney_account": {
    "email": "your_disney@email.com",
    "password": "your_password"
  },
  "pass_type": "Disney Incredi-Pass",
  "guest": {
    "first_name": "Mickey",
    "last_name": "Mouse",
    "birthdate": "1990-01-01",
    "address": "123 Main St",
    "city": "Orlando",
    "state": "FL",
    "zip": "32830",
    "phone": "4075551234"
  },
  "payment": {
    "card_number": "4111111111111111",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "card_name": "Mickey Mouse",
    "billing_zip": "32830"
  },
  "proxies": {"list": []},
  "captcha": {"api_key": "YOUR_2CAPTCHA_KEY"},
  "ebay": {
    "enabled": false,
    "markup_multiplier": 1.5,
    "listing_type": "activation"
  }
}
"""
