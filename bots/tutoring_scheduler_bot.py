#!/usr/bin/env python3
"""
tutoring_scheduler_bot.py — Tutoring Scheduler Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Matches students with tutors based on subject/availability,
schedules real sessions via Google Calendar / Calendly,
charges a booking fee via Stripe, and posts updates to BotController.

SETUP
─────
1. Install dependencies:
      pip install requests google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client stripe

2. For Google Calendar scheduling (option A):
      - Create a Google Cloud project, enable Calendar API.
      - Create a service account, download JSON key, rename as
        `google_service_account.json` next to this script.
      - Share the calendar you want to use with the service account email
        (e.g., your main calendar or a dedicated bot calendar).
      - Set env var: export GOOGLE_CALENDAR_ID="primary"

   For Calendly scheduling (option B):
      - Get a Calendly personal access token from
        https://calendly.com/integrations/api_webhooks
      - Export: CALENDLY_TOKEN="your-token"

3. For Stripe payment (optional, to collect booking fee):
      - Export: STRIPE_SECRET_KEY="sk_test_..."
      - Export: STRIPE_PUBLISHABLE_KEY="pk_test_..." (if using Checkout)
      - The bot will create a PaymentIntent and output a client_secret;
        you'd need a frontend to complete the payment. For testing,
        it can just log the payment URL.

4. Create a config file `tutoring_config.json` (see example at bottom).
   Define tutors, subjects, rates, and the bot's fee percentage.

5. Attach to BotController.
"""

import json, os, time, uuid, threading, requests
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

# ═══════════════════════════════════════════════════════════════════════════
# Hub connection
HUB = "http://localhost:8765"
BOT_ID = "tutoring_scheduler_bot"
BOT_NAME = "Tutoring Scheduler"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tutoring_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tutoring_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 60   # check for new student requests every minute

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
        "bot_fee_percent": 15.0,   # booking fee % on top of tutor rate
        "scheduling_provider": "google",  # "google" or "calendly"
        "google": {
            "service_account_file": "google_service_account.json",
            "calendar_id": os.getenv("GOOGLE_CALENDAR_ID", "primary")
        },
        "calendly": {
            "token": os.getenv("CALENDLY_TOKEN", ""),
            "user_uri": ""  # your Calendly user URI (e.g., https://api.calendly.com/users/...)
        },
        "stripe": {
            "enabled": False,
            "secret_key": os.getenv("STRIPE_SECRET_KEY", ""),
            "public_key": os.getenv("STRIPE_PUBLISHABLE_KEY", "")
        },
        "tutors": [
            {
                "name": "Dr. Alice",
                "email": "alice@university.edu",
                "subjects": ["Calculus", "Linear Algebra"],
                "rate_per_hour": 50.0,
                "calendar_id": "primary"  # or specific calendar ID
                # if using Calendly: "calendly_event_url": "https://calendly.com/alice/30min"
            }
        ],
        "student_requests": [
            {
                "student_name": "Bob",
                "email": "bob@student.edu",
                "subject": "Calculus",
                "preferred_times": ["2026-06-01T14:00:00Z", "2026-06-01T15:00:00Z"],
                "duration_minutes": 60
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
# Google Calendar integration (if chosen)
def get_google_calendar_service():
    """Return a Google Calendar API service instance using service account."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa_file = CFG["google"]["service_account_file"]
    if not os.path.exists(sa_file):
        _post("Google service account file not found.", "error")
        return None
    credentials = service_account.Credentials.from_service_account_file(
        sa_file, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=credentials)

def create_google_event(tutor, student, start_time, duration_min, calendar_id):
    """Create a Google Calendar event and return the event link."""
    service = get_google_calendar_service()
    if not service: return None
    end = start_time + timedelta(minutes=duration_min)
    event = {
        "summary": f"Tutoring: {student['student_name']} x {tutor['name']} - {student['subject']}",
        "description": f"Tutor: {tutor['name']}\nStudent: {student['student_name']}\nSubject: {student['subject']}",
        "start": {"dateTime": start_time.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        "attendees": [
            {"email": tutor["email"]},
            {"email": student["email"]}
        ],
    }
    try:
        event = service.events().insert(calendarId=calendar_id, body=event).execute()
        return event.get("htmlLink")
    except Exception as e:
        _post(f"Google Calendar error: {e}", "error")
        return None

# Calendly integration (alternative)
def book_calendly_slot(tutor, start_time, student):
    """Book an event on Calendly using the API."""
    token = CFG["calendly"]["token"]
    if not token: return None
    # Get event type UUID from tutor's scheduling URL
    event_url = tutor.get("calendly_event_url")
    if not event_url:
        _post(f"No Calendly URL for {tutor['name']}", "warning")
        return None
    # Extract UUID from URL (e.g., https://calendly.com/alice/30min -> 30min)
    parts = event_url.rstrip("/").split("/")
    event_slug = parts[-1]
    user_slug = parts[-2] if len(parts) > 2 else ""
    # Look up event type UUID via Calendly API
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # Get user URI
    user_uri = CFG["calendly"]["user_uri"]
    if not user_uri:
        # try to fetch user
        resp = requests.get("https://api.calendly.com/users/me", headers=headers)
        user_uri = resp.json().get("resource", {}).get("uri", "")
        if not user_uri: return None
    # Get event types
    resp = requests.get(f"{user_uri}/event_types", headers=headers)
    event_types = resp.json().get("collection", [])
    event_uuid = None
    for et in event_types:
        if et["scheduling_url"].endswith(event_slug):
            event_uuid = et["uri"].split("/")[-1]
            break
    if not event_uuid:
        _post("Could not find Calendly event type.", "error")
        return None
    # Schedule event
    payload = {
        "event_type_uuid": event_uuid,
        "start_time": start_time.isoformat(),
        "invitee": {
            "email": student["email"],
            "name": student["student_name"]
        },
        "location": {
            "kind": "custom",
            "location": "Online"
        }
    }
    resp = requests.post("https://api.calendly.com/scheduled_events", json=payload, headers=headers)
    if resp.status_code == 201:
        event = resp.json()
        return event.get("resource", {}).get("uri")
    else:
        _post(f"Calendly booking failed: {resp.text}", "error")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# Payment (Stripe) – create a charge for the booking fee
def create_stripe_payment(student, total_amount, tutor_name, session_id):
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return None
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(total_amount * 100),  # cents
            currency="usd",
            description=f"Tutoring with {tutor_name} - {student['student_name']}",
            metadata={
                "session_id": session_id,
                "student_email": student["email"],
                "tutor": tutor_name
            }
        )
        return intent.client_secret
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# Matching & Scheduling logic
def find_matching_tutors(student_req: dict) -> List[dict]:
    """Return tutors that teach the requested subject."""
    matching = []
    for tutor in CFG.get("tutors", []):
        if student_req["subject"] in tutor.get("subjects", []):
            matching.append(tutor)
    return matching

def get_free_slots(tutor: dict, date_start: str, date_end: str = None) -> List[datetime]:
    """
    Check tutor's availability (via Google Calendar free/busy or Calendly).
    For simplicity, we'll just return the student's preferred times that are free.
    Real implementation would query the calendar.
    """
    # Placeholder: assume all preferred times are available.
    # We'll filter later when creating the event (Google will reject conflicts).
    # For a robust bot, you'd call freebusy.query.
    times = []
    for t_str in student_req.get("preferred_times", []):
        try:
            dt = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
            times.append(dt)
        except: pass
    return times

def process_student_request(student_req: dict):
    """Try to match and schedule a session for a student."""
    # Skip if already processed (by unique key: student email + subject)
    key = f"{student_req['email']}_{student_req['subject']}"
    if any(s["key"] == key for s in STATE.get("scheduled_sessions", [])):
        return

    tutors = find_matching_tutors(student_req)
    if not tutors:
        _post(f"No tutor found for {student_req['subject']}", "info")
        return

    for tutor in tutors:
        # Find overlapping times
        available = get_free_slots(tutor, student_req.get("date"))
        if not available:
            continue
        # Pick the first available slot
        start_time = available[0]
        duration = student_req.get("duration_minutes", 60)

        # Create event via chosen provider
        link = None
        if CFG["scheduling_provider"] == "google":
            cal_id = tutor.get("calendar_id", CFG["google"]["calendar_id"])
            link = create_google_event(tutor, student_req, start_time, duration, cal_id)
        elif CFG["scheduling_provider"] == "calendly":
            link = book_calendly_slot(tutor, start_time, student_req)
        else:
            _post("No scheduling provider configured.", "error")
            return

        if not link:
            continue

        # Calculate fee
        tutor_rate = tutor["rate_per_hour"]
        total_amount = tutor_rate * (duration / 60) * (1 + CFG["bot_fee_percent"] / 100)
        fee = total_amount - tutor_rate * (duration / 60)

        # Create Stripe payment intent if enabled
        session_id = str(uuid.uuid4())
        client_secret = create_stripe_payment(student_req, total_amount, tutor["name"], session_id)

        # Log the scheduled session
        STATE.setdefault("scheduled_sessions", []).append({
            "key": key,
            "student": student_req["student_name"],
            "tutor": tutor["name"],
            "subject": student_req["subject"],
            "start_time": start_time.isoformat(),
            "duration": duration,
            "total_amount": round(total_amount, 2),
            "bot_fee": round(fee, 2),
            "stripe_client_secret": client_secret,
            "event_link": link
        })
        save_state(STATE)

        _post(f"🗓️ Scheduled: {student_req['student_name']} with {tutor['name']} "
              f"for {student_req['subject']} at {start_time}. Fee: ${total_amount:.2f}",
              "info", {"client_secret": client_secret})

def main():
    wait_for_hub()
    _post("Tutoring Scheduler Bot online. Watching for student requests...", "info")

    while True:
        # Process each student request from config (can be refreshed dynamically)
        cfg = load_config()
        requests_list = cfg.get("student_requests", [])
        for req in requests_list:
            process_student_request(req)
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `tutoring_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "bot_fee_percent": 15.0,
  "scheduling_provider": "google",
  "google": {
    "service_account_file": "google_service_account.json",
    "calendar_id": "primary"
  },
  "calendly": {
    "token": "YOUR_CALENDLY_TOKEN",
    "user_uri": "https://api.calendly.com/users/YOUR_USER_UUID"
  },
  "stripe": {
    "enabled": true,
    "secret_key": "sk_test_...",
    "public_key": "pk_test_..."
  },
  "tutors": [
    {
      "name": "Dr. Alice",
      "email": "alice@university.edu",
      "subjects": ["Calculus", "Linear Algebra"],
      "rate_per_hour": 50.0,
      "calendar_id": "primary"
    }
  ],
  "student_requests": [
    {
      "student_name": "Bob",
      "email": "bob@student.edu",
      "subject": "Calculus",
      "preferred_times": ["2026-06-01T14:00:00Z"],
      "duration_minutes": 60
    }
  ]
}
"""
