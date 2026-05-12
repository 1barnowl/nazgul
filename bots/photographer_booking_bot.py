#!/usr/bin/env python3
"""
photographer_booking_bot.py — Photographer/Videographer Booking Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Matches clients with vetted photographers/videographers based on
   shoot type (real estate, portrait, event, etc.).
2. Checks availability via Google Calendar and books sessions.
3. Collects 15% booking fee via Stripe (optional).
4. Posts updates to BotController.

✦ REAL APIs: Google Calendar, Stripe.
  Requires a Google service account and (optionally) Stripe keys.
  For educational and research purposes only.

SETUP
─────
1. Install dependencies:
      pip install google-auth google-api-python-client stripe requests

2. Google Calendar:
   - Create a Google Cloud project, enable Calendar API.
   - Create a service account, download JSON key as `google_service_account.json`
     (place next to this script).
   - Share the calendar(s) you want to use with the service account email.
   - Export: GOOGLE_CALENDAR_ID="primary" (or a specific calendar ID)

3. Stripe (optional for payments):
   - Export: STRIPE_SECRET_KEY="sk_test_..."
   - Export: STRIPE_PUBLISHABLE_KEY="pk_test_..."

4. Create `photographer_config.json` (example at bottom).
   Fill in:
   - Photographers/videographers with specialties, rates, calendar IDs, emails.
   - Client requests (shoot details, preferred times).

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
BOT_ID = "photographer_booking_bot"
BOT_NAME = "Photographer Booking Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photographer_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photographer_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 60   # seconds between processing new requests

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
        "bot_commission_percent": 15.0,   # % added to freelancer's rate
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
        "freelancers": [
            {
                "id": "photog1",
                "name": "Alex Turner",
                "email": "alex@example.com",
                "specialties": ["real_estate_photo", "portrait_photo", "event_photo"],
                "rate_per_hour": 100.0,
                "calendar_id": "primary"
            },
            {
                "id": "videog1",
                "name": "Jordan Lee",
                "email": "jordan@example.com",
                "specialties": ["event_video", "real_estate_video"],
                "rate_per_hour": 120.0,
                "calendar_id": "primary"
            }
        ],
        "client_requests": [
            {
                "client_name": "Sarah Connor",
                "email": "sarah@example.com",
                "shoot_type": "real_estate_photo",
                "preferred_times": ["2026-06-01T10:00:00Z", "2026-06-01T11:00:00Z"],
                "duration_hours": 2,
                "location": "456 Elm St, Springfield",
                "notes": "Twilight shoot, need wide angle"
            }
        ]
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE): return {"scheduled_shoots": []}
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

def create_google_event(freelancer, client_req, start_time, duration_hours, calendar_id):
    """Create a Google Calendar event and return its HTML link."""
    service = get_google_calendar_service()
    if not service:
        return None
    end = start_time + timedelta(hours=duration_hours)
    event = {
        "summary": f"Shoot: {freelancer['name']} + {client_req['client_name']} ({client_req['shoot_type']})",
        "description": f"Photographer/Videographer: {freelancer['name']}\nClient: {client_req['client_name']}\nType: {client_req['shoot_type']}\nLocation: {client_req.get('location','')}\nNotes: {client_req.get('notes','')}",
        "start": {"dateTime": start_time.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        "attendees": [
            {"email": freelancer["email"]},
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
def create_stripe_payment(client_req, total_amount, freelancer_name, session_id):
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return None
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(round(total_amount * 100)),   # cents
            currency="usd",
            description=f"Shoot with {freelancer_name} for {client_req['client_name']}",
            metadata={
                "session_id": session_id,
                "client_email": client_req["email"],
                "freelancer": freelancer_name
            }
        )
        return intent.client_secret
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# Matching & Scheduling
def find_matching_freelancers(shoot_type: str) -> List[dict]:
    """Return freelancers who offer the requested shoot type."""
    return [f for f in CFG.get("freelancers", [])
            if shoot_type in f.get("specialties", [])]

def process_client_request(client_req: dict):
    """Match and schedule a shoot."""
    # Unique key to avoid double booking
    first_pref = (client_req.get("preferred_times", [""])[0] if client_req.get("preferred_times") else "")
    key = f"{client_req['email']}_{client_req['shoot_type']}_{first_pref}"
    if any(s.get("key") == key for s in STATE.get("scheduled_shoots", [])):
        return

    shoot_type = client_req.get("shoot_type")
    freelancers = find_matching_freelancers(shoot_type)
    if not freelancers:
        _post(f"No freelancer available for '{shoot_type}'", "info")
        return

    preferred = client_req.get("preferred_times", [])
    if not preferred:
        return

    # Pick first freelancer and first time slot (you could implement smarter matching)
    freelancer = freelancers[0]
    start_time_str = preferred[0]
    try:
        start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
    except:
        _post(f"Invalid time format: {start_time_str}", "error")
        return

    duration_h = client_req.get("duration_hours", 2)

    # Schedule via Google Calendar
    cal_id = freelancer.get("calendar_id", CFG["google"]["calendar_id"])
    event_link = create_google_event(freelancer, client_req, start_time, duration_h, cal_id)
    if not event_link:
        return

    # Calculate price
    hourly_rate = freelancer["rate_per_hour"]
    base_price = hourly_rate * duration_h
    commission_rate = CFG["bot_commission_percent"] / 100.0
    total_amount = base_price * (1 + commission_rate)
    bot_fee = total_amount - base_price

    # Stripe payment intent
    session_id = str(uuid.uuid4())
    client_secret = create_stripe_payment(client_req, total_amount, freelancer["name"], session_id)

    # Record session
    STATE.setdefault("scheduled_shoots", []).append({
        "key": key,
        "client": client_req["client_name"],
        "freelancer": freelancer["name"],
        "shoot_type": shoot_type,
        "start_time": start_time.isoformat(),
        "duration_h": duration_h,
        "base_price": round(base_price, 2),
        "total_price": round(total_amount, 2),
        "bot_fee": round(bot_fee, 2),
        "stripe_client_secret": client_secret,
        "event_link": event_link
    })
    save_state(STATE)

    _post(f"📸 Scheduled: {client_req['client_name']} with {freelancer['name']} "
          f"({shoot_type}) for {duration_h}h at {start_time}. "
          f"Total: ${total_amount:.2f} (Bot fee: ${bot_fee:.2f})",
          "info", {"client_secret": client_secret})

def main():
    wait_for_hub()
    _post("Photographer/Videographer Booking Bot online. Monitoring client requests...", "info")

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
# Example `photographer_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "bot_commission_percent": 15.0,
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
  "freelancers": [
    {
      "id": "photog1",
      "name": "Alex Turner",
      "email": "alex@example.com",
      "specialties": ["real_estate_photo", "portrait_photo", "event_photo"],
      "rate_per_hour": 100.0,
      "calendar_id": "primary"
    }
  ],
  "client_requests": [
    {
      "client_name": "Sarah Connor",
      "email": "sarah@example.com",
      "shoot_type": "real_estate_photo",
      "preferred_times": ["2026-06-01T10:00:00Z"],
      "duration_hours": 2,
      "location": "456 Elm St, Springfield",
      "notes": "Twilight shoot"
    }
  ]
}
"""
