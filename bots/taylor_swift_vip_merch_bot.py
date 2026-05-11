#!/usr/bin/env python3
"""
taylor_swift_vip_merch_bot.py — Taylor Swift Eras Tour VIP Merch Auto‑Buy & Resell Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors official tour pop‑up shop for VIP boxes.
2. Auto‑logs in, adds VIP box to cart, checks out via Shopify.
3. Rotates proxies and solves CAPTCHAs with 2Captcha.
4. Lists purchased VIP boxes on eBay at 3× markup.

✦ FOR RESEARCH / EDUCATIONAL USE ONLY.
  Automated checkout may violate the store’s Terms of Service.

SETUP
─────
1. Install dependencies:
      pip install playwright requests ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key:  export CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing (optional):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `vip_merch_config.json` (example at bottom).
   Fill in:
   - Product URL for the VIP box.
   - Shopify store domain (if different from the product URL).
   - Your email/account credentials (if needed).
   - Shipping & payment details.

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

# ── BotController connection ──────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "taylor_swift_vip_merch_bot"
BOT_NAME = "Taylor Swift VIP Merch Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "vip_merch_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "vip_merch_state.json")

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

# ── Config ─────────────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {
            "product": {
                "url": "https://store.taylorswift.com/products/eras-tour-vip-box",
                "name": "Eras Tour VIP Box",
                "price": 150.0,                # retail price (for eBay listing)
                "quantity": 1
            },
            "shopify": {
                "domain": "store.taylorswift.com",
                "add_to_cart_selector": "button[name='add']",
                "sold_out_selector": "button[disabled][name='add']",
                "checkout_url": "/checkout"
            },
            "customer": {
                "email": "you@example.com",
                "first_name": "Taylor",
                "last_name": "Swift",
                "address": "13 Management Street",
                "city": "Nashville",
                "state": "TN",
                "zip": "37201",
                "phone": "5551231313",
                "card_number": "4111111111111111",
                "card_expiry": "12/28",
                "card_cvv": "123",
                "card_name": "Taylor Swift"
            },
            "proxies": {"list": []},
            "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
            "ebay": {
                "enabled": True,
                "markup_multiplier": 3.0,
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

# ── Shopify checkout (Playwright) ──────────────────────────────────────────────
def attempt_purchase():
    product = CFG["product"]
    shop = CFG.get("shopify", {})
    customer = CFG.get("customer", {})

    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy:
        launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Navigate to product page
            _post(f"Navigating to {product['url']}", "info")
            page.goto(product["url"], wait_until="networkidle", timeout=30000)

            # 2. Check stock (Add to Cart button enabled and no sold-out)
            add_sel = shop.get("add_to_cart_selector", "button[name='add']")
            sold_out_sel = shop.get("sold_out_selector", "button[disabled][name='add']")

            if page.locator(sold_out_sel).count() > 0:
                _post("VIP box is sold out.", "info")
                return False

            add_btn = page.locator(add_sel)
            if not add_btn.is_visible() or not add_btn.is_enabled():
                _post("Add to Cart button not available.", "warning")
                return False

            # 3. Add to cart
            add_btn.click()
            _post("Added to cart.", "info")

            # 4. Go to checkout (Shopify's /checkout path)
            checkout_url = product["url"].rsplit("/", 1)[0].replace("/products", "") + shop.get("checkout_url", "/checkout")
            page.goto(checkout_url, wait_until="networkidle", timeout=30000)

            # 5. Login (if needed – Shopify may already have customer info via cookies, but we can fill)
            # We'll fill contact info
            _fill_shopify_contact(page, customer)

            # 6. Continue to shipping
            page.click("button:has-text('Continue to shipping')")
            page.wait_for_load_state("networkidle")

            # 7. Fill shipping address (if not pre-filled)
            _fill_shopify_shipping(page, customer)

            # 8. Continue to payment
            page.click("button:has-text('Continue to payment')")
            page.wait_for_load_state("networkidle")

            # 9. Fill credit card (often inside iframes)
            page.wait_for_timeout(3000)
            _fill_shopify_payment(page, customer)

            # 10. Handle CAPTCHA
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                if not solve_captcha(page):
                    return False
                page.wait_for_timeout(2000)

            # 11. Complete order
            order_btn = "button:has-text('Complete order'), button:has-text('Pay now'), button#checkout-pay-button"
            page.click(order_btn)
            _post("Order submitted.", "info")
            page.wait_for_timeout(10000)

            if "thank you" in page.content().lower() or "order confirmed" in page.content().lower():
                _post("🎉 VIP box purchase successful!", "info")
                return True
            else:
                _post("Order could not be verified.", "warning")
                return False
        except Exception as e:
            _post(f"Checkout error: {e}", "error")
            return False
        finally:
            browser.close()

def _fill_shopify_contact(page, cust):
    try:
        page.fill("input#checkout_email, input[name='checkout[email]']", cust["email"])
        page.fill("input#checkout_shipping_address_first_name, input[name='checkout[shipping_address][first_name]']",
                  cust["first_name"])
        page.fill("input#checkout_shipping_address_last_name, input[name='checkout[shipping_address][last_name]']",
                  cust["last_name"])
    except Exception:
        pass

def _fill_shopify_shipping(page, cust):
    try:
        page.fill("input#checkout_shipping_address_address1, input[name='checkout[shipping_address][address1]']",
                  cust["address"])
        page.fill("input#checkout_shipping_address_city, input[name='checkout[shipping_address][city]']",
                  cust["city"])
        page.select_option("select#checkout_shipping_address_country, select[name='checkout[shipping_address][country]']",
                           "US")
        page.wait_for_timeout(1000)
        page.fill("input#checkout_shipping_address_zip, input[name='checkout[shipping_address][zip]']",
                  cust["zip"])
        if cust.get("state"):
            page.select_option("select#checkout_shipping_address_province, select[name='checkout[shipping_address][province]']",
                               cust["state"])
        page.fill("input#checkout_shipping_address_phone, input[name='checkout[shipping_address][phone]']",
                  cust["phone"])
    except Exception:
        pass

def _fill_shopify_payment(page, cust):
    # Try iframe approach (common for card number)
    try:
        card_iframe = page.frame_locator("iframe.card-number-iframe, iframe[title='Secure card number input frame']")
        card_iframe.locator("input[name='number']").fill(cust["card_number"])
    except Exception:
        # Fallback direct input (rare)
        page.fill("input#number, input[name='number']", cust["card_number"])

    try:
        exp_iframe = page.frame_locator("iframe.card-expiry-iframe, iframe[title='Secure expiration date input frame']")
        exp_iframe.locator("input[name='expiry']").fill(cust["card_expiry"])
    except Exception:
        page.fill("input#expiry, input[name='expiry']", cust["card_expiry"])

    try:
        cvv_iframe = page.frame_locator("iframe.card-cvc-iframe, iframe[title='Secure CVC input frame']")
        cvv_iframe.locator("input[name='verification_value']").fill(cust["card_cvv"])
    except Exception:
        page.fill("input#verification_value, input[name='verification_value']", cust["card_cvv"])

    # Name on card
    try:
        page.fill("input#checkout_credit_card_name, input[name='checkout[credit_card][name]']",
                  cust["card_name"])
    except Exception:
        pass

# ── eBay listing ───────────────────────────────────────────────────────────────
def list_on_ebay(item_name, retail_price):
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
    ebay_price = round(retail_price * markup, 2)
    title = f"Taylor Swift Eras Tour VIP Box – UNOPENED – Ready to Ship"
    description = f"Brand new, unopened {item_name} from the Eras Tour online pop‑up shop. Ships immediately."
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": description,
            "PrimaryCategory": {"CategoryID": "13758"},  # Entertainment Memorabilia > Music Memorabilia
            "StartPrice": ebay_price,
            "Quantity": 1,
            "ListingDuration": "Days_30",
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

    _post("Taylor Swift VIP Merch Bot online. Monitoring store...", "info")

    while True:
        # Prevent duplicate purchases within 24 hours
        recent = any(
            (datetime.utcnow() - datetime.fromisoformat(p["timestamp"])) < timedelta(hours=24)
            for p in state.get("purchased", []) if p.get("url") == CFG["product"]["url"]
        )
        if recent:
            _post("Already purchased a VIP box within 24h. Skipping.", "info")
        else:
            success = attempt_purchase()
            if success:
                state.setdefault("purchased", []).append({
                    "url": CFG["product"]["url"],
                    "timestamp": datetime.utcnow().isoformat(),
                    "price": CFG["product"]["price"]
                })
                save_state(state)

                if CFG["ebay"].get("auto_list"):
                    list_on_ebay(CFG["product"]["name"], CFG["product"]["price"])

        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `vip_merch_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "product": {
    "url": "https://store.taylorswift.com/products/eras-tour-vip-box",
    "name": "Eras Tour VIP Box",
    "price": 150.0,
    "quantity": 1
  },
  "shopify": {
    "domain": "store.taylorswift.com",
    "add_to_cart_selector": "button[name='add']",
    "sold_out_selector": "button[disabled][name='add']",
    "checkout_url": "/checkout"
  },
  "customer": {
    "email": "you@example.com",
    "first_name": "Taylor",
    "last_name": "Swift",
    "address": "13 Management Street",
    "city": "Nashville",
    "state": "TN",
    "zip": "37201",
    "phone": "5551231313",
    "card_number": "4111111111111111",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "card_name": "Taylor Swift"
  },
  "proxies": { "list": [] },
  "captcha": { "api_key": "YOUR_2CAPTCHA_KEY" },
  "ebay": {
    "enabled": true,
    "markup_multiplier": 3.0,
    "auto_list": true
  }
}
"""
