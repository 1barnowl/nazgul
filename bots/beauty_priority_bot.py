#!/usr/bin/env python3
"""
beauty_priority_bot.py — Beauty Priority Booking Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Scans real‑time availability for high‑demand beauticians.
2. Finds the soonest open slot and books it immediately.
3. Charges a % priority service fee on top of the beautician's rate.
4. Reports to BotController.

✦ REAL APIs: Google Calendar, Stripe.
  Requires a Google service account JSON key and (optionally) Stripe keys.
  For research and educational purposes only.

SETUP
─────
1. Install dependencies:
      pip install google-auth google-api-python-client stripe requests

2. Google Calendar:
   - Create a Google Cloud project, enable Calendar API.
   - Create a service account, download JSON key as `google_service_account.json`
     (place next to this script).
   - Share the beautician's calendar with the service account email.
   - Export: GOOGLE_CALENDAR_ID="primary" (or a specific calendar ID)

3. Stripe (optional for collecting the priority fee):
   - Export: STRIPE_SECRET_KEY="sk_test_..."
   - Export: STRIPE_PUBLISHABLE_KEY="pk_test_..."

4. Create `beauty_config.json` (example at bottom).
   Fill in:
   - Beauticians with their services, rates, calendar IDs, emails.
   - Client requests (service type, earliest possible time, etc.)
   - Priority fee percent (e.g., 15%)

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
# Hub connection
HUB = "http://localhost:8765"
BOT_ID = "beauty_priority_bot"
BOT_NAME = "Beauty Priority Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beauty_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beauty_state.json")

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
        "priority_fee_percent": 15.0,   # % added to beautician's rate for priority booking
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
        "beauticians": [
            {
                "id": "beaut1",
                "name": "Glam Studio",
                "email": "glam@example.com",
                "services": ["hair_styling", "makeup", "nails"],
                "rate_per_hour": 80.0,
                "calendar_id": "primary"
            }
        ],
        "client_requests": [
            {
                "client_name": "Mia",
                "email": "mia@example.com",
                "service": "hair_styling",
                "earliest_start": "2026-06-01T09:00:00Z",   # client available from this time
                "duration_minutes": 60,
                "notes": "Wedding updo"
            }
        ]
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE): return {"priority_bookings": []}
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

def get_freebusy(calendar_id, time_min, time_max):
    """Return busy blocks from Google Calendar as a list of events."""
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
        _post(f"Freebusy query error: {e}", "error")
        return []

def find_soonest_slot(calendar_id, beautician_email, start_window, duration_min):
    """
    Find the soonest available timeslot of length duration_min after start_window.
    Checks the next 7 days in 15‑minute increments.
    Returns start_time (datetime) or None.
    """
    service = get_google_calendar_service()
    if not service:
        return None

    # We'll search from start_window to start_window+7 days in 15‑min steps
    current = start_window
    end_range = start_window + timedelta(days=7)
    step = timedelta(minutes=15)
    while current + timedelta(minutes=duration_min) <= end_range:
        # Check if slot overlaps with any busy blocks
        slot_end = current + timedelta(minutes=duration_min)
        # Query freebusy for a brief window around the slot (to reduce calls we can query larger)
        busy = get_freebusy(calendar_id, current - timedelta(hours=1), slot_end + timedelta(hours=1))
        busy_narrow = [b for b in busy if _overlaps(b, current, slot_end)]
        if not busy_narrow:
            return current
        current += step
    return None

def _overlaps(busy_block, slot_start, slot_end):
    """Check if the slot overlaps with a busy block."""
    busy_start = datetime.fromisoformat(busy_block["start"])
    busy_end = datetime.fromisoformat(busy_block["end"])
    return slot_start < busy_end and slot_end > busy_start

def create_google_event(beautician, client_req, start_time, duration_min, calendar_id):
    service = get_google_calendar_service()
    if not service:
        return None
    end = start_time + timedelta(minutes=duration_min)
    event = {
        "summary": f"Priority Booking: {beautician['name']} + {client_req['client_name']} ({client_req['service']})",
        "description": f"Beautician: {beautician['name']}\nClient: {client_req['client_name']}\nService: {client_req['service']}\nNotes: {client_req.get('notes','')}",
        "start": {"dateTime": start_time.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        "attendees": [
            {"email": beautician["email"]},
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
def create_stripe_payment(client_req, total_amount, beautician_name, session_id):
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return None
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(round(total_amount * 100)),   # cents
            currency="usd",
            description=f"Priority booking with {beautician_name} for {client_req['client_name']}",
            metadata={
                "session_id": session_id,
                "client_email": client_req["email"],
                "beautician": beautician_name
            }
        )
        return intent.client_secret
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# Matching & Priority Booking
def process_client_request(client_req: dict):
    """Find a beautician for the requested service, locate soonest slot, book, and charge priority fee."""
    first_start = client_req.get("earliest_start", "")
    key = f"{client_req['email']}_{client_req['service']}_{first_start}"
    if any(s.get("key") == key for s in STATE.get("priority_bookings", [])):
        return

    service_type = client_req["service"]
    beauticians = [b for b in CFG.get("beauticians", [])
                   if service_type in b.get("services", [])]
    if not beauticians:
        _post(f"No beautician for '{service_type}'", "info")
        return

    # Parse earliest start time
    try:
        earliest = datetime.fromisoformat(first_start.replace("Z", "+00:00"))
    except:
        _post(f"Invalid earliest_start: {first_start}", "error")
        return

    duration = client_req.get("duration_minutes", 60)

    best_slot = None
    best_beautician = None
    for b in beauticians:
        cal_id = b.get("calendar_id", CFG["google"]["calendar_id"])
        slot = find_soonest_slot(cal_id, b["email"], earliest, duration)
        if slot and (best_slot is None or slot < best_slot):
            best_slot = slot
            best_beautician = b

    if not best_slot:
        _post(f"No available slot found for '{service_type}'", "info")
        return

    # Create event
    cal_id = best_beautician.get("calendar_id", CFG["google"]["calendar_id"])
    event_link = create_google_event(best_beautician, client_req, best_slot, duration, cal_id)
    if not event_link:
        return

    # Calculate price
    base_price = best_beautician["rate_per_hour"] * (duration / 60.0)
    priority_fee_pct = CFG["priority_fee_percent"] / 100.0
    total_amount = base_price * (1 + priority_fee_pct)
    bot_fee = total_amount - base_price

    session_id = str(uuid.uuid4())
    client_secret = create_stripe_payment(client_req, total_amount, best_beautician["name"], session_id)

    STATE.setdefault("priority_bookings", []).append({
        "key": key,
        "client": client_req["client_name"],
        "beautician": best_beautician["name"],
        "service": service_type,
        "start_time": best_slot.isoformat(),
        "duration_min": duration,
        "base_price": round(base_price, 2),
        "total_price": round(total_amount, 2),
        "bot_fee": round(bot_fee, 2),
        "stripe_client_secret": client_secret,
        "event_link": event_link
    })
    save_state(STATE)

    _post(f"💄 Priority Booking: {client_req['client_name']} with {best_beautician['name']} "
          f"({service_type}) at {best_slot.isoformat()}. "
          f"Total: ${total_amount:.2f} (Priority fee: ${bot_fee:.2f})",
          "info", {"client_secret": client_secret})

def main():
    wait_for_hub()
    _post("Beauty Priority Bot online. Finding soonest slots...", "info")

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
# Example `beauty_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "priority_fee_percent": 15.0,
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
  "beauticians": [
    {
      "id": "beaut1",
      "name": "Glam Studio",
      "email": "glam@example.com",
      "services": ["hair_styling", "makeup", "nails"],
      "rate_per_hour": 80.0,
      "calendar_id": "primary"
    }
  ],
  "client_requests": [
    {
      "client_name": "Mia",
      "email": "mia@example.com",
      "service": "hair_styling",
      "earliest_start": "2026-06-01T09:00:00Z",
      "duration_minutes": 60,
      "notes": "Wedding updo"
    }
  ]
}
"""
