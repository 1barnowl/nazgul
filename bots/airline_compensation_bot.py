#!/usr/bin/env python3
"""
airline_compensation_bot.py — EU261 Airline Compensation Auto‑Claim Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Checks flight status via Aviationstack API to verify EU261 eligibility.
2. Automates claim submission on the airline’s EU261 form (Playwright).
3. Takes a 25% commission on successful claims (Stripe invoice).
4. Reports everything to BotController.

✦ REAL APIs: Aviationstack, Playwright, Stripe, 2Captcha.
  For educational / research purposes only. Automated claim filing may
  violate airline terms. Use only on test accounts or with permission.

SETUP
─────
1. Install dependencies:
      pip install playwright stripe requests
      python -m playwright install chromium

2. Get a free API key from https://aviationstack.com/product (100 req/mo).
   Export: AVIATIONSTACK_API_KEY="your_key"

3. Set 2Captcha key (optional, if airlines use CAPTCHA):
      export CAPTCHA_API_KEY="your_key"

4. Create `airline_claims_config.json` (example at bottom). Fill in:
   - Passenger details and flights to check.
   - Airline claim form URLs, selectors (customise per airline).
   - Your Stripe key for commission billing.

5. Attach to BotController.
"""

import json
import os
import time
import uuid
import threading
import requests
from datetime import datetime, timedelta
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ═══════════════════════════════════════════════════════════════════════════
# Hub connection
HUB      = "http://localhost:8765"
BOT_ID   = "airline_compensation_bot"
BOT_NAME = "EU261 Compensation Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "airline_claims_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "airline_claims_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 86400  # daily scan

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
        "aviationstack_api_key": os.getenv("AVIATIONSTACK_API_KEY", ""),
        "commission_percent": 25.0,
        "passengers": [
            {
                "first_name": "John",
                "last_name": "Smith",
                "email": "john@example.com",
                "booking_reference": "ABC123",
                "flights": [
                    {
                        "airline_iata": "FR",           # Ryanair
                        "flight_number": "FR1234",
                        "departure_date": "2026-06-01",
                        "departure_airport": "STN",
                        "arrival_airport": "MAD"
                    }
                ]
            }
        ],
        "airlines": [
            {
                "iata": "FR",
                "name": "Ryanair",
                "claim_url": "https://www.ryanair.com/gb/en/help/complaints/eu261",
                "selectors": {
                    "first_name": "input#firstName",
                    "last_name": "input#lastName",
                    "email": "input#email",
                    "booking_ref": "input#bookingRef",
                    "flight_date": "input#flightDate",
                    "flight_number": "input#flightNumber",
                    "departure": "input#depAirport",
                    "arrival": "input#arrAirport",
                    "delay_duration": "select#delayHours",
                    "submit": "button.submit-claim"
                },
                "requires_captcha": False
            }
        ],
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
        return {"filed_claims": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()

# ═══════════════════════════════════════════════════════════════════════════
# AviationStack flight status check (free tier)
AVIATIONSTACK_KEY = CFG.get("aviationstack_api_key", "")

def check_flight_delay(flight_number, airline_iata, date):
    """
    Query Aviationstack for real flight status and determine EU261 eligibility.
    Returns (delay_minutes, is_eligible) or (None, False) on failure.
    EU261 eligibility: >3h delay at arrival, cancellation less than
    14 days before departure, denied boarding. Simplified: check if delay > 180 min.
    """
    if not AVIATIONSTACK_KEY:
        _post("Aviationstack API key not set. Cannot check flight status.", "error")
        return None, False

    params = {
        "access_key": AVIATIONSTACK_KEY,
        "flight_number": flight_number,
        "airline_iata": airline_iata,
        "flight_date": date,
        "limit": 1
    }
    try:
        resp = requests.get("https://api.aviationstack.com/v1/flights", params=params, timeout=15)
        data = resp.json()
        if data.get("data") and len(data["data"]) > 0:
            flight = data["data"][0]
            arrival = flight.get("arrival", {})
            # Aviationstack may provide actual arrival, scheduled arrival, delay in minutes (departure/arrival)
            delay = arrival.get("delay")  # arrival delay in minutes, can be null/None
            if delay is not None and delay > 180:
                _post(f"Flight {flight_number} had an arrival delay of {delay} minutes.", "info")
                return delay, True
            else:
                _post(f"Flight {flight_number} delay {delay if delay else 'none'} (not eligible).", "info")
                return delay, False
        else:
            _post(f"Could not find flight {flight_number} on {date}.", "warning")
    except Exception as e:
        _post(f"Aviationstack API error: {e}", "error")
    return None, False

# ═══════════════════════════════════════════════════════════════════════════
# CAPTCHA solver (re‑used from prior bots)
CAPTCHA_KEY = os.getenv("CAPTCHA_API_KEY", "")
def solve_captcha(page, sitekey=None):
    if not CAPTCHA_KEY: return False
    try:
        if not sitekey:
            el = page.locator("[data-sitekey]")
            if el.count(): sitekey = el.get_attribute("data-sitekey")
            else: return False
        resp = requests.get("http://2captcha.com/in.php", params={
            "key": CAPTCHA_KEY, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": page.url, "json": 1
        }, timeout=15).json()
        if resp.get("status") != 1: return False
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
            if r.get("request") != "CAPCHA_NOT_READY": break
    except: pass
    return False

# ═══════════════════════════════════════════════════════════════════════════
# Automated claim filing (Playwright)
def file_eu261_claim(airline_cfg, passenger, flight_info, delay_min):
    """
    Navigate to airline's EU261 claim form, fill in details, and submit.
    Returns True if submission seems successful.
    """
    if not airline_cfg:
        _post("Airline configuration missing.", "error")
        return False

    proxy = None  # can use proxies if configured, omitted for simplicity
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        try:
            claim_url = airline_cfg["claim_url"]
            _post(f"Opening claim form: {claim_url}", "info")
            page.goto(claim_url, wait_until="networkidle", timeout=30000)

            sel = airline_cfg["selectors"]

            # Fill passenger info
            page.fill(sel["first_name"], passenger["first_name"])
            page.fill(sel["last_name"], passenger["last_name"])
            page.fill(sel["email"], passenger["email"])
            if sel.get("booking_ref"):
                page.fill(sel["booking_ref"], passenger["booking_reference"])

            # Fill flight details
            page.fill(sel["flight_date"], flight_info["departure_date"])
            page.fill(sel["flight_number"], flight_info["flight_number"])
            page.fill(sel["departure"], flight_info["departure_airport"])
            page.fill(sel["arrival"], flight_info["arrival_airport"])

            # Specify delay (if form requires it)
            if sel.get("delay_duration"):
                # Map delay_min to a dropdown value
                hours = delay_min // 60
                if hours >= 3:
                    page.select_option(sel["delay_duration"], "over_3_hours")
                else:
                    # shouldn't file if less than 3 hours, but just in case
                    page.select_option(sel["delay_duration"], "less_than_3")

            # Handle CAPTCHA if airline uses one (common)
            if airline_cfg.get("requires_captcha") or \
               page.locator("[data-sitekey]").count() or \
               page.locator("iframe[title*='captcha']").count():
                if not solve_captcha(page):
                    _post("CAPTCHA could not be solved, claim not submitted.", "error")
                    return False
                time.sleep(2)

            # Submit
            submit_sel = sel.get("submit", "button[type='submit']")
            page.click(submit_sel)
            page.wait_for_timeout(5000)

            # Check for confirmation
            if "thank you" in page.content().lower() or "claim submitted" in page.content().lower():
                _post(f"EU261 claim submitted for {passenger['email']}!", "info")
                return True
            else:
                _post("Claim submission may have failed – check manually.", "warning")
                return False
        except Exception as e:
            _post(f"Claim filing error: {e}", "error")
            return False
        finally:
            browser.close()

# ═══════════════════════════════════════════════════════════════════════════
# Commission billing (Stripe)
def charge_commission(client_email, amount_eur=250):
    """Bill the client 25% of the estimated compensation (€250 average * percent)."""
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return True  # skip payment

    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    commission_rate = CFG["commission_percent"] / 100.0
    fee = round(amount_eur * commission_rate, 2)
    if fee <= 0:
        return True

    try:
        intent = stripe.PaymentIntent.create(
            amount=int(fee * 100),      # euros -> cents (stripe assumes lowest currency unit; here EUR)
            currency="eur",
            description=f"EU261 claim assistance fee",
            metadata={"client_email": client_email}
        )
        _post(f"Charged {client_email} €{fee} commission.", "info")
        return True
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return False

# ═══════════════════════════════════════════════════════════════════════════
# Main processing
def process_claims():
    state = load_state()
    passengers = CFG.get("passengers", [])
    if not passengers:
        _post("No passengers configured.", "info")
        return

    for passenger in passengers:
        email = passenger.get("email")
        if not email:
            continue
        # Skip if already processed all flights for this passenger
        if email in [c.get("passenger_email") for c in state.get("filed_claims", [])]:
            continue

        for flight in passenger.get("flights", []):
            flight_number = flight.get("flight_number")
            airline_iata = flight.get("airline_iata")
            dep_date = flight.get("departure_date")

            # 1. Check EU261 eligibility
            delay, eligible = check_flight_delay(flight_number, airline_iata, dep_date)
            if not eligible:
                _post(f"Flight {flight_number} not eligible for EU261.", "info")
                continue

            # 2. Find airline configuration
            airline_cfg = next((a for a in CFG.get("airlines", []) if a["iata"] == airline_iata), None)
            if not airline_cfg:
                _post(f"No airline config for IATA {airline_iata}.", "warning")
                continue

            # 3. File claim
            claim_success = file_eu261_claim(airline_cfg, passenger, flight, delay)
            if claim_success:
                # Record
                state.setdefault("filed_claims", []).append({
                    "passenger_email": email,
                    "flight": flight_number,
                    "date": dep_date,
                    "delay_min": delay,
                    "timestamp": datetime.utcnow().isoformat()
                })
                save_state(state)

                # 4. Charge commission (example: assume average €400 compensation)
                charge_commission(email, amount_eur=400)
                break  # one claim per passenger per run

def main():
    wait_for_hub()
    _post("EU261 Compensation Bot online. Checking flight delays...", "info")
    while True:
        process_claims()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `airline_claims_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "aviationstack_api_key": "YOUR_API_KEY",
  "commission_percent": 25.0,
  "passengers": [
    {
      "first_name": "John",
      "last_name": "Smith",
      "email": "john@example.com",
      "booking_reference": "ABC123",
      "flights": [
        {
          "airline_iata": "FR",
          "flight_number": "FR1234",
          "departure_date": "2026-06-01",
          "departure_airport": "STN",
          "arrival_airport": "MAD"
        }
      ]
    }
  ],
  "airlines": [
    {
      "iata": "FR",
      "name": "Ryanair",
      "claim_url": "https://www.ryanair.com/gb/en/help/complaints/eu261",
      "selectors": {
        "first_name": "input#firstName",
        "last_name": "input#lastName",
        "email": "input#email",
        "booking_ref": "input#bookingRef",
        "flight_date": "input#flightDate",
        "flight_number": "input#flightNumber",
        "departure": "input#depAirport",
        "arrival": "input#arrAirport",
        "delay_duration": "select#delayHours",
        "submit": "button.submit-claim"
      },
      "requires_captcha": false
    }
  ],
  "stripe": {
    "enabled": false,
    "secret_key": "sk_test_..."
  }
}
"""
