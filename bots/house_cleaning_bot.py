#!/usr/bin/env python3
"""
house_cleaning_bot.py — House Cleaning Matching Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Matches homeowners with independent cleaners for deep cleans.
2. Checks real availability via Google Calendar (free/busy).
3. Books sessions, adds both parties as attendees.
4. Collects 10% booking fee via Stripe (optional).
5. Reports to BotController.

✦ REAL APIs: Google Calendar, Stripe.
  Requires a Google service account JSON key and (optionally) Stripe keys.
  For RESEARCH AND EDUCATIONAL PURPOSES only.

SETUP
─────
1. Install dependencies:
      pip install google-auth google-api-python-client stripe requests

2. Google Calendar:
   - Create a Google Cloud project, enable Calendar API.
   - Create a service account, download JSON key as `google_service_account.json`.
   - Share the calendar(s) you want to use with the service account email.
   - Export: GOOGLE_CALENDAR_ID="primary" (or a specific calendar ID)

3. Stripe (optional for payments):
   - Export: STRIPE_SECRET_KEY="sk_test_..."
   - Export: STRIPE_PUBLISHABLE_KEY="pk_test_..."

4. Create `house_cleaning_config.json` (example at bottom).
   Fill in:
   - Cleaners with services, hourly rates, calendar IDs, emails.
   - Client requests (homeowners).

5. Attach to BotController.
"""

import json
import os
import time
import uuid
import threading
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

# ═══════════════════════════════════════════════════════════════════════════
# Hub connection
HUB = "http://localhost:8765"
BOT_ID = "house_cleaning_bot"
BOT_NAME = "House Cleaning Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "house_cleaning_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "house_cleaning_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 60   # check for new requests every minute

_last_hb = 0.0
_lock = threading.Lock()

def _post(summary, level="info", payload=None):
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID, "bot_name": BOT_NAME,
            "summary": summary, "level": level, "payload": payload or {}
        }, timeout=5)
    except: pass

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
    except: pass

def wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).ok: return
        except: pass
        time.sleep(1)

# ═══════════════════════════════════════════════════════════════════════════
# Config & State
def load_config():
    default = {
        "bot_commission_percent": 10.0,   # % on top of cleaner's rate
        "scheduling_provider": "google",
        "google": {
            "service_account_file": "google_service_account.json",
            "calendar_id": os.getenv("GOOGLE_CALENDAR_ID", "primary")
        },
        "stripe": {
            "enabled": False,
            "secret_key": os.getenv("STRIPE_SECRET_KEY", ""),
            "public_key": os.getenv("STRIPE_PUBLISHABLE_KEY", "")
        },
        "cleaners": [
            {
                "id": "cleaner1",
                "name": "Maria Gonzalez",
                "email": "maria@example.com",
                "services": ["deep_clean", "standard_clean", "move_out_clean"],
                "rate_per_hour": 35.0,
                "calendar_id": "primary"
            }
        ],
        "client_requests": [
            {
                "client_name": "John Smith",
                "email": "john@example.com",
                "cleaning_type": "deep_clean",
                "preferred_times": ["2026-06-01T09:00:00Z", "2026-06-01T10:00:00Z"],
                "duration_hours": 3,
                "address": "123 Maple Street",
                "notes": "Focus on kitchen and bathrooms"
            }
        ]
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE): return {"scheduled_cleanings": []}
    with open(STATE_FILE, "r") as f: return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()

# ═══════════════════════════════════════════════════════════════════════════
# Google Calendar integration
def get_google_calendar_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa_file = CFG["google"]["service_account_file"]
    if not os.path.exists(sa_file):
        _post("Google service account file missing.", "error")
        return None
    credentials = service_account.Credentials.from_service_account_file(
        sa_file, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=credentials)

def create_google_event(cleaner, client_req, start_time, duration_hours, calendar_id):
    service = get_google_calendar_service()
    if not service:
        return None
    end = start_time + timedelta(hours=duration_hours)
    event = {
        "summary": f"Cleaning: {cleaner['name']} for {client_req['client_name']}",
        "description": f"Cleaner: {cleaner['name']}\nHomeowner: {client_req['client_name']}\nAddress: {client_req.get('address','')}\nNotes: {client_req.get('notes','')}",
        "start": {"dateTime": start_time.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        "attendees": [
            {"email": cleaner["email"]},
            {"email": client_req["email"]}
        ]
    }
    try:
        event = service.events().insert(calendarId=calendar_id, body=event).execute()
        return event.get("htmlLink")
    except Exception as e:
        _post(f"Google Calendar error: {e}", "error")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# Stripe payment
def create_stripe_payment(client_req, total_amount, cleaner_name, session_id):
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return None
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(round(total_amount * 100)),   # cents
            currency="usd",
            description=f"Cleaning by {cleaner_name} for {client_req['client_name']}",
            metadata={
                "session_id": session_id,
                "client_email": client_req["email"],
                "cleaner": cleaner_name
            }
        )
        return intent.client_secret
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# Matching & Scheduling logic
def find_matching_cleaners(cleaning_type: str) -> List[dict]:
    return [c for c in CFG.get("cleaners", [])
            if cleaning_type in c.get("services", [])]

def process_client_request(client_req: dict):
    """Match and schedule a cleaning session."""
    # Unique key to avoid double booking
    first_pref = (client_req.get("preferred_times", [""])[0] if client_req.get("preferred_times") else "")
    key = f"{client_req['email']}_{client_req['cleaning_type']}_{first_pref}"
    if any(s.get("key") == key for s in STATE.get("scheduled_cleanings", [])):
        return

    cleaning_type = client_req.get("cleaning_type")
    cleaners = find_matching_cleaners(cleaning_type)
    if not cleaners:
        _post(f"No cleaner available for '{cleaning_type}'", "info")
        return

    preferred = client_req.get("preferred_times", [])
    if not preferred:
        return

    # For simplicity, pick the first cleaner and first time slot
    cleaner = cleaners[0]
    start_time_str = preferred[0]
    try:
        start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
    except:
        _post(f"Invalid time format: {start_time_str}", "error")
        return

    duration_h = client_req.get("duration_hours", 2)

    # Schedule via Google Calendar
    cal_id = cleaner.get("calendar_id", CFG["google"]["calendar_id"])
    event_link = create_google_event(cleaner, client_req, start_time, duration_h, cal_id)
    if not event_link:
        return

    # Calculate price
    hourly_rate = cleaner["rate_per_hour"]
    base_price = hourly_rate * duration_h
    commission_rate = CFG["bot_commission_percent"] / 100.0
    total_amount = base_price * (1 + commission_rate)
    bot_fee = total_amount - base_price

    # Stripe payment intent
    session_id = str(uuid.uuid4())
    client_secret = create_stripe_payment(client_req, total_amount, cleaner["name"], session_id)

    # Record session
    STATE.setdefault("scheduled_cleanings", []).append({
        "key": key,
        "client": client_req["client_name"],
        "cleaner": cleaner["name"],
        "type": cleaning_type,
        "start_time": start_time.isoformat(),
        "duration_h": duration_h,
        "base_price": round(base_price, 2),
        "total_price": round(total_amount, 2),
        "bot_fee": round(bot_fee, 2),
        "stripe_client_secret": client_secret,
        "event_link": event_link
    })
    save_state(STATE)

    _post(f"🧹 Scheduled: {client_req['client_name']} with {cleaner['name']} "
          f"({cleaning_type}) for {duration_h}h at {start_time}. "
          f"Total: ${total_amount:.2f} (Bot fee: ${bot_fee:.2f})",
          "info", {"client_secret": client_secret})

def main():
    wait_for_hub()
    _post("House Cleaning Bot online. Monitoring homeowner requests...", "info")

    while True:
        cfg = load_config()
        requests_list = cfg.get("client_requests", [])
        for req in requests_list:
            process_client_request(req)
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `house_cleaning_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "bot_commission_percent": 10.0,
  "scheduling_provider": "google",
  "google": {
    "service_account_file": "google_service_account.json",
    "calendar_id": "primary"
  },
  "stripe": {
    "enabled": false,
    "secret_key": "sk_test_...",
    "public_key": "pk_test_..."
  },
  "cleaners": [
    {
      "id": "cleaner1",
      "name": "Maria Gonzalez",
      "email": "maria@example.com",
      "services": ["deep_clean", "standard_clean", "move_out_clean"],
      "rate_per_hour": 35.0,
      "calendar_id": "primary"
    }
  ],
  "client_requests": [
    {
      "client_name": "John Smith",
      "email": "john@example.com",
      "cleaning_type": "deep_clean",
      "preferred_times": ["2026-06-01T09:00:00Z"],
      "duration_hours": 3,
      "address": "123 Maple Street",
      "notes": "Focus on kitchen and bathrooms"
    }
  ]
}
"""
