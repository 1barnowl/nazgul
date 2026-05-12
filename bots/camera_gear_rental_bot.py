#!/usr/bin/env python3
"""
camera_gear_rental_bot.py — Peer‑to‑Peer Camera/Gear Rental Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Lists gear owners (drones, lenses, cameras) with availability.
2. Matches renters’ requests to available gear.
3. Books the rental on Google Calendar (both parties added).
4. Collects payment (including deposit + insurance) via Stripe.
5. Takes a 12% commission on the rental price.
6. Reports everything to BotController.

✦ REAL APIs: Google Calendar (free/busy), Stripe (PaymentIntent).
  Requires Google service account JSON key and Stripe secret key.
  For educational/research purposes only.

SETUP
─────
1. Install dependencies:
      pip install google-auth google-api-python-client stripe requests

2. Google Calendar:
   • Create a project, enable Calendar API, create a service account.
   • Download the JSON key and save as `google_service_account.json` next to this script.
   • Share the owners’ calendars with the service account email.
   • Export GOOGLE_CALENDAR_ID (optional – default “primary”).

3. Stripe (for payments):
   • Export STRIPE_SECRET_KEY="sk_test_..."
   • (Optionally) STRIPE_PUBLISHABLE_KEY (not used here, but helpful for frontend).

4. Create `gear_rental_config.json` (example at bottom) with:
   • gear_owners (id, name, email, gear_type, rate_per_hour, deposit, insurance_percent, calendar_id)
   • rental_requests (renter_name, email, gear_type, desired_start, duration_hours)
   • bot_fee_percent (e.g., 12.0)

5. Attach to BotController.
"""

import json
import os
import time
import uuid
import threading
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════════
# Hub connection (BotController)
HUB = "http://localhost:8765"
BOT_ID = "camera_gear_rental_bot"
BOT_NAME = "Gear Rental Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gear_rental_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gear_rental_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 60   # check for new rental requests every 60s

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
        "bot_fee_percent": 12.0,
        "google": {
            "service_account_file": "google_service_account.json",
            "calendar_id": os.getenv("GOOGLE_CALENDAR_ID", "primary")
        },
        "stripe": {
            "secret_key": os.getenv("STRIPE_SECRET_KEY", ""),
            "public_key": os.getenv("STRIPE_PUBLISHABLE_KEY", "")
        },
        "gear_owners": [
            {
                "id": "owner1",
                "name": "DJI Store",
                "email": "dji@example.com",
                "gear_type": "drone",
                "model": "DJI Mavic 3 Pro",
                "rate_per_hour": 50.0,
                "deposit": 200.0,
                "insurance_percent": 5.0,      # % of rental price for insurance
                "calendar_id": "primary"
            }
        ],
        "rental_requests": [
            {
                "renter_name": "Alex",
                "email": "alex@example.com",
                "gear_type": "drone",
                "desired_start": "2026-06-01T10:00:00Z",
                "duration_hours": 4,
                "notes": "Real estate video shoot"
            }
        ]
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"rentals": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

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

def get_freebusy(calendar_id, time_min, time_max):
    """Return list of busy blocks for the given calendar."""
    service = get_google_calendar_service()
    if not service:
        return []
    body = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "items": [{"id": calendar_id}]
    }
    try:
        result = service.freebusy().query(body=body).execute()
        busy = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])
        return busy
    except Exception as e:
        _post(f"Free/busy query error: {e}", "error")
        return []

def find_available_slot(calendar_id, desired_start, duration_hours):
    """
    Check if the desired time slot is free.
    Returns True if free, False if any overlap.
    """
    desired_end = desired_start + timedelta(hours=duration_hours)
    busy = get_freebusy(calendar_id, desired_start - timedelta(hours=1), desired_end + timedelta(hours=1))
    for block in busy:
        block_start = datetime.fromisoformat(block["start"])
        block_end = datetime.fromisoformat(block["end"])
        if desired_start < block_end and desired_end > block_start:
            return False
    return True

def create_google_event(owner, renter_req, start_time, duration_hours, calendar_id):
    service = get_google_calendar_service()
    if not service:
        return None
    end_time = start_time + timedelta(hours=duration_hours)
    summary = f"Gear Rental: {owner['gear_type']} ({owner['model']}) – {renter_req['renter_name']}"
    description = (f"Owner: {owner['name']}\nRenter: {renter_req['renter_name']}\n"
                   f"Gear: {owner['gear_type']} {owner['model']}\n"
                   f"Dates: {start_time} – {end_time}\n"
                   f"Notes: {renter_req.get('notes', '')}")
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_time.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end_time.isoformat(), "timeZone": "UTC"},
        "attendees": [
            {"email": owner["email"]},
            {"email": renter_req["email"]}
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
def create_stripe_payment(renter_req, total_amount, rental_id):
    """Create a Stripe PaymentIntent for the total rental amount."""
    if not CFG["stripe"]["secret_key"]:
        _post("Stripe secret key not set. Payment skipped.", "warning")
        return None
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(round(total_amount * 100)),  # cents
            currency="usd",
            description=f"Gear Rental {rental_id} – {renter_req['renter_name']}",
            metadata={
                "rental_id": rental_id,
                "renter_email": renter_req["email"]
            }
        )
        return intent.client_secret
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# Core rental logic
def find_matching_gear(gear_type: str) -> List[dict]:
    """Return owners offering the requested gear type."""
    return [o for o in CFG.get("gear_owners", [])
            if o.get("gear_type") == gear_type]

def process_rental_request(req: dict):
    """
    Match a renter with a gear owner. Check availability, calculate costs,
    book a Google Calendar event, and process payment via Stripe.
    """
    renter_email = req["email"]
    gear_type = req["gear_type"]
    desired_start_str = req.get("desired_start")
    if not desired_start_str:
        _post(f"No start time provided by {renter_email}", "error")
        return

    try:
        desired_start = datetime.fromisoformat(desired_start_str.replace("Z", "+00:00"))
    except:
        _post(f"Invalid start time: {desired_start_str}", "error")
        return

    duration = req.get("duration_hours", 1)

    # Prevent duplicate booking for same person, same gear type, same start
    key = f"{renter_email}_{gear_type}_{desired_start_str}"
    if any(r.get("key") == key for r in STATE.get("rentals", [])):
        return

    owners = find_matching_gear(gear_type)
    if not owners:
        _post(f"No gear owner found for '{gear_type}'", "info")
        return

    # For simplicity, try each owner until one is free
    for owner in owners:
        cal_id = owner.get("calendar_id", CFG["google"]["calendar_id"])
        if not find_available_slot(cal_id, desired_start, duration):
            continue

        # Schedule the event
        event_link = create_google_event(owner, req, desired_start, duration, cal_id)
        if not event_link:
            continue

        # Calculate costs
        rental_rate = owner["rate_per_hour"]
        base_rental = rental_rate * duration
        deposit = owner.get("deposit", 0.0)
        insurance_pct = owner.get("insurance_percent", 0.0)
        insurance = base_rental * (insurance_pct / 100.0)
        bot_fee = base_rental * (CFG["bot_fee_percent"] / 100.0)
        total_amount = base_rental + deposit + insurance + bot_fee

        rental_id = str(uuid.uuid4())
        client_secret = create_stripe_payment(req, total_amount, rental_id)

        # Record in state
        STATE.setdefault("rentals", []).append({
            "key": key,
            "rental_id": rental_id,
            "renter": req["renter_name"],
            "owner": owner["name"],
            "gear": f"{owner['gear_type']} {owner.get('model','')}",
            "start_time": desired_start.isoformat(),
            "duration_h": duration,
            "base_rental": round(base_rental, 2),
            "deposit": round(deposit, 2),
            "insurance": round(insurance, 2),
            "bot_fee": round(bot_fee, 2),
            "total": round(total_amount, 2),
            "stripe_client_secret": client_secret,
            "event_link": event_link
        })
        save_state(STATE)

        _post(f"📷 Rental booked! {req['renter_name']} → {owner['name']} ({gear_type}) "
              f"on {desired_start} for {duration}h. Total: ${total_amount:.2f} "
              f"(Bot fee: ${bot_fee:.2f})",
              "info", {"rental_id": rental_id, "client_secret": client_secret})
        return

    _post(f"No available gear found for {renter_email} at {desired_start_str}", "warning")

# ═══════════════════════════════════════════════════════════════════════════
# Main loop
def main():
    wait_for_hub()
    _post("Camera/Gear Rental Bot online. Monitoring rental requests...", "info")

    while True:
        cfg = load_config()
        requests_list = cfg.get("rental_requests", [])
        for req in requests_list:
            process_rental_request(req)
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `gear_rental_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "bot_fee_percent": 12.0,
  "google": {
    "service_account_file": "google_service_account.json",
    "calendar_id": "primary"
  },
  "stripe": {
    "secret_key": "sk_test_...",
    "public_key": "pk_test_..."
  },
  "gear_owners": [
    {
      "id": "owner1",
      "name": "DJI Store",
      "email": "dji@example.com",
      "gear_type": "drone",
      "model": "DJI Mavic 3 Pro",
      "rate_per_hour": 50.0,
      "deposit": 200.0,
      "insurance_percent": 5.0,
      "calendar_id": "primary"
    }
  ],
  "rental_requests": [
    {
      "renter_name": "Alex",
      "email": "alex@example.com",
      "gear_type": "drone",
      "desired_start": "2026-06-01T10:00:00Z",
      "duration_hours": 4,
      "notes": "Real estate video shoot"
    }
  ]
}
"""
