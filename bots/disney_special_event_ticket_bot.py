#!/usr/bin/env python3
"""
disney_special_event_ticket_bot.py — Disney After Hours / Special Event Ticket Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors Disney World/Disneyland special event pages for ticket drops.
2. Auto‑logs into your Disney account.
3. Selects the desired event, date, and ticket type.
4. Completes checkout with stored payment (via Playwright).
5. Optionally lists tickets on eBay at 2‑3× face value.

✦ FOR EDUCATIONAL / RESEARCH PURPOSES ONLY.
  Automated purchasing violates Disney’s Terms of Service and
  may result in permanent bans and order cancellations.

SETUP
─────
1. Install dependencies:
      pip install playwright requests ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key:  export CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing (optional):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `disney_event_ticket_config.json` (example at bottom).
   Fill in:
   - Disney account credentials.
   - List of target events with event page URLs and desired dates.
   - Payment details.
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
BOT_ID   = "disney_special_event_ticket_bot"
BOT_NAME = "Disney Special Event Ticket Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "disney_event_ticket_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "disney_event_ticket_state.json")

SCAN_INTERVAL      = 30    # seconds between availability checks
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

# ── Config & State ────────────────────────────────────────────────────────────
def load_config():
    default = {
        "disney_account": {
            "email": "your_disney_account@email.com",
            "password": "your_password"
        },
        "events": [
            {
                "name": "Mickey's Very Merry Christmas Party",
                "url": "https://disneyworld.disney.go.com/tickets/events/mickeys-very-merry-christmas-party/",
                "dates": ["2026-11-15", "2026-11-20"],  # dates to check
                "ticket_type": "Adult",                # or "Child"
                "party_size": 4,
                "face_value": 149.0                    # approximate retail price for listing
            },
            {
                "name": "Disney Villains After Hours",
                "url": "https://disneyworld.disney.go.com/tickets/events/disney-villains-after-hours/",
                "dates": ["2026-06-01", "2026-06-08"],
                "ticket_type": "Adult",
                "party_size": 2,
                "face_value": 145.0
            }
        ],
        "payment": {
            "card_number": "4111111111111111",
            "card_expiry": "12/28",
            "card_cvv": "123",
            "card_name": "Your Name",
            "billing_zip": "32830"
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
        "ebay": {
            "enabled": False,
            "markup_multiplier": 2.5,
            "auto_list": False
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
        return {"purchased_tickets": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()

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

# ── Captcha solver (2Captcha) ─────────────────────────────────────────────────
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

# ── eBay listing ──────────────────────────────────────────────────────────────
def list_on_ebay(event_name, date, price):
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
    ebay_price = round(price * markup, 2)
    title = f"Disney {event_name} Ticket {date} – INSTANT DELIVERY"
    description = f"Guaranteed authentic {event_name} ticket for {date}. Will transfer immediately."
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": description,
            "PrimaryCategory": {"CategoryID": "170067"},   # Tickets & Experiences > Theme Park & Club Passes
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
        response = trading.execute("AddFixedPriceItem", payload)
        if response.dict().get("Ack") == "Success":
            _post(f"eBay listing created: {response.dict()['ItemID']} at ${ebay_price}", "info")
    except Exception as e:
        _post(f"eBay listing error: {e}", "error")

# ── Main purchase flow ────────────────────────────────────────────────────────
def attempt_purchase_for_event(event_cfg):
    """Try to purchase tickets for a specific event. Returns True if successful."""
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed.", "error")
        return False

    account = CFG["disney_account"]
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
            # 1. Open event page
            url = event_cfg["url"]
            _post(f"Navigating to {url}", "info")
            page.goto(url, wait_until="networkidle", timeout=30000)

            # 2. Handle queue (if present, similar to AP bot)
            start_time = datetime.utcnow()
            while True:
                if "queue" in page.url.lower() or page.locator("text=Please Wait").count():
                    _post("In queue... waiting.", "info")
                    time.sleep(5)
                    page.reload(wait_until="domcontentloaded")
                    continue
                # Check for purchase button/combo
                buy_btn = page.locator("a:has-text('Purchase'), button:has-text('Purchase')")
                if buy_btn.is_visible():
                    _post("Event ticket page active.", "info")
                    break
                if page.locator("text=Coming Soon").count() or page.locator("text=Not Yet Available").count():
                    _post("Tickets not yet on sale.", "info")
                    return False
                if (datetime.utcnow() - start_time).seconds > 600:
                    _post("Timed out waiting for ticket sales.", "warning")
                    return False
                time.sleep(2)

            # 3. Login if needed
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

            # 4. Select date (calendar widget)
            for date_str in event_cfg.get("dates", []):
                _post(f"Trying to select date {date_str}...", "info")
                # Click on date cell – Disney uses a calendar; we look for a day number
                day = str(int(date_str.split("-")[2]))  # remove leading zero
                # May need to navigate month if not visible
                # Just attempt to click
                date_cell = page.locator(f"td[data-date='{date_str}'], td[aria-label*='{date_str}']")
                if date_cell.count() == 0:
                    # Fallback: find the day number in the calendar
                    date_cell = page.locator(f"td:has-text('{day}')")
                if date_cell.count() and date_cell.is_visible():
                    date_cell.click()
                    _post(f"Selected date {date_str}.", "info")
                    break
                else:
                    _post(f"Date {date_str} not clickable.", "warning")
                    continue
            else:
                _post("Could not select any of the target dates.", "warning")
                return False

            # 5. Select ticket type (Adult/Child) and quantity
            ticket_type = event_cfg.get("ticket_type", "Adult")
            party_size = event_cfg.get("party_size", 2)
            # Use labels or dropdowns
            type_radio = page.locator(f"label:has-text('{ticket_type}') input")
            if type_radio.count():
                type_radio.check()
            # Quantity
            qty_input = page.locator("input[type='number'], select.quantity")
            if qty_input.count():
                qty_input.fill(str(party_size))
            # Continue
            continue_btn = page.locator("button:has-text('Continue')")
            if continue_btn.is_visible():
                continue_btn.click()
                page.wait_for_load_state("networkidle")

            # 6. Fill payment details (if not already stored)
            _post("Filling payment details...", "info")
            page.fill("input#cardNumber", payment["card_number"])
            page.fill("input#expiry", payment["card_expiry"])
            page.fill("input#cvv", payment["card_cvv"])
            page.fill("input#billingZipCode", payment["billing_zip"])

            # 7. CAPTCHA?
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                if not solve_captcha(page):
                    return False
                time.sleep(2)

            # 8. Place order
            order_btn = page.locator("button:has-text('Place Order')")
            if order_btn.is_visible():
                order_btn.click()
                _post("Order submitted.", "info")
            else:
                _post("Cannot find Place Order button.", "error")
                return False

            page.wait_for_timeout(8000)
            if "thank you" in page.content().lower() or "confirmation" in page.content().lower():
                _post(f"🎉 {event_cfg['name']} ticket purchase successful!", "info")
                return True
            else:
                _post("Purchase may have failed.", "warning")
                return False
        except Exception as e:
            _post(f"Purchase error: {e}", "error")
            return False
        finally:
            browser.close()

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    _post("Disney Special Event Ticket Bot online.", "info")

    events = CFG.get("events", [])
    while True:
        for event in events:
            # Skip if already purchased a ticket for this event within the last day
            event_name = event["name"]
            recent = any(
                t["event"] == event_name and 
                (datetime.utcnow() - datetime.fromisoformat(t["timestamp"])) < timedelta(hours=24)
                for t in STATE.get("purchased_tickets", [])
            )
            if recent:
                _post(f"Skipping {event_name} – already purchased recently.", "info")
                continue

            success = attempt_purchase_for_event(event)
            if success:
                # Record purchase
                STATE.setdefault("purchased_tickets", []).append({
                    "event": event_name,
                    "timestamp": datetime.utcnow().isoformat(),
                    "face_value": event.get("face_value", 150.0)
                })
                save_state(STATE)

                # List on eBay
                if CFG["ebay"].get("auto_list"):
                    # Use first date as the listing date
                    date = event.get("dates", ["unknown"])[0]
                    list_on_ebay(event["name"], date, event.get("face_value", 150.0))

            # Small delay between different events to avoid detection
            time.sleep(random.uniform(5, 15))

        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `disney_event_ticket_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "disney_account": {
    "email": "your_disney_account@email.com",
    "password": "your_password"
  },
  "events": [
    {
      "name": "Mickey's Very Merry Christmas Party",
      "url": "https://disneyworld.disney.go.com/tickets/events/mickeys-very-merry-christmas-party/",
      "dates": ["2026-11-15", "2026-11-20"],
      "ticket_type": "Adult",
      "party_size": 4,
      "face_value": 149.0
    }
  ],
  "payment": {
    "card_number": "4111111111111111",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "card_name": "Your Name",
    "billing_zip": "32830"
  },
  "proxies": {"list": []},
  "captcha": {"api_key": "YOUR_2CAPTCHA_KEY"},
  "ebay": {
    "enabled": true,
    "markup_multiplier": 2.5,
    "auto_list": true
  }
}
"""
