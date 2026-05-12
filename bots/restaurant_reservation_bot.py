#!/usr/bin/env python3
"""
restaurant_reservation_bot.py — Prime‑Time Restaurant Reservation Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors restaurant booking platforms (OpenTable, Resy, etc.).
2. Searches for hard‑to‑get tables at desired times.
3. Automatically books the reservation for a client.
4. Collects a fee via Stripe and optionally lists the reservation on eBay.

✦ REAL‑WORLD INTEGRATIONS: Playwright, 2Captcha, Stripe, eBay, Google Calendar.
  Requires configuration for the specific booking site.
  For educational and research purposes only.

SETUP
─────
1. Install dependencies:
      pip install playwright requests stripe ebaysdk
      python -m playwright install chromium

2. Configure API keys:
      export CAPTCHA_API_KEY="your-2captcha-key"
      export STRIPE_SECRET_KEY="sk_test_..."
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN
      (optional) Set up Google service account if using calendar.

3. Create `restaurant_config.json` (example at bottom).
   Define booking sites, locations, date/time preferences, client info,
   and payment details.

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
from typing import List, Dict, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

try:
    from ebaysdk.trading import Connection as eBayTrading
    HAS_EBAY = True
except ImportError:
    HAS_EBAY = False

# ═══════════════════════════════════════════════════════════════════════════
# Hub connection (BotController)
HUB      = "http://localhost:8765"
BOT_ID   = "restaurant_reservation_bot"
BOT_NAME = "Restaurant Reservation Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restaurant_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restaurant_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 60   # seconds between reservation attempts

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
        "booking_sites": [
            {
                "platform": "opentable",
                "base_url": "https://www.opentable.com",
                "search_url": "https://www.opentable.com/s?dateTime={date}T{time}&covers={party}&restaurantName={restaurant}",
                "selectors": {
                    "available_slot": "button[data-test='time-slot']",
                    "slot_time": "span.time",
                    "confirm_button": "button[data-test='confirm-reservation']",
                    "guest_first_name": "input#firstName",
                    "guest_last_name": "input#lastName",
                    "guest_email": "input#email",
                    "guest_phone": "input#phone",
                    "special_requests": "textarea#specialRequests",
                    "complete_booking": "button[data-test='complete-booking']"
                },
                "authentication": {
                    "login_required": False,
                    "email_selector": "",
                    "password_selector": "",
                    "login_submit": ""
                }
            },
            {
                "platform": "resy",
                "base_url": "https://resy.com",
                "search_url": "https://resy.com/cities/nyc/{restaurantSlug}?date={date}&seats={party}",
                "selectors": {
                    "available_slot": "button.available-time",
                    "first_name": "input[name='first_name']",
                    "last_name": "input[name='last_name']",
                    "email": "input[name='email']",
                    "phone": "input[name='phone']",
                    "book_button": "button.submit-booking"
                }
            }
        ],
        "clients": [
            {
                "client_name": "John Smith",
                "email": "john@example.com",
                "phone": "5551234567",
                "first_name": "John",
                "last_name": "Smith"
            }
        ],
        "requested_reservations": [
            {
                "restaurant": "Gramercy Tavern (NYC)",
                "platform": "opentable",
                "date": "2026-06-15",
                "time": "19:00",
                "party_size": 4,
                "preferred_times": ["19:00", "19:30", "20:00"]
            },
            {
                "restaurant": "Carbone (NYC)",
                "platform": "resy",
                "restaurant_slug": "carbone",
                "date": "2026-06-15",
                "time": "20:00",
                "party_size": 2
            }
        ],
        "resale": {
            "ebay": {
                "enabled": False,
                "markup_multiplier": 2.0
            },
            "booking_fee_usd": 50.0
        },
        "stripe": {
            "enabled": False,
            "secret_key": os.getenv("STRIPE_SECRET_KEY", "")
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
        return {"booked_reservations": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()

# ═══════════════════════════════════════════════════════════════════════════
# Proxy & Captcha (same as previous bots)
_proxies = []
_proxy_idx = 0

def next_proxy():
    global _proxy_idx
    if not _proxies:
        return None
    with _lock:
        p = _proxies[_proxy_idx % len(_proxies)]
        _proxy_idx += 1
        return p

CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")

def solve_captcha(page, sitekey=None):
    if not CAPTCHA_API_KEY:
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
            "key": CAPTCHA_API_KEY, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": page.url, "json": 1
        }, timeout=15).json()
        if resp.get("status") != 1:
            return False
        cid = resp["request"]
        for i in range(30):
            time.sleep(5)
            r = requests.get("http://2captcha.com/res.php", params={
                "key": CAPTCHA_API_KEY, "action": "get", "id": cid, "json": 1
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
# Stripe fee collection
def charge_booking_fee(client_email, fee_usd, reservation_id):
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return True  # skip payment
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(round(fee_usd * 100)),
            currency="usd",
            description=f"Restaurant Reservation – {reservation_id}",
            metadata={"client_email": client_email}
        )
        _post(f"Stripe PaymentIntent: {intent.client_secret}", "info")
        # For simplicity, we assume payment is completed externally (or you could check status)
        return True
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return False

# ═══════════════════════════════════════════════════════════════════════════
# eBay listing for reservations
def list_on_ebay(restaurant, date_time, party_size, confirmation_code, price):
    if not HAS_EBAY or not CFG["resale"]["ebay"]["enabled"]:
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
    ebay_price = round(price * markup, 2)
    title = f"Prime-Time Reservation at {restaurant} on {date_time} (Party of {party_size})"
    description = f"Guaranteed reservation for {party_size} at {restaurant} on {date_time}. Will transfer to your name. Confirmation: {confirmation_code}"
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
# Booking Engine
def book_reservation(site_cfg, target, client):
    """
    Navigate booking flow for a given platform.
    Return confirmation code/ID or None.
    """
    platform = site_cfg["platform"]
    selectors = site_cfg.get("selectors", {})
    base_url = site_cfg.get("base_url", "")

    # Construct search URL if provided, else we expect a direct link
    search_url = site_cfg.get("search_url", "")
    if search_url:
        # Replace placeholders
        search_url = search_url.replace("{date}", target["date"]).replace("{time}", target.get("time", "19:00"))
        search_url = search_url.replace("{party}", str(target.get("party_size", 2)))
        search_url = search_url.replace("{restaurant}", target["restaurant"].replace(" ", "%20"))
        if "restaurantSlug" in target:
            search_url = search_url.replace("{restaurantSlug}", target["restaurant_slug"])
    else:
        search_url = target.get("url", "")

    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy:
        launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            _post(f"Opening {search_url}", "info")
            page.goto(search_url, wait_until="networkidle", timeout=30000)

            # Login if needed (check site config for authentication)
            if site_cfg.get("authentication", {}).get("login_required"):
                login_sel = site_cfg["authentication"]
                page.fill(login_sel.get("email_selector", "input#email"), client.get("email"))
                page.fill(login_sel.get("password_selector", "input#password"), "password")  # stored elsewhere
                page.click(login_sel.get("login_submit", "button[type='submit']"))
                page.wait_for_load_state("networkidle")

            # Select available time slot (the first one if multiple preferred)
            slot_btn_sel = selectors.get("available_slot", "button.available-time")
            preferred_times = target.get("preferred_times", [target["time"]])
            slot_found = False
            for t in preferred_times:
                # Try to find a slot matching the time
                slots = page.locator(slot_btn_sel)
                for i in range(slots.count()):
                    slot = slots.nth(i)
                    # May need to check text/time
                    if t in slot.inner_text() or slot.get_attribute("data-time") == t:
                        slot.click()
                        slot_found = True
                        break
                if slot_found:
                    break
            if not slot_found:
                # Just click the first available slot
                if page.locator(slot_btn_sel).count():
                    page.locator(slot_btn_sel).first.click()
                else:
                    _post("No time slots available.", "warning")
                    return None

            # Fill guest details
            if selectors.get("guest_first_name"):
                page.fill(selectors["guest_first_name"], client.get("first_name", ""))
            if selectors.get("guest_last_name"):
                page.fill(selectors["guest_last_name"], client.get("last_name", ""))
            if selectors.get("guest_email"):
                page.fill(selectors["guest_email"], client.get("email"))
            if selectors.get("guest_phone"):
                page.fill(selectors["guest_phone"], client.get("phone"))
            if selectors.get("special_requests"):
                page.fill(selectors["special_requests"], target.get("special_requests", ""))

            # Confirm / Reserve
            confirm_sel = selectors.get("confirm_button", "button:has-text('Confirm')")
            if page.locator(confirm_sel).is_visible():
                page.click(confirm_sel)
                page.wait_for_timeout(1000)

            # Handle possible CAPTCHA
            if page.locator("[data-sitekey]").count() or page.locator("iframe[title*='captcha']").count():
                if not solve_captcha(page):
                    _post("Captcha solve failed.", "error")
                    return None
                time.sleep(2)
                # Retry confirmation after captcha
                if page.locator(confirm_sel).is_visible():
                    page.click(confirm_sel)

            # Final submit
            complete_sel = selectors.get("complete_booking", "button:has-text('Complete')")
            if page.locator(complete_sel).is_visible():
                page.click(complete_sel)
                page.wait_for_timeout(5000)

            # Check for confirmation
            content = page.content().lower()
            if "confirmation" in content or "reserved" in content or "thank you" in content:
                # Extract confirmation ID if possible
                conf = re.search(r'(?:confirmation\s*#?|reservation\s*#?)\s*:?\s*([A-Z0-9]+)', content, re.I)
                conf_id = conf.group(1) if conf else "booked"
                _post(f"Reservation secured for {client['client_name']}! Conf: {conf_id}", "info")
                return conf_id
            else:
                _post("Could not verify booking confirmation.", "warning")
                return None
        except Exception as e:
            _post(f"Booking error: {e}", "error")
            return None
        finally:
            browser.close()

# ═══════════════════════════════════════════════════════════════════════════
# Main processing loop
def process_reservations():
    state = load_state()
    reservations = CFG.get("requested_reservations", [])
    clients = CFG.get("clients", [])

    for res in reservations:
        # Skip if already booked (check restaurant + date)
        key = f"{res['restaurant']}_{res['date']}_{res['time']}"
        if any(r.get("key") == key for r in state.get("booked_reservations", [])):
            continue

        # Find a client (could match by email or use a pool)
        client = clients[0] if clients else {}
        if not client:
            _post("No client available for booking.", "warning")
            continue

        # Find site config matching platform
        platform = res.get("platform", "opentable")
        site_cfg = next((s for s in CFG.get("booking_sites", []) if s["platform"] == platform), None)
        if not site_cfg:
            _post(f"No site config for platform '{platform}'", "error")
            continue

        # Attempt booking
        conf_code = book_reservation(site_cfg, res, client)
        if conf_code:
            fee = CFG["resale"]["booking_fee_usd"]
            # Charge fee
            if charge_booking_fee(client["email"], fee, conf_code):
                # Record successful booking
                state.setdefault("booked_reservations", []).append({
                    "key": key,
                    "restaurant": res["restaurant"],
                    "date_time": f"{res['date']} {res['time']}",
                    "party": res.get("party_size", 2),
                    "confirmation": conf_code,
                    "client": client["client_name"],
                    "fee": fee,
                    "timestamp": datetime.utcnow().isoformat()
                })
                save_state(state)
                # List on eBay if enabled
                if CFG["resale"]["ebay"]["enabled"]:
                    list_on_ebay(
                        res["restaurant"],
                        f"{res['date']} {res['time']}",
                        res.get("party_size", 2),
                        conf_code,
                        fee
                    )
            else:
                # Payment failed but booking succeeded?
                _post("Booking succeeded but payment failed.", "warning")
                state.setdefault("booked_reservations", []).append({
                    "key": key,
                    "restaurant": res["restaurant"],
                    "confirmation": conf_code,
                    "client": client["client_name"],
                    "fee": 0,
                    "timestamp": datetime.utcnow().isoformat()
                })
                save_state(state)

def main():
    wait_for_hub()
    _post("Restaurant Reservation Bot online. Searching for prime‑time tables...", "info")
    while True:
        process_reservations()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `restaurant_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "booking_sites": [
    {
      "platform": "opentable",
      "search_url": "https://www.opentable.com/s?dateTime={date}T{time}&covers={party}&restaurantName={restaurant}",
      "selectors": {
        "available_slot": "button[data-test='time-slot']",
        "confirm_button": "button[data-test='confirm-reservation']",
        "guest_first_name": "input#firstName",
        "guest_last_name": "input#lastName",
        "guest_email": "input#email",
        "guest_phone": "input#phone",
        "complete_booking": "button[data-test='complete-booking']"
      }
    },
    {
      "platform": "resy",
      "search_url": "https://resy.com/cities/nyc/{restaurantSlug}?date={date}&seats={party}",
      "selectors": {
        "available_slot": "button.available-time",
        "first_name": "input[name='first_name']",
        "last_name": "input[name='last_name']",
        "email": "input[name='email']",
        "phone": "input[name='phone']",
        "book_button": "button.submit-booking"
      }
    }
  ],
  "clients": [
    {"client_name": "John Smith", "email": "john@example.com", "phone": "5551234567",
     "first_name": "John", "last_name": "Smith"}
  ],
  "requested_reservations": [
    {
      "restaurant": "Gramercy Tavern",
      "platform": "opentable",
      "date": "2026-06-15",
      "time": "19:00",
      "party_size": 4,
      "preferred_times": ["19:00", "19:30"]
    }
  ],
  "resale": {
    "ebay": {"enabled": false, "markup_multiplier": 2.0},
    "booking_fee_usd": 50
  },
  "stripe": {"enabled": false, "secret_key": "sk_test_..."}
}
"""
