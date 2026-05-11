#!/usr/bin/env python3
"""
disney_adr_bot.py — Disney Dining Reservation Auto‑Book & Resell Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Continuously monitors My Disney Experience for hard‑to‑get ADRs.
2. Auto‑logs into your Disney account.
3. Checks availability for specified restaurants, dates, times, party size.
4. If a spot opens, instantly books it.
5. Optionally lists the reservation on eBay (or marks it for transfer).

✦ FOR EDUCATIONAL / RESEARCH PURPOSES ONLY.
  Automated booking violates Disney’s Terms of Service and
  may lead to permanent bans and cancellation of reservations.

SETUP
─────
1. Install dependencies:
      pip install playwright requests ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key:
      export CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing (optional):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `disney_adr_config.json` (example at bottom).
   Fill in:
   - Disney account credentials.
   - List of target restaurants, date ranges, meal periods, party sizes.
   - Payment details for holding the reservation (if required).
   - Proxy list (optional).

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
BOT_ID   = "disney_adr_bot"
BOT_NAME = "Disney ADR Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "disney_adr_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "disney_adr_state.json")

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

# ── Config ─────────────────────────────────────────────────────────────────────
def load_config():
    default = {
        "disney_account": {
            "email": "your_disney_account@email.com",
            "password": "your_password"
        },
        "targets": [
            {
                "restaurant": "Cinderella's Royal Table",
                "location": "Magic Kingdom",
                "meal_period": "dinner",          # breakfast, lunch, dinner
                "date_range_start": "2026-06-01",
                "date_range_end": "2026-06-10",
                "party_size": 4,
                "preferred_times": ["6:00 PM", "7:00 PM", "5:30 PM"]
            },
            {
                "restaurant": "Space 220",
                "location": "EPCOT",
                "meal_period": "lunch",
                "date_range_start": "2026-06-01",
                "date_range_end": "2026-06-10",
                "party_size": 2,
                "preferred_times": ["12:00 PM", "1:00 PM"]
            },
            {
                "restaurant": "Oga's Cantina",
                "location": "Disney's Hollywood Studios",
                "meal_period": "dinner",
                "date_range_start": "2026-06-01",
                "date_range_end": "2026-06-10",
                "party_size": 4,
                "preferred_times": ["7:00 PM", "8:00 PM"]
            }
        ],
        "booking": {
            "payment": {
                "card_number": "4111111111111111",
                "card_expiry": "12/28",
                "card_cvv": "123",
                "card_name": "Your Name",
                "billing_zip": "32830"
            },
            "contact": {
                "phone": "4075551234"
            }
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
        "ebay": {
            "enabled": False,
            "markup_multiplier": 1.5,
            "auto_list": False
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
        return {"booked_reservations": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# Proxy rotation, CAPTCHA solving (same as previous bots)
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
    # same 2Captcha implementation as previous bots
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

# ── Disney ADR Checker & Booker ────────────────────────────────────────────────
def attempt_adr_booking():
    """
    Logs into Disney account, loops through targets, searches for availability,
    and books the first available slot it finds.
    Returns (restaurant_name, date, time) if booked, else None.
    """
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed.", "error")
        return None

    account = CFG["disney_account"]
    targets = CFG.get("targets", [])
    if not targets:
        _post("No ADR targets configured.", "info")
        return None

    booking_cfg = CFG.get("booking", {})
    payment = booking_cfg.get("payment", {})
    contact = booking_cfg.get("contact", {})

    proxy = next_proxy()
    launch_opts = {"headless": False}  # headed helps avoid bot detection
    if proxy:
        launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        context = browser.new_context()
        page = context.new_page()

        try:
            # ── Login ──
            _post("Navigating to Disney login...", "info")
            page.goto("https://disneyworld.disney.go.com/login", wait_until="networkidle", timeout=30000)
            # Accept cookies if present
            try:
                page.click("button:has-text('Accept All Cookies')", timeout=2000)
            except: pass

            page.fill("input#email", account["email"])
            page.fill("input#password", account["password"])
            page.click("button[type='submit']")
            page.wait_for_load_state("networkidle")

            # Handle possible 2FA (SMS/code) – if required, cannot automate easily; report
            if "verify" in page.url.lower() or "two-step" in page.content().lower():
                _post("2FA required. Cannot proceed automatically.", "error")
                return None

            # ── Loop over targets ──
            for target in targets:
                restaurant = target["restaurant"]
                meal = target.get("meal_period", "dinner")
                party = target.get("party_size", 2)
                times = target.get("preferred_times", [])

                # Calculate dates to check (today + next few days within range)
                start_date = datetime.strptime(target["date_range_start"], "%Y-%m-%d").date()
                end_date = datetime.strptime(target["date_range_end"], "%Y-%m-%d").date()
                today = datetime.utcnow().date()
                check_dates = [d for d in _date_range(start_date, end_date) if d >= today]
                if not check_dates:
                    continue

                # For each date, we'll attempt to search
                for check_date in check_dates:
                    date_str = check_date.strftime("%Y-%m-%d")
                    _post(f"Searching {restaurant} on {date_str}...", "info")

                    # Build dining reservation URL (Disney's search)
                    # Usually: https://disneyworld.disney.go.com/dining/...
                    # We'll use the search endpoint directly by constructing GET parameters
                    search_url = (
                        f"https://disneyworld.disney.go.com/dining/{restaurant.replace(' ', '-').lower()}/"
                        f"reservation-search?date={date_str}&time=800&partySize={party}"
                    )
                    page.goto(search_url, wait_until="networkidle", timeout=30000)

                    # The page may display available time slots.
                    # We look for buttons that contain preferred times.
                    for preferred_time in times:
                        # Time format on page: "6:00 PM"
                        slot_btn = page.locator(f"button:has-text('{preferred_time}')")
                        if slot_btn.count() and slot_btn.is_visible():
                            _post(f"Found {restaurant} at {preferred_time} on {date_str}! Booking...", "error")
                            # Click the time slot
                            slot_btn.first.click()
                            page.wait_for_timeout(1000)

                            # Proceed to review/reserve page
                            # Usually a "Continue" button
                            continue_btn = page.locator("button:has-text('Continue')")
                            if continue_btn.is_visible():
                                continue_btn.click()
                                page.wait_for_load_state("networkidle")

                            # Fill in any required info (guest names, phone, etc.)
                            # Guest names often pre-filled from profile; we'll just check for phone
                            try:
                                phone_field = page.locator("input[name='phone'], input#phone")
                                if phone_field.is_visible():
                                    phone_field.fill(contact.get("phone", ""))
                            except: pass

                            # Payment guarantee (some ADRs require credit card)
                            # Look for card fields
                            if page.locator("input[name='cardNumber']").is_visible():
                                page.fill("input[name='cardNumber']", payment["card_number"])
                                page.fill("input[name='expiry']", payment["card_expiry"])
                                page.fill("input[name='cvv']", payment["card_cvv"])
                                page.fill("input[name='zipCode']", payment["billing_zip"])

                            # Final confirm button
                            confirm_btn = page.locator("button:has-text('Confirm Reservation'), button:has-text('Book Now')")
                            if confirm_btn.is_visible():
                                confirm_btn.click()
                                page.wait_for_timeout(5000)

                                if "confirmation" in page.url.lower() or "thank you" in page.content().lower():
                                    _post(f"🎉 Booked {restaurant} on {date_str} at {preferred_time}!", "info")
                                    return (restaurant, date_str, preferred_time)
                            break  # Stop checking further times for this date

                # If no slot found for any date, continue to next target
                time.sleep(random.uniform(2, 4))  # delays to avoid bot patterns

            _post("No availability found for any target.", "info")
            return None
        except Exception as e:
            _post(f"ADR booking error: {e}", "error")
            return None
        finally:
            browser.close()

def _date_range(start, end):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)

# ── Resell listing (eBay) ─────────────────────────────────────────────────────
def list_adr_on_ebay(restaurant, date, time_slot, party_size):
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
    mark_up = CFG["ebay"]["markup_multiplier"]
    base_price = 50.0  # hard-to-get ADR starting price, could be dynamic
    ebay_price = round(base_price * mark_up, 2)
    title = f"Disney {restaurant} ADR {date} {time_slot} – Party of {party_size}"
    desc = f"Guaranteed Advance Dining Reservation for {restaurant} on {date} at {time_slot} for {party_size} guests."
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": desc,
            "PrimaryCategory": {"CategoryID": "170067"},  # Tickets & Experiences > Theme Park Passes
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
            item_id = response.dict()["ItemID"]
            _post(f"ADR listed on eBay: {item_id} at ${ebay_price}", "info")
    except Exception as e:
        _post(f"eBay listing error: {e}", "error")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    state = load_state()
    _post("Disney ADR Bot online — monitoring for hard‑to‑get reservations.", "info")

    while True:
        # Don't run if already booked the same day (prevent duplicate charges)
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        already_booked = any(
            r.get("date") == today_str
            for r in state.get("booked_reservations", [])
        )
        if already_booked:
            _post("Already booked today. Skipping scan.", "info")
        else:
            result = attempt_adr_booking()
            if result:
                restaurant, date, time_slot = result
                # Record booking
                state.setdefault("booked_reservations", []).append({
                    "restaurant": restaurant,
                    "date": date,
                    "time": time_slot,
                    "timestamp": datetime.utcnow().isoformat()
                })
                save_state(state)

                # Optionally list on eBay
                if CFG["ebay"].get("auto_list"):
                    target = next((t for t in CFG["targets"] if t["restaurant"] == restaurant), None)
                    party = target["party_size"] if target else 2
                    list_adr_on_ebay(restaurant, date, time_slot, party)

        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `disney_adr_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "disney_account": {
    "email": "your_disney_account@email.com",
    "password": "your_password"
  },
  "targets": [
    {
      "restaurant": "Cinderella's Royal Table",
      "location": "Magic Kingdom",
      "meal_period": "dinner",
      "date_range_start": "2026-06-01",
      "date_range_end": "2026-06-10",
      "party_size": 4,
      "preferred_times": ["6:00 PM", "7:00 PM", "5:30 PM"]
    }
  ],
  "booking": {
    "payment": {
      "card_number": "4111111111111111",
      "card_expiry": "12/28",
      "card_cvv": "123",
      "card_name": "Your Name",
      "billing_zip": "32830"
    },
    "contact": {
      "phone": "4075551234"
    }
  },
  "proxies": {"list": []},
  "captcha": {"api_key": "YOUR_2CAPTCHA_KEY"},
  "ebay": {
    "enabled": false,
    "markup_multiplier": 1.5,
    "auto_list": false
  }
}
"""
