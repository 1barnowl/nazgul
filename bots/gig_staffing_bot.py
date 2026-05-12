#!/usr/bin/env python3
"""
gig_staffing_bot.py — Event Gig Staffing Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Matches catering companies with bartenders/servers from a gig pool.
2. Checks real availability via Google Calendar (free/busy).
3. Schedules shifts and books them on the worker's calendar.
4. Collects a flat $15 booking fee per worker per shift via Stripe.
5. Reports to BotController.

✦ REAL APIs: Google Calendar, Stripe.
  Requires Google service account JSON key and Stripe secret key.
  For educational/research purposes only.

SETUP
─────
1. Install dependencies:
      pip install google-auth google-api-python-client stripe requests

2. Google Calendar:
   - Create a project, enable Calendar API, create a service account.
   - Download the JSON key as `google_service_account.json` next to this script.
   - Share each worker's calendar with the service account email.
   - Export: GOOGLE_CALENDAR_ID (optional, default "primary").

3. Stripe (for collecting the booking fee):
   - Export: STRIPE_SECRET_KEY="sk_test_..."

4. Create `gig_config.json` (example at bottom). Fill in:
   - workers: id, name, email, role, calendar_id (optional)
   - client_requests: company_name, email, role, date, start_time, duration_hours, num_workers

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
BOT_ID = "gig_staffing_bot"
BOT_NAME = "Gig Staffing Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gig_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gig_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 60   # check for new client requests every minute

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
        "booking_fee_per_worker": 15.0,   # flat $15 per worker per shift
        "google": {
            "service_account_file": "google_service_account.json",
            "calendar_id": os.getenv("GOOGLE_CALENDAR_ID", "primary")
        },
        "stripe": {
            "secret_key": os.getenv("STRIPE_SECRET_KEY", "")
        },
        "workers": [
            {
                "id": "worker1",
                "name": "Jane Doe",
                "email": "jane@example.com",
                "role": "bartender",
                "calendar_id": "primary"
            },
            {
                "id": "worker2",
                "name": "John Smith",
                "email": "john@example.com",
                "role": "server",
                "calendar_id": "primary"
            }
        ],
        "client_requests": [
            {
                "company_name": "Elite Catering",
                "email": "events@elite.example.com",
                "role": "bartender",
                "event_date": "2026-06-01",
                "start_time": "18:00:00",      # local time, use UTC? We'll assume UTC in example.
                "duration_hours": 6,
                "num_workers": 2,
                "notes": "Wedding reception"
            }
        ]
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE): return {"filled_shifts": []}
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
    """Return list of busy blocks for the given calendar."""
    service = get_google_calendar_service()
    if not service: return []
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

def is_slot_free(calendar_id, start_time, duration_hours):
    """Check if the time slot is free for the given duration_hours."""
    end_time = start_time + timedelta(hours=duration_hours)
    busy = get_freebusy(calendar_id, start_time - timedelta(hours=1), end_time + timedelta(hours=1))
    for block in busy:
        bstart = datetime.fromisoformat(block["start"])
        bend = datetime.fromisoformat(block["end"])
        if start_time < bend and end_time > bstart:
            return False
    return True

def create_google_event(worker, client_req, start_time, duration_hours, calendar_id):
    service = get_google_calendar_service()
    if not service: return None
    end = start_time + timedelta(hours=duration_hours)
    summary = f"Gig: {worker['role']} for {client_req['company_name']}"
    description = f"Worker: {worker['name']}\nCompany: {client_req['company_name']}\nRole: {worker['role']}\nNotes: {client_req.get('notes','')}"
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_time.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        "attendees": [
            {"email": worker["email"]},
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
# Stripe payment (flat fee)
def create_stripe_fee(client_req, total_fee, booking_id):
    if not CFG["stripe"]["secret_key"]:
        _post("Stripe secret key not set. Payment skipped.", "warning")
        return None
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(round(total_fee * 100)),  # cents
            currency="usd",
            description=f"Gig staffing fee – {client_req['company_name']}",
            metadata={
                "booking_id": booking_id,
                "company_email": client_req["email"]
            }
        )
        return intent.client_secret
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# Core staffing logic
def process_client_request(client_req: dict):
    """Match workers for the requested role, check availability, book shifts, collect fee."""
    key = f"{client_req['company_name']}_{client_req['role']}_{client_req['event_date']}_{client_req['start_time']}"
    if any(s.get("key") == key for s in STATE.get("filled_shifts", [])):
        return

    # Build shift start datetime
    event_date = client_req.get("event_date")
    start_time_str = client_req.get("start_time")
    if not event_date or not start_time_str:
        _post(f"Missing date/time in request from {client_req['company_name']}", "error")
        return
    try:
        dt_str = f"{event_date}T{start_time_str}Z"
        shift_start = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except:
        _post(f"Invalid date/time format: {event_date} {start_time_str}", "error")
        return

    duration = client_req.get("duration_hours", 4)
    role_needed = client_req["role"]
    num_needed = client_req.get("num_workers", 1)

    # Find all workers of that role
    eligible = [w for w in CFG.get("workers", []) if w.get("role") == role_needed]
    if not eligible:
        _post(f"No workers with role '{role_needed}'", "info")
        return

    booked_workers = []
    for worker in eligible:
        cal_id = worker.get("calendar_id", CFG["google"]["calendar_id"])
        if is_slot_free(cal_id, shift_start, duration):
            # Book the shift on their calendar (they must be attendee)
            event_link = create_google_event(worker, client_req, shift_start, duration, cal_id)
            if event_link:
                booked_workers.append({
                    "worker": worker["name"],
                    "email": worker["email"],
                    "event_link": event_link
                })
                if len(booked_workers) >= num_needed:
                    break

    if not booked_workers:
        _post(f"No available {role_needed}s for {client_req['company_name']} on {event_date}", "warning")
        return

    # Calculate fee: $15 per booked worker
    total_fee = CFG["booking_fee_per_worker"] * len(booked_workers)
    booking_id = str(uuid.uuid4())
    client_secret = create_stripe_fee(client_req, total_fee, booking_id)

    # Record in state
    STATE.setdefault("filled_shifts", []).append({
        "key": key,
        "booking_id": booking_id,
        "company": client_req["company_name"],
        "role": role_needed,
        "start_time": shift_start.isoformat(),
        "duration_h": duration,
        "workers": booked_workers,
        "total_fee": round(total_fee, 2),
        "stripe_client_secret": client_secret
    })
    save_state(STATE)

    workers_str = ", ".join(w["worker"] for w in booked_workers)
    _post(f"🍸 Staffed: {client_req['company_name']} ({role_needed}) – {workers_str} "
          f"on {shift_start}. Booking fee: ${total_fee:.2f}",
          "info", {"booking_id": booking_id, "client_secret": client_secret})

def main():
    wait_for_hub()
    _post("Event Gig Staffing Bot online. Monitoring catering requests...", "info")

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
# Example `gig_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "booking_fee_per_worker": 15.0,
  "google": {
    "service_account_file": "google_service_account.json",
    "calendar_id": "primary"
  },
  "stripe": {
    "secret_key": "sk_test_..."
  },
  "workers": [
    {
      "id": "worker1",
      "name": "Jane Doe",
      "email": "jane@example.com",
      "role": "bartender",
      "calendar_id": "primary"
    },
    {
      "id": "worker2",
      "name": "John Smith",
      "email": "john@example.com",
      "role": "bartender",
      "calendar_id": "primary"
    }
  ],
  "client_requests": [
    {
      "company_name": "Elite Catering",
      "email": "events@elite.example.com",
      "role": "bartender",
      "event_date": "2026-06-01",
      "start_time": "18:00:00",
      "duration_hours": 6,
      "num_workers": 2,
      "notes": "Wedding reception"
    }
  ]
}
"""
