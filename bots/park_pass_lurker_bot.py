#!/usr/bin/env python3
"""
park_pass_lurker_bot.py — Park Reservation Availability Lurker Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Continuously monitors Disney park pass reservations for sold‑out dates.
2. Detects when a cancellation opens a slot and instantly books it.
3. Optionally lists the park admission slot on eBay at a 2‑3× markup.

✦ FOR EDUCATIONAL / RESEARCH PURPOSES ONLY.
  Automated reservation booking violates Disney’s Terms of Service
  and can lead to permanent bans and loss of tickets/accounts.

SETUP
─────
1. Install dependencies:
      pip install playwright requests ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key:  export CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing (optional):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `park_pass_lurker_config.json` (example at bottom).
   Provide:
   - Disney account credentials.
   - List of parks and dates to watch.
   - Payment details.

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
BOT_ID   = "park_pass_lurker_bot"
BOT_NAME = "Park Pass Lurker"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "park_pass_lurker_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "park_pass_lurker_state.json")

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
        "parks": [
            {
                "name": "Magic Kingdom",
                "date": "2026-06-15",
                "party_size": 4,
                "face_value": 0   # park passes are often free with ticket; selling "access slot"
            },
            {
                "name": "EPCOT",
                "date": "2026-06-15",
                "party_size": 2
            }
        ],
        "booking": {
            "preferred_times": ["Morning", "Afternoon"],  # if applicable
            "contact": {
                "phone": "4075551234"
            }
        },
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
            "markup_multiplier": 2.0,
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
        return {"booked_parks": []}
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

# ── eBay listing (if enabled) ────────────────────────────────────────────────
def list_on_ebay(park, date, party_size):
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
    base_price = 25.0   # arbitrary – park passes themselves are free, but the slot is valuable
    ebay_price = round(base_price * markup * party_size, 2)
    title = f"Disney {park} Park Pass {date} – Guaranteed"
    desc = f"Guaranteed park pass reservation for {park} on {date} for {party_size} guests. Will transfer immediately."
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": desc,
            "PrimaryCategory": {"CategoryID": "170067"},
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
        _post(f"eBay error: {e}", "error")

# ── Main lurker loop ───────────────────────────────────────────────────────────
def attempt_booking():
    """Log into Disney, loop through configured parks/dates, check availability, book if open."""
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed.", "error")
        return

    account = CFG["disney_account"]
    parks = CFG.get("parks", [])
    if not parks:
        _post("No park/date combos to check.", "info")
        return

    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy:
        launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            # 1. Go to park pass reservation page (often "My Disney Experience" -> "Park Pass")
            _post("Navigating to Disney Park Pass page...", "info")
            page.goto("https://disneyworld.disney.go.com/park-reservations/", wait_until="networkidle", timeout=30000)

            # Handle cookie consent
            try:
                page.click("button:has-text('Accept All Cookies')", timeout=2000)
            except:
                pass

            # 2. Sign in if needed
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
                    return

            # 3. Iterate over desired parks and dates
            for park in parks:
                park_name = park["name"]
                date_str = park["date"]
                party = park.get("party_size", 2)

                # Skip if already booked for this park/date combo in last 24h
                already = [
                    p for p in STATE.get("booked_parks", [])
                    if p["park"] == park_name and p["date"] == date_str and
                       (datetime.utcnow() - datetime.fromisoformat(p["timestamp"])) < timedelta(hours=24)
                ]
                if already:
                    continue

                _post(f"Checking {park_name} for {date_str}...", "info")

                # Navigate to the reservation selection flow.
                # Disney typically loads a calendar/interactive interface.
                # We'll refresh the park pass page and select the park from a dropdown.
                # (Exact selectors may change; these are typical as of recent Disney site.)
                # Step A: Click "Make a Reservation" button
                make_res_btn = page.locator("a:has-text('Make a Reservation'), button:has-text('Make a Reservation')")
                if make_res_btn.count():
                    make_res_btn.click()
                    page.wait_for_load_state("networkidle")

                # Step B: Select park (if not already selected)
                park_select = page.locator("select#park, select[aria-label='Select a Park']")
                if park_select.count():
                    park_select.select_option(label=park_name)
                    time.sleep(2)

                # Step C: Navigate calendar to find the date – this is complex.
                # We'll assume the page shows a calendar and we try to click the day.
                day = str(int(date_str.split("-")[2]))  # remove leading zero
                # Use a generic selector for the day button (often a span or link)
                date_cell = page.locator(f"td[data-date='{date_str}'], button:has-text('{day}')")
                if date_cell.count() == 0:
                    # Fallback: try to find any clickable element with the date text
                    date_cell = page.locator(f"*:has-text('{day}')")
                if date_cell.is_visible():
                    date_cell.first.click()
                    _post(f"Clicked date {date_str}.", "info")
                else:
                    _post(f"Could not find clickable date for {date_str}.", "warning")
                    continue

                time.sleep(2)

                # Step D: Choose time window if applicable
                preferred_times = CFG.get("booking", {}).get("preferred_times", [])
                time_selected = False
                for t in preferred_times:
                    slot = page.locator(f"button:has-text('{t}'), input[value='{t}']")
                    if slot.is_visible():
                        slot.click()
                        time_selected = True
                        break
                if not time_selected:
                    # Try to click any "Select" button for the first available time
                    any_time = page.locator("button:has-text('Select')")
                    if any_time.is_visible():
                        any_time.first.click()

                # Step E: Continue / "Add to Cart" or "Next"
                continue_btn = page.locator("button:has-text('Continue'), button:has-text('Next')")
                if continue_btn.is_visible():
                    continue_btn.click()
                    page.wait_for_load_state("networkidle")

                # Step F: Sometimes it asks for party size / guest selection
                # If there is a dropdown for number of guests, set it
                guest_select = page.locator("select.party-size, select[aria-label='Number of Guests']")
                if guest_select.count():
                    guest_select.select_option(str(party))

                # Step G: Finalise payment (usually not required, but may require card for no-show guarantee)
                payment_fields = CFG.get("payment", {})
                if page.locator("input#cardNumber").is_visible():
                    page.fill("input#cardNumber", payment_fields["card_number"])
                    page.fill("input#expiry", payment_fields["card_expiry"])
                    page.fill("input#cvv", payment_fields["card_cvv"])
                    page.fill("input#billingZipCode", payment_fields["billing_zip"])

                # CAPTCHA?
                if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                    if not solve_captcha(page):
                        continue
                    time.sleep(2)

                # Confirm reservation
                confirm_btn = page.locator("button:has-text('Confirm Reservation'), button:has-text('Reserve Park')")
                if confirm_btn.is_visible():
                    confirm_btn.click()
                    _post(f"Reservation submitted for {park_name} on {date_str}", "info")
                    page.wait_for_timeout(8000)
                    if "confirmation" in page.url.lower() or "thank you" in page.content().lower():
                        _post(f"🎉 Park pass booked for {park_name} on {date_str}!", "info")
                        # Record
                        STATE.setdefault("booked_parks", []).append({
                            "park": park_name,
                            "date": date_str,
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        save_state(STATE)
                        # eBay listing
                        if CFG["ebay"].get("auto_list"):
                            list_on_ebay(park_name, date_str, party)
                        # Return after one successful booking to avoid hammering
                        return
        except Exception as e:
            _post(f"Booking error: {e}", "error")
        finally:
            browser.close()

def main():
    _wait_for_hub()
    _post("Park Pass Lurker Bot online – watching for cancellations.", "info")
    while True:
        attempt_booking()
        _heartbeat()
        # Wait a bit before the next check to avoid rate limiting
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `park_pass_lurker_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "disney_account": {
    "email": "your_disney_account@email.com",
    "password": "your_password"
  },
  "parks": [
    {
      "name": "Magic Kingdom",
      "date": "2026-06-15",
      "party_size": 4
    }
  ],
  "booking": {
    "preferred_times": ["Morning", "Afternoon"],
    "contact": {
      "phone": "4075551234"
    }
  },
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
    "markup_multiplier": 2.0,
    "auto_list": true
  }
}
"""
