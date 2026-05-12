#!/usr/bin/env python3
"""
dmv_appointment_bot.py — DMV Appointment Sniper & Resell Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors state DMV online systems for driving test cancellations.
2. Auto‑books the soonest available slot for a queue of clients.
3. Requires Playwright, proxy rotation, and 2Captcha integration.
4. Optionally charges a $25–50 fee via Stripe per booking.

✦ REAL-WORLD INTEGRATIONS:
   - Playwright for browser automation.
   - Google Calendar for adding the appointment to the client’s calendar (optional).
   - Stripe for collecting the booking fee.

✦ CONFIGURATION:
   Each state’s DMV website has different selectors and flows.
   You must customise the `dmv_sites` section in the config file for your target state.
   The bot uses a queue‑based system: when a slot is found, the next client
   in the `client_queue` list is booked.

✦ THIS IS A RESEARCH TOOL. Automating DMV appointments may violate state
  regulations and terms of use. Use only on authorised test systems.

SETUP
─────
1. Install dependencies:
      pip install playwright requests stripe google-auth google-api-python-client
      python -m playwright install chromium

2. Set environment variables:
      export CAPTCHA_API_KEY="your-2captcha-key"
      export GOOGLE_CALENDAR_ID="primary"              # optional
      export STRIPE_SECRET_KEY="sk_test_..."           # optional

3. Place a Google service account JSON key as `google_service_account.json`
   (optional – for Google Calendar integration).

4. Create `dmv_config.json` (example at bottom).

5. Attach to BotController.
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

# ═══════════════════════════════════════════════════════════════════════════
# Hub connection
HUB      = "http://localhost:8765"
BOT_ID   = "dmv_appointment_bot"
BOT_NAME = "DMV Appointment Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dmv_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dmv_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 60   # seconds between scans

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
        "dmv_sites": [
            {
                "state": "California",
                "url": "https://www.dmv.ca.gov/portal/appointments/",
                "selectors": {
                    "service": "select#serviceType",
                    "service_value": "Driving Test",
                    "location": "select#officeId",
                    "location_value": "Los Angeles",
                    "date_picker": "input#appointmentDate",
                    "time_slot_selector": "button.available-slot",
                    "first_name": "input#firstName",
                    "last_name": "input#lastName",
                    "dl_number": "input#dlNumber",
                    "phone": "input#phone",
                    "submit": "button#submitAppointment"
                },
                "pre_actions": "accept_cookies",   # optional: click cookie banner
                "post_actions": "screenshot"        # optional
            }
        ],
        "client_queue": [
            {
                "client_name": "Jane Doe",
                "email": "jane@example.com",
                "dl_number": "D1234567",
                "first_name": "Jane",
                "last_name": "Doe",
                "phone": "5551234567"
            }
        ],
        "booking_fee_usd": 35.0,           # flat fee per successful booking
        "google_calendar": {
            "enabled": False,
            "service_account_file": "google_service_account.json",
            "calendar_id": os.getenv("GOOGLE_CALENDAR_ID", "primary")
        },
        "stripe": {
            "enabled": False,
            "secret_key": os.getenv("STRIPE_SECRET_KEY", "")
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", "")}
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"booked_clients": {}}
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
            _post(f"Captcha submission failed: {resp}", "error")
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
                _post("Captcha solved!", "info")
                return True
            if r.get("request") != "CAPCHA_NOT_READY":
                break
    except:
        pass
    return False

# ═══════════════════════════════════════════════════════════════════════════
# Stripe fee collection
def charge_booking_fee(client_email, fee_usd, booking_id):
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return True  # no payment gateway, skip
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(round(fee_usd * 100)),
            currency="usd",
            description=f"DMV Appointment Booking – {booking_id}",
            metadata={"booking_id": booking_id, "client_email": client_email}
        )
        # In a real service, you'd redirect to a checkout page.
        # For demo, we log the client_secret and assume payment is completed externally.
        _post(f"PaymentIntent created: {intent.client_secret}", "info")
        return True
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return False

# ═══════════════════════════════════════════════════════════════════════════
# Google Calendar – add appointment to client’s calendar
def add_to_google_calendar(client, appointment_dt, location, duration_min=30):
    """Optional: create an event on the bot's calendar and invite the client."""
    if not CFG["google_calendar"]["enabled"]:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        sa = CFG["google_calendar"]["service_account_file"]
        if not os.path.exists(sa):
            return None
        creds = service_account.Credentials.from_service_account_file(
            sa, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        service = build("calendar", "v3", credentials=creds)
        end = appointment_dt + timedelta(minutes=duration_min)
        event = {
            "summary": f"DMV Driving Test – {client['client_name']}",
            "description": f"Appointment at {location}. DL: {client.get('dl_number','')}",
            "start": {"dateTime": appointment_dt.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
            "attendees": [{"email": client["email"]}]
        }
        cal_id = CFG["google_calendar"]["calendar_id"]
        event = service.events().insert(calendarId=cal_id, body=event).execute()
        return event.get("htmlLink")
    except Exception as e:
        _post(f"Google Calendar error: {e}", "warning")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# DMV Booking Flow (configurable via selectors)
def book_appointment(site_cfg: dict, client: dict) -> Optional[str]:
    """
    Attempt to book a driving test appointment for the client.
    Returns the confirmation number/message or None on failure.
    """
    selectors = site_cfg.get("selectors", {})
    if not selectors:
        _post("No selectors defined for this site. Skipping.", "error")
        return None

    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy:
        launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            # 1. Navigate to the appointment page
            _post(f"Opening DMV site: {site_cfg['url']}", "info")
            page.goto(site_cfg["url"], wait_until="networkidle", timeout=30000)

            # 2. Pre-actions (cookie consent, etc.)
            if "accept_cookies" in site_cfg.get("pre_actions", ""):
                try:
                    page.click("button:has-text('Accept')", timeout=2000)
                except:
                    pass

            # 3. Select service type
            svc_sel = selectors.get("service")
            svc_val = selectors.get("service_value")
            if svc_sel and svc_val:
                page.select_option(svc_sel, svc_val)
                _post(f"Selected service: {svc_val}", "info")

            # 4. Select office/location
            loc_sel = selectors.get("location")
            loc_val = selectors.get("location_value")
            if loc_sel and loc_val:
                page.select_option(loc_sel, loc_val)
                _post(f"Selected location: {loc_val}", "info")

            # 5. Date selection (if required – some sites auto-show calendar)
            date_sel = selectors.get("date_picker")
            if date_sel:
                # Click the date picker to open calendar, then choose the first available date
                page.click(date_sel)
                # This is highly site-dependent; we'll try to pick the first non-disabled day
                page.wait_for_selector(".calendar-day:not(.disabled)", timeout=5000).click()
            else:
                # Some sites show the next available slot directly
                pass

            # 6. Click first available time slot
            time_sel = selectors.get("time_slot_selector", "button.available-slot")
            if not page.locator(time_sel).first.is_visible(timeout=10000):
                _post("No time slots available.", "info")
                return None
            page.locator(time_sel).first.click()
            _post("Selected a time slot.", "info")

            # 7. Fill applicant details
            for field, selector in [
                ("first_name", selectors.get("first_name")),
                ("last_name", selectors.get("last_name")),
                ("dl_number", selectors.get("dl_number")),
                ("phone", selectors.get("phone"))
            ]:
                if selector:
                    value = client.get(field, "")
                    if value:
                        page.fill(selector, value)
            _post("Filled client details.", "info")

            # 8. Handle CAPTCHA (often appears before submit)
            if page.locator("[data-sitekey]").count() or page.locator("iframe[title*='captcha']").count():
                if not solve_captcha(page):
                    _post("Captcha unsolved – aborting.", "error")
                    return None
                time.sleep(2)

            # 9. Submit
            submit_sel = selectors.get("submit", "button[type='submit']")
            page.click(submit_sel)
            page.wait_for_timeout(5000)

            # 10. Check for confirmation
            page_text = page.content()
            if "confirmation" in page_text.lower() or "thank you" in page_text.lower():
                # Extract confirmation number if possible (generic)
                conf = re.search(r'(?:confirmation|appointment)\s*(?:#|:)\s*([A-Z0-9-]+)', page_text, re.I)
                conf_id = conf.group(1) if conf else "Unknown"
                _post(f"Appointment booked for {client['client_name']}. Confirmation: {conf_id}", "info")
                return conf_id
            else:
                _post("Could not confirm booking.", "warning")
                return None
        except Exception as e:
            _post(f"Booking error: {e}", "error")
            return None
        finally:
            browser.close()

# ═══════════════════════════════════════════════════════════════════════════
# Main scanning loop
def scan_and_book():
    state = load_state()
    clients = CFG.get("client_queue", [])
    if not clients:
        _post("No clients in queue.", "info")
        return

    # Find the first client not yet booked
    next_client = None
    for c in clients:
        key = c["email"]
        if key not in state.get("booked_clients", {}):
            next_client = c
            break
    if not next_client:
        _post("All clients in queue have been booked.", "info")
        return

    for site in CFG.get("dmv_sites", []):
        _post(f"Scanning {site['state']} for {next_client['client_name']}...", "info")
        conf = book_appointment(site, next_client)
        if conf:
            # Charge fee
            fee = CFG.get("booking_fee_usd", 35)
            if charge_booking_fee(next_client["email"], fee, conf):
                state.setdefault("booked_clients", {})[next_client["email"]] = {
                    "confirmation": conf,
                    "booked_at": datetime.utcnow().isoformat(),
                    "fee": fee
                }
                save_state(state)
                # Add to calendar
                add_to_google_calendar(next_client, datetime.utcnow() + timedelta(days=7), site["selectors"]["location_value"])
                _post(f"🎫 Booking for {next_client['client_name']} complete! Fee: ${fee}", "info")
                return   # one booking per cycle
            else:
                _post("Payment failed – booking recorded but fee not collected.", "warning")
                # Still record it so we don't double‑book
                state.setdefault("booked_clients", {})[next_client["email"]] = {
                    "confirmation": conf,
                    "booked_at": datetime.utcnow().isoformat(),
                    "fee": 0
                }
                save_state(state)
                return

def main():
    wait_for_hub()
    _post("DMV Appointment Bot online. Scanning for driving test slots...", "info")
    while True:
        scan_and_book()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `dmv_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "dmv_sites": [
    {
      "state": "California",
      "url": "https://www.dmv.ca.gov/portal/appointments/",
      "selectors": {
        "service": "select#serviceType",
        "service_value": "Driving Test",
        "location": "select#officeId",
        "location_value": "Los Angeles",
        "date_picker": "input#appointmentDate",
        "time_slot_selector": "button.available-slot",
        "first_name": "input#firstName",
        "last_name": "input#lastName",
        "dl_number": "input#dlNumber",
        "phone": "input#phone",
        "submit": "button#submitAppointment"
      }
    }
  ],
  "client_queue": [
    {"client_name": "Jane Doe", "email": "jane@example.com", "dl_number": "D1234567",
     "first_name": "Jane", "last_name": "Doe", "phone": "5551234567"}
  ],
  "booking_fee_usd": 35,
  "google_calendar": {"enabled": false, "service_account_file": "google_service_account.json", "calendar_id": "primary"},
  "stripe": {"enabled": false, "secret_key": "sk_test_..."},
  "proxies": {"list": []},
  "captcha": {"api_key": "YOUR_2CAPTCHA_KEY"}
}
"""
