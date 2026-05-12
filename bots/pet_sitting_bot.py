#!/usr/bin/env python3
"""
pet_sitting_bot.py — Dog Walking & Pet Sitting Scheduling Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Matches owners with available walkers/sitters, schedules sessions
via Google Calendar, optionally collects payment via Stripe,
and takes a 10‑15% commission per booking.

✦ REAL FUNCTIONAL BOT — uses live Google Calendar, Stripe APIs.
  Requires proper API credentials. For educational/research purposes
  only. Do not use in production without consent.

SETUP
─────
1. Install dependencies:
      pip install requests google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client stripe

2. For Google Calendar scheduling:
   - Create a Google Cloud project, enable Calendar API.
   - Create a service account, download JSON key as `google_service_account.json`
     (place next to this script).
   - Share the calendar(s) you want to use with the service account email.
   - Set env var: GOOGLE_CALENDAR_ID="primary" (or a specific calendar ID).

3. For Stripe payments (optional):
   - Export: STRIPE_SECRET_KEY="sk_test_..."
   - Export: STRIPE_PUBLISHABLE_KEY="pk_test_..."

4. Create `pet_sitting_config.json` (example at bottom). Fill in:
   - Walkers/sitters with their subjects (dog walking, cat sitting, etc.),
     rate per 30 min, calendar ID, email.
   - Owner requests (you can add dummy requests; in production these
     would come from a web form or API).

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
BOT_ID = "pet_sitting_bot"
BOT_NAME = "Pet Sitting Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pet_sitting_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pet_sitting_state.json")

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
        "bot_commission_percent": 12.0,   # % added to base rate
        "scheduling_provider": "google",  # only Google for now
        "google": {
            "service_account_file": "google_service_account.json",
            "calendar_id": os.getenv("GOOGLE_CALENDAR_ID", "primary")
        },
        "stripe": {
            "enabled": False,
            "secret_key": os.getenv("STRIPE_SECRET_KEY", ""),
            "public_key": os.getenv("STRIPE_PUBLISHABLE_KEY", "")
        },
        "walkers_sitters": [
            {
                "id": "walker1",
                "name": "Jessica",
                "email": "jessica@example.com",
                "services": ["dog_walking", "cat_sitting"],
                "rate_per_30_min": 20.0,
                "calendar_id": "primary",
                "bio": "Certified vet tech, loves animals."
            }
        ],
        "owner_requests": [
            {
                "owner_name": "Alex",
                "email": "alex@example.com",
                "pet_type": "dog",
                "service": "dog_walking",
                "preferred_dates": ["2026-06-01T09:00:00Z", "2026-06-01T10:00:00Z"],
                "duration_minutes": 30,
                "address": "123 Main St",
                "notes": "Friendly lab, needs 30 min walk"
            }
        ]
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE): return {"scheduled_sessions": []}
    with open(STATE_FILE, "r") as f: return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()

# ═══════════════════════════════════════════════════════════════════════════
# Google Calendar integration
def get_google_calendar_service():
    """Create a Calendar API service using the service account."""
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

def create_google_event(walker, owner_req, start_time, duration_min, calendar_id):
    """Create a Google Calendar event, return HTML link."""
    service = get_google_calendar_service()
    if not service:
        return None
    end = start_time + timedelta(minutes=duration_min)
    event = {
        "summary": f"{owner_req['pet_type'].capitalize()} {owner_req['service']} - {owner_req['owner_name']}",
        "description": f"Walker: {walker['name']}\nOwner: {owner_req['owner_name']}\nAddress: {owner_req.get('address','')}\nNotes: {owner_req.get('notes','')}",
        "start": {"dateTime": start_time.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        "attendees": [
            {"email": walker["email"]},
            {"email": owner_req["email"]}
        ]
    }
    try:
        event = service.events().insert(calendarId=calendar_id, body=event).execute()
        return event.get("htmlLink")
    except Exception as e:
        _post(f"Google Calendar error: {e}", "error")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# Stripe payment (collect total including commission)
def create_stripe_payment(owner_req, total_amount, walker_name, session_id):
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return None
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(round(total_amount * 100)),
            currency="usd",
            description=f"Pet sitting with {walker_name} for {owner_req['owner_name']}",
            metadata={
                "session_id": session_id,
                "owner_email": owner_req["email"],
                "walker": walker_name
            }
        )
        return intent.client_secret
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# Matching & Scheduling
def find_available_walkers(service_requested: str) -> List[dict]:
    """Return walkers/sitters offering the requested service."""
    return [w for w in CFG.get("walkers_sitters", [])
            if service_requested in w.get("services", [])]

def get_walker_free_slots(walker: dict, start_date_str: Optional[str] = None) -> List[datetime]:
    """
    Placeholder: check walker's real calendar using freebusy API.
    For simplicity, we'll assume the provided preferred_dates from owner
    are directly checked against conflicts. We'll let Google Calendar handle
    conflicts when inserting; if duplicate, it will fail.
    (Real implementation would query freebusy and offer only truly open slots.)
    """
    # Return all provided dates as available (they will be validated on insert)
    return []

def process_owner_request(owner_req: dict):
    """Match and schedule a session for the owner."""
    # Unique key to avoid double booking
    key = f"{owner_req['email']}_{owner_req['service']}_{owner_req.get('preferred_dates', [''])[0]}"
    if any(s.get("key") == key for s in STATE.get("scheduled_sessions", [])):
        return

    walkers = find_available_walkers(owner_req["service"])
    if not walkers:
        _post(f"No walker found for {owner_req['service']}", "info")
        return

    preferred_times = owner_req.get("preferred_dates", [])
    if not preferred_times:
        _post(f"No preferred times from {owner_req['owner_name']}", "warning")
        return

    # Try the first walker, first time slot
    walker = walkers[0]  # could add more complex matching later
    start_time_str = preferred_times[0]
    try:
        start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
    except:
        _post(f"Invalid time format: {start_time_str}", "error")
        return

    duration = owner_req.get("duration_minutes", 30)

    # Create calendar event
    cal_id = walker.get("calendar_id", CFG["google"]["calendar_id"])
    event_link = create_google_event(walker, owner_req, start_time, duration, cal_id)
    if not event_link:
        return

    # Calculate total price
    base_rate = walker["rate_per_30_min"] * (duration / 30.0)
    commission_rate = CFG["bot_commission_percent"] / 100.0
    total_amount = base_rate * (1 + commission_rate)
    bot_fee = total_amount - base_rate

    # Create Stripe payment intent
    session_id = str(uuid.uuid4())
    client_secret = create_stripe_payment(owner_req, total_amount, walker["name"], session_id)

    # Record session
    STATE.setdefault("scheduled_sessions", []).append({
        "key": key,
        "owner": owner_req["owner_name"],
        "walker": walker["name"],
        "service": owner_req["service"],
        "pet_type": owner_req.get("pet_type", ""),
        "start_time": start_time.isoformat(),
        "duration_min": duration,
        "base_price": round(base_rate, 2),
        "total_price": round(total_amount, 2),
        "bot_fee": round(bot_fee, 2),
        "stripe_client_secret": client_secret,
        "event_link": event_link
    })
    save_state(STATE)

    _post(f"🐾 Scheduled: {owner_req['owner_name']} with {walker['name']} "
          f"({owner_req['service']}) at {start_time}. Total: ${total_amount:.2f} "
          f"(Bot earns ${bot_fee:.2f})",
          "info", {"client_secret": client_secret})

def main():
    wait_for_hub()
    _post("Pet Sitting Bot online. Monitoring owner requests...", "info")

    while True:
        cfg = load_config()
        requests_list = cfg.get("owner_requests", [])
        for owner_req in requests_list:
            process_owner_request(owner_req)
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `pet_sitting_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "bot_commission_percent": 12.0,
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
  "walkers_sitters": [
    {
      "id": "walker1",
      "name": "Jessica",
      "email": "jessica@example.com",
      "services": ["dog_walking", "cat_sitting"],
      "rate_per_30_min": 20.0,
      "calendar_id": "primary"
    }
  ],
  "owner_requests": [
    {
      "owner_name": "Alex",
      "email": "alex@example.com",
      "pet_type": "dog",
      "service": "dog_walking",
      "preferred_dates": ["2026-06-01T09:00:00Z"],
      "duration_minutes": 30,
      "address": "123 Main St",
      "notes": "Friendly lab"
    }
  ]
}
"""
