#!/usr/bin/env python3
"""
disney_popcorn_bucket_bot.py — Disney Parks Limited Popcorn Bucket Auto‑Buy & Resell Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors Disney mobile‑order pages for limited popcorn buckets.
2. Auto‑logs into Disney account, adds bucket to cart, checks out.
3. Rotates proxies and solves CAPTCHAs with 2Captcha.
4. Lists purchased buckets on eBay at a configurable markup.

✦ FOR EDUCATIONAL / RESEARCH PURPOSES ONLY.
  Automated purchasing may violate Disney’s Terms of Service.

SETUP
─────
1. Install dependencies:
      pip install playwright requests beautifulsoup4 ebaysdk
      python -m playwright install chromium

2. Get a 2Captcha API key (https://2captcha.com/) → export CAPTCHA_API_KEY

3. Get eBay Trading API credentials (optional for auto‑listing):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `popcorn_bucket_config.json` (example at bottom).
   Fill in:
   - Disney account credentials.
   - Target bucket name (e.g., “Figment Popcorn Bucket”).
   - Mobile‑order URL and CSS selectors for the item and checkout.
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

# ── BotController connection ─────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "disney_popcorn_bucket_bot"
BOT_NAME = "Disney Popcorn Bucket Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "popcorn_bucket_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "popcorn_bucket_state.json")

SCAN_INTERVAL      = 30    # seconds between checks
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

# ── Configuration ─────────────────────────────────────────────────────────────
def load_config():
    default = {
        "target_bucket": "Figment Popcorn Bucket",
        "disney_account": {
            "email": "your_disney_account@email.com",
            "password": "your_password"
        },
        "mobile_order": {
            # Example Disneyland mobile order URL (find your specific restaurant)
            "restaurant_url": "https://disneyland.disney.go.com/dining/disneyland/popcorn-cart/",
            "item_selector": "div.menu-item:has-text('Figment')",   # CSS for the bucket item
            "add_to_cart_selector": "button.add-to-order",
            "checkout_button": "button.continue-to-checkout",
            "pickup_window_selector": "select.pickup-time"
        },
        "checkout_profile": {
            "first_name": "Mickey",
            "last_name": "Mouse",
            "address": "1313 Disneyland Dr",
            "city": "Anaheim",
            "state": "CA",
            "zip": "92802",
            "card_number": "4111111111111111",
            "card_expiry": "12/28",
            "card_cvv": "123",
            "phone": "7147814636"
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
        "ebay": {
            "enabled": True,
            "markup_multiplier": 3.0,
            "auto_list": True,
            "listing_duration_days": 30
        }
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

CFG = load_config()

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"purchased_buckets": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Proxy rotation ────────────────────────────────────────────────────────────
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

# ── Disney Mobile Order Purchase (Playwright) ─────────────────────────────────
def attempt_disney_purchase():
    """
    Simulates the Disney mobile order flow:
    - Logs into Disney account
    - Navigates to the restaurant page
    - Locates the target popcorn bucket and adds it to cart
    - Completes checkout with stored payment
    Returns True if successful.
    """
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed.", "error")
        return False

    account = CFG["disney_account"]
    checkout = CFG["checkout_profile"]
    target = CFG["target_bucket"]
    order_cfg = CFG["mobile_order"]

    proxy = next_proxy()
    launch_options = {
        "headless": False,
        "viewport": {"width": 375, "height": 812},  # iPhone X
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
    }
    if proxy:
        launch_options["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_options)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Go to Disneyland / Disney World mobile order base URL
            _post("Opening Disney mobile order site...", "info")
            # Many mobile orders start from the app, but we can try the direct restaurant URL
            page.goto(order_cfg["restaurant_url"], wait_until="networkidle", timeout=30000)

            # 2. If not logged in, sign in
            if page.locator("text=Sign In").is_visible():
                _post("Logging into Disney account...", "info")
                page.click("text=Sign In")
                page.wait_for_load_state("networkidle")
                page.fill("input#email", account["email"])
                page.fill("input#password", account["password"])
                page.click("button[type='submit']")
                page.wait_for_load_state("networkidle")
                # Possibly handle 2FA?
                if "verify" in page.url.lower() or "two-step" in page.content().lower():
                    _post("2FA required – cannot automate.", "error")
                    return False

            # 3. Look for the target popcorn bucket in the menu
            _post(f"Searching for '{target}' on the menu...", "info")
            item_selector = order_cfg.get("item_selector", f"div.menu-item:has-text('{target}')")
            bucket_item = page.locator(item_selector)
            if not bucket_item.is_visible():
                _post("Bucket not found on the menu – maybe sold out or not listed.", "warning")
                return False

            # 4. Add to cart
            bucket_item.click()
            time.sleep(1)
            add_btn = order_cfg.get("add_to_cart_selector", "button.add-to-order")
            page.click(add_btn)
            _post("Bucket added to cart.", "info")

            # 5. Proceed to checkout
            page.click(order_cfg.get("checkout_button", "button.continue-to-checkout"))
            page.wait_for_load_state("networkidle")

            # 6. Fill contact / payment if not already on file
            _fill_disney_checkout(page, checkout)

            # 7. CAPTCHA?
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                if not solve_captcha(page):
                    return False
                page.wait_for_timeout(2000)

            # 8. Pick a pickup window if needed
            pickup_sel = order_cfg.get("pickup_window_selector")
            if pickup_sel and page.locator(pickup_sel).count():
                page.select_option(pickup_sel, index=0)  # earliest

            # 9. Place order
            page.click("button:has-text('Place Order')")
            page.wait_for_timeout(8000)

            if "Order Confirmed" in page.content() or "Thank you" in page.content():
                _post("🎉 Disney popcorn bucket order placed!", "info")
                return True
            else:
                _post("Order may not have completed.", "warning")
                return False
        except Exception as e:
            _post(f"Disney purchase error: {e}", "error")
            return False
        finally:
            browser.close()

def _fill_disney_checkout(page, checkout):
    """Fill generic checkout fields, if they appear."""
    try:
        page.fill("input[name='firstName']", checkout["first_name"])
        page.fill("input[name='lastName']", checkout["last_name"])
        page.fill("input[name='addressLine1']", checkout["address"])
        page.fill("input[name='city']", checkout["city"])
        page.select_option("select[name='state']", checkout["state"])
        page.fill("input[name='zipCode']", checkout["zip"])
        page.fill("input[name='phone']", checkout["phone"])

        # Credit card
        page.fill("input[name='cardNumber']", checkout["card_number"])
        page.fill("input[name='expiry']", checkout["card_expiry"])
        page.fill("input[name='cvv']", checkout["card_cvv"])
    except Exception:
        pass  # fields may already be stored

# ── eBay listing ──────────────────────────────────────────────────────────────
def list_on_ebay(bucket_name, purchase_price):
    if not HAS_EBAY:
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
    title = f"Disney Parks {bucket_name} – In Hand Ready to Ship"
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": f"Brand new {bucket_name} from Disney Parks. Limited availability, in hand and ready to ship immediately.",
            "PrimaryCategory": {"CategoryID": "13877"},  # Collectibles > Disneyana > Contemporary (1968-Now)
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

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    state = load_state()

    _post("Disney Popcorn Bucket Bot online. Monitoring mobile order availability...", "info")

    while True:
        # Prevent duplicate purchases within 24 hours for the same bucket
        last = state.get("purchased_buckets", [])
        recent = any(
            (datetime.utcnow() - datetime.fromisoformat(p["timestamp"])) < timedelta(hours=24)
            for p in last if p["bucket"] == CFG["target_bucket"]
        )
        if recent:
            _post("Already purchased a similar bucket within 24h – skipping.", "info")
        else:
            success = attempt_disney_purchase()
            if success:
                # Record purchase
                state.setdefault("purchased_buckets", []).append({
                    "bucket": CFG["target_bucket"],
                    "timestamp": datetime.utcnow().isoformat(),
                    "price": 20.0  # approximate retail; adjust as needed
                })
                save_state(state)

                # Auto-list on eBay
                if CFG["ebay"].get("auto_list"):
                    list_on_ebay(CFG["target_bucket"], 20.0)

        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `popcorn_bucket_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "target_bucket": "Figment Popcorn Bucket",
  "disney_account": {
    "email": "your_disney_account@email.com",
    "password": "your_password"
  },
  "mobile_order": {
    "restaurant_url": "https://disneyland.disney.go.com/dining/disneyland/popcorn-cart/",
    "item_selector": "div.menu-item:has-text('Figment')",
    "add_to_cart_selector": "button.add-to-order",
    "checkout_button": "button.continue-to-checkout",
    "pickup_window_selector": "select.pickup-time"
  },
  "checkout_profile": {
    "first_name": "Mickey",
    "last_name": "Mouse",
    "address": "1313 Disneyland Dr",
    "city": "Anaheim",
    "state": "CA",
    "zip": "92802",
    "card_number": "4111111111111111",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "phone": "7147814636"
  },
  "proxies": { "list": [] },
  "captcha": { "api_key": "YOUR_2CAPTCHA_KEY" },
  "ebay": {
    "enabled": true,
    "markup_multiplier": 3.0,
    "auto_list": true,
    "listing_duration_days": 30
  }
}
"""
