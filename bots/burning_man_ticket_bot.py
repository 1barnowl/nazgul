#!/usr/bin/env python3
"""
burning_man_ticket_bot.py — Burning Man Ticket Harvester & Reseller
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors the Burning Man ticket sale page for the main sale.
2. Enters the queue, waits, and auto‑completes checkout the moment
   it becomes available.
3. After purchase, lists the ticket(s) on StubHub (or eBay) at a
   configurable markup.

✦ REAL‑WORLD AUTOMATION: Playwright, 2Captcha, Stripe (optional),
  StubHub Seller API or eBay Trading API.
  For educational / research purposes only. Automated ticket purchasing
  violates Burning Man’s terms and may result in permanent bans.

SETUP
─────
1. Install dependencies:
      pip install playwright requests stripe ebaysdk
      python -m playwright install chromium

   For StubHub selling:
      No official Python SDK, but you can use the HTTP API (provided below).
      You’ll need a StubHub seller token (see stubhub.com/developers).

2. Set environment variables:
      export CAPTCHA_API_KEY="your-2captcha-key"
      export STUBHUB_APP_TOKEN="your-app-token"   # For StubHub API
      export STUBHUB_APP_SECRET="your-app-secret"
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN  (if using eBay)

3. Create `burning_man_config.json` (example at bottom).
   - Provide your Burning Man account (if needed).
   - Payment details (credit card).
   - Number of tickets and price maximum.
   - Desired sale URL (may change yearly – check official page).
   - Resale platform preference and markup multiplier.

4. Attach to BotController.
"""

import json
import os
import re
import time
import uuid
import random
import threading
import requests
from datetime import datetime, timedelta
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

try:
    from ebaysdk.trading import Connection as eBayTrading
    HAS_EBAY = True
except ImportError:
    HAS_EBAY = False

# ═══════════════════════════════════════════════════════════════════════════
# BotController hub
HUB      = "http://localhost:8765"
BOT_ID   = "burning_man_ticket_bot"
BOT_NAME = "Burning Man Ticket Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "burning_man_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "burning_man_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 10   # check very frequently during sale

_last_hb = 0.0
_lock = threading.Lock()

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
    with _lock:
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

def wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).ok:
                return
        except Exception:
            pass
        time.sleep(1)

# ═══════════════════════════════════════════════════════════════════════════
# Config & State
def load_config():
    default = {
        "ticket": {
            "sale_url": "https://tickets.burningman.org/",
            "num_tickets": 2,
            "max_price_per_ticket": 575.0,   # main sale price + fees
            "ticket_type": "Main Sale"       # could be "FOMO" etc.
        },
        "buyer_account": {
            "email": "your@email.com",
            "password": "your_bm_account_password"
        },
        "payment": {
            "card_number": "4111111111111111",
            "card_expiry": "12/28",
            "card_cvv": "123",
            "cardholder_name": "John Doe",
            "billing_address": "123 Playa Way",
            "billing_city": "Black Rock City",
            "billing_state": "NV",
            "billing_zip": "89412",
            "country": "US"
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", "")},
        "resale": {
            "platform": "stubhub",          # or "ebay"
            "markup_multiplier": 3.0,       # sell for 3x face value
            "stubhub": {
                "app_token": os.getenv("STUBHUB_APP_TOKEN", ""),
                "app_secret": os.getenv("STUBHUB_APP_SECRET", ""),
                "listing_event_id": None,   # needed to create listing (search event API)
                "listing_quantity": 1
            },
            "ebay": {
                "enabled": False,
                "markup_multiplier": 3.0
            }
        }
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"purchased": False, "orders": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()

# ═══════════════════════════════════════════════════════════════════════════
# Proxy & Captcha utilities
_proxies = CFG.get("proxies", {}).get("list", [])
_proxy_idx = 0

def next_proxy():
    global _proxy_idx
    if not _proxies:
        return None
    with _lock:
        p = _proxies[_proxy_idx % len(_proxies)]
        _proxy_idx += 1
        return p

CAPTCHA_KEY = CFG.get("captcha", {}).get("api_key", "")

def solve_captcha(page, sitekey=None):
    if not CAPTCHA_KEY:
        _post("No 2Captcha API key.", "error")
        return False
    try:
        if not sitekey:
            el = page.locator("[data-sitekey]")
            if el.count():
                sitekey = el.get_attribute("data-sitekey")
            else:
                return False
        resp = requests.get("http://2captcha.com/in.php", params={
            "key": CAPTCHA_KEY, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": page.url, "json": 1
        }, timeout=15).json()
        if resp.get("status") != 1:
            return False
        cid = resp["request"]
        for i in range(30):
            time.sleep(5)
            r = requests.get("http://2captcha.com/res.php", params={
                "key": CAPTCHA_KEY, "action": "get", "id": cid, "json": 1
            }).json()
            if r.get("status") == 1:
                token = r["request"]
                page.evaluate(f"""
                    document.getElementById('g-recaptcha-response').innerHTML = '{token}';
                    if (typeof ___grecaptcha_cfg !== 'undefined') {{
                        Object.keys(___grecaptcha_cfg.clients).forEach(id =>
                            ___grecaptcha_cfg.clients[id].W.O.callback(token));
                    }}
                """)
                return True
            if r.get("request") != "CAPCHA_NOT_READY":
                break
    except:
        pass
    return False

# ═══════════════════════════════════════════════════════════════════════════
# Burning Man ticket purchase (Playwright automation)
def purchase_tickets():
    """
    Navigate the Burning Man sale page, handle queue/ waiting room,
    select tickets, fill details, pay, and return order confirmation if successful.
    """
    ticket_cfg = CFG["ticket"]
    buyer = CFG["buyer_account"]
    payment = CFG["payment"]
    url = ticket_cfg["sale_url"]

    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy:
        launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            # 1. Open the sale page
            _post(f"Opening {url} ...", "info")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 2. Handle queue/waiting room (Burning Man uses Queue‑it or similar)
            # We'll loop until the "Buy Tickets" button appears or we timeout.
            start_time = time.time()
            while time.time() - start_time < 300:  # 5 minutes
                if page.url.startswith("https://queue."):
                    _post("In queue, waiting...", "info")
                    time.sleep(5)
                    page.reload(wait_until="domcontentloaded")
                    continue
                # Look for a known buy button or ticket selection
                if page.locator("a:has-text('Buy Tickets'), button:has-text('Purchase')").count() > 0:
                    _post("Queue passed! Sale page loaded.", "info")
                    break
                # Check if "Sold Out" appears
                if page.locator("text=Sold Out").count() > 0:
                    _post("Tickets are sold out.", "error")
                    return None
                time.sleep(2)
            else:
                _post("Queue timeout.", "warning")
                return None

            # 3. Login if needed (some sales require account)
            if page.locator("input#loginEmail, input[name='email']").count() > 0:
                _post("Logging in...", "info")
                page.fill("input#loginEmail, input[name='email']", buyer["email"])
                page.fill("input#loginPassword, input[name='password']", buyer["password"])
                page.click("button[type='submit'], button:has-text('Sign In')")
                page.wait_for_load_state("networkidle")

            # 4. Select ticket type and quantity
            ticket_type = ticket_cfg.get("ticket_type", "Main Sale")
            # Common selection: radio button or dropdown
            type_radio = page.locator(f"label:has-text('{ticket_type}') input")
            if type_radio.count():
                type_radio.check()
                _post(f"Selected ticket type: {ticket_type}", "info")
            qty_select = page.locator("select.quantity, input[type='number']")
            if qty_select.count():
                qty_select.fill(str(ticket_cfg["num_tickets"]))
            # Click "Add to Cart" / "Proceed"
            add_btn = page.locator("button:has-text('Add to Cart'), button:has-text('Proceed')")
            if add_btn.is_visible():
                add_btn.click()
                page.wait_for_load_state("networkidle")

            # 5. Fill payment details (Burning Man often uses a payment form)
            _post("Filling payment details...", "info")
            # The following selectors are generic; adjust based on the actual site
            page.fill("input#firstName", buyer["first_name"])
            page.fill("input#lastName", buyer["last_name"])
            page.fill("input#email", buyer["email"])
            page.fill("input#cardNumber", payment["card_number"])
            page.fill("input#cardExpiry", payment["card_expiry"])
            page.fill("input#cardCvv", payment["card_cvv"])
            page.fill("input#cardHolderName", payment["cardholder_name"])
            page.fill("input#billingAddress", payment["billing_address"])
            page.fill("input#billingCity", payment["billing_city"])
            page.select_option("select#billingState", payment["billing_state"])
            page.fill("input#billingZip", payment["billing_zip"])
            page.select_option("select#country", payment["country"])

            # 6. CAPTCHA (often present before final submission)
            if page.locator("[data-sitekey]").count() or page.locator("iframe[title*='captcha']").count():
                if not solve_captcha(page):
                    return None
                time.sleep(2)

            # 7. Place order
            order_btn = page.locator("button:has-text('Place Order'), button:has-text('Complete Purchase')")
            if order_btn.is_visible():
                order_btn.click()
                _post("Order submitted!", "info")
            else:
                _post("Cannot find Place Order button.", "error")
                return None

            page.wait_for_timeout(8000)
            # 8. Verify confirmation
            content = page.content().lower()
            if "thank you" in content or "order confirmed" in content:
                conf_id = re.search(r'(?:order|confirmation)\s*(?:#|:)\s*([A-Z0-9-]+)', content)
                conf = conf_id.group(1) if conf_id else "UNKNOWN"
                _post(f"🎉 Tickets purchased! Confirmation: {conf}", "info")
                return conf
            else:
                _post("Could not confirm purchase.", "warning")
                return None
        except Exception as e:
            _post(f"Purchase error: {e}", "error")
            return None
        finally:
            browser.close()

# ═══════════════════════════════════════════════════════════════════════════
# StubHub listing (HTTP API)
def list_on_stubhub(confirmation_code, face_value, quantity):
    """Create a listing on StubHub using their API."""
    app_token = CFG["resale"]["stubhub"].get("app_token", "")
    app_secret = CFG["resale"]["stubhub"].get("app_secret", "")
    if not app_token or not app_secret:
        _post("StubHub API credentials missing. Skipping listing.", "warning")
        return

    # Step 1: Get a token
    auth_url = "https://api.stubhub.com/login"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "password", "username": app_token, "password": app_secret,
            "scope": "PRODUCTION"}
    try:
        r = requests.post(auth_url, headers=headers, data=data)
        if r.status_code != 200:
            _post(f"StubHub auth failed: {r.text}", "error")
            return
        token = r.json().get("access_token")
    except Exception as e:
        _post(f"StubHub auth error: {e}", "error")
        return

    # Step 2: Create listing for a specific event (we need an event_id)
    event_id = CFG["resale"]["stubhub"]["listing_event_id"]
    if not event_id:
        # You must obtain the event ID from the StubHub catalog API first
        _post("No StubHub event ID set. Cannot create listing.", "error")
        return

    markup = CFG["resale"]["markup_multiplier"]
    price = round(face_value * markup, 2)

    listing_data = {
        "eventId": event_id,
        "quantity": quantity,
        "price": price,
        "seatNumbers": "General Admission",
        "deliveryMethod": "Electronic",
        "externalListingId": confirmation_code,
        "listingSource": "External"
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.post("https://api.stubhub.com/sellers/listings/v2",
                             json=listing_data, headers=headers)
        if resp.status_code == 201:
            _post(f"StubHub listing created: ${price} x {quantity} tickets", "info")
        else:
            _post(f"StubHub listing failed: {resp.text}", "error")
    except Exception as e:
        _post(f"StubHub error: {e}", "error")

# ═══════════════════════════════════════════════════════════════════════════
# eBay listing (fallback)
def list_on_ebay(confirmation_code, face_value):
    if not HAS_EBAY or not CFG["resale"]["ebay"].get("enabled", False):
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
    markup = CFG["resale"]["ebay"]["markup_multiplier"]
    ebay_price = round(face_value * markup, 2)
    title = f"Burning Man Main Sale Ticket – Guaranteed – In Hand"
    description = f"You are buying a guaranteed Burning Man ticket purchased during the main sale. Will transfer immediately upon receipt."
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": description,
            "PrimaryCategory": {"CategoryID": "170067"},  # Tickets & Experiences
            "StartPrice": ebay_price,
            "Quantity": 1,
            "ListingDuration": "Days_30",
            "Country": "US",
            "Currency": "USD",
            "ListingType": "FixedPriceItem",
            "Site": "US"
        }
    }
    try:
        resp = trading.execute("AddFixedPriceItem", payload)
        if resp.dict().get("Ack") == "Success":
            _post(f"eBay listing created: {resp.dict()['ItemID']} at ${ebay_price}", "info")
    except Exception as e:
        _post(f"eBay error: {e}", "error")

# ═══════════════════════════════════════════════════════════════════════════
# Main orchestration
def run():
    state = load_state()
    if state.get("purchased"):
        _post("Tickets already purchased. Skipping.", "info")
        return

    conf = purchase_tickets()
    if conf:
        state["purchased"] = True
        state.setdefault("orders", []).append({
            "confirmation": conf,
            "timestamp": datetime.utcnow().isoformat(),
            "face_value": CFG["ticket"]["max_price_per_ticket"]
        })
        save_state(state)

        # Resale listing
        platform = CFG["resale"]["platform"]
        if platform == "stubhub":
            list_on_stubhub(conf, CFG["ticket"]["max_price_per_ticket"],
                           CFG["ticket"]["num_tickets"])
        elif platform == "ebay":
            list_on_ebay(conf, CFG["ticket"]["max_price_per_ticket"])
        else:
            _post(f"Unknown resale platform: {platform}", "warning")

def main():
    wait_for_hub()
    _post("Burning Man Ticket Harvester Bot online. Polling for main sale...", "info")
    while True:
        run()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `burning_man_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "ticket": {
    "sale_url": "https://tickets.burningman.org/",
    "num_tickets": 2,
    "max_price_per_ticket": 575.0,
    "ticket_type": "Main Sale"
  },
  "buyer_account": {
    "email": "your@email.com",
    "password": "your_password"
  },
  "payment": {
    "card_number": "4111111111111111",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "cardholder_name": "John Doe",
    "billing_address": "123 Playa Way",
    "billing_city": "Black Rock City",
    "billing_state": "NV",
    "billing_zip": "89412",
    "country": "US"
  },
  "proxies": {"list": []},
  "captcha": {"api_key": "YOUR_2CAPTCHA_KEY"},
  "resale": {
    "platform": "stubhub",
    "markup_multiplier": 3.0,
    "stubhub": {
      "app_token": "YOUR_STUBHUB_APP_TOKEN",
      "app_secret": "YOUR_STUBHUB_APP_SECRET",
      "listing_event_id": 123456789
    },
    "ebay": {
      "enabled": false,
      "markup_multiplier": 3.0
    }
  }
}
"""
