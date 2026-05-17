#!/usr/bin/env python3
"""
social_calendar_bot.py — Birthday/Holiday Schedule Memory Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Manages your entire social calendar: contacts, birthdays, holidays,
anniversaries.  14 days before an event it posts a reminder to the
BotController hub along with a clickable Amazon gift‑search link.

Web interface for manual input; also accepts JSON POST to /add_contact
and /add_event.

Requirements:
    pip install flask requests

Configuration:
    On first run a file `social_calendar_config.json` is created.
    Add your Amazon Associate tag (optional) to monetise gift links.
"""

import json
import os
import time
import threading
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, render_template_string, request, redirect, url_for

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "social_calendar"
BOT_NAME = "Social Calendar"

CFG_FILE = Path(__file__).with_name("social_calendar_config.json")
CONTACTS_FILE = Path(__file__).with_name("social_contacts.json")
EVENTS_FILE   = Path(__file__).with_name("social_events.json")

DEFAULT_CONFIG = {
    "web_port": 5069,
    "amazon_affiliate_tag": "",          # optional – e.g. "yourtag-20"
    "check_interval_minutes": 60,        # how often to scan for upcoming events
    "reminder_lead_days": 14,            # default 14 days in advance
    "gift_search_keywords": ["gift", "present"]
}

# ── Hub helpers ─────────────────────────────────────────────────────────────
def post_to_hub(summary, level="info", payload=None):
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id":   BOT_ID,
            "bot_name": BOT_NAME,
            "summary":  summary,
            "level":    level,
            "payload":  payload or {},
        }, timeout=5)
    except Exception:
        pass

def wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Data I/O ────────────────────────────────────────────────────────────────
def load_json(filepath, default=[]):
    if filepath.exists():
        with open(filepath, "r") as f:
            return json.load(f)
    return default

def save_json(filepath, data):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

# ── Date helpers ────────────────────────────────────────────────────────────
def parse_birthday(bday_str):
    """Try to parse birthday string as YYYY-MM-DD or MM-DD. Returns (month, day, year or None)."""
    for fmt in ("%Y-%m-%d", "%m-%d"):
        try:
            dt = datetime.strptime(bday_str, fmt)
            return dt.month, dt.day, dt.year if fmt.startswith("%Y") else None
        except ValueError:
            continue
    return None, None, None

def next_occurrence(month, day, year_hint=None):
    """Return the date of the next occurrence of month/day starting from today.
    If year_hint is given, we treat it as annual for the current/future year."""
    today = date.today()
    candidate = date(today.year, month, day)
    if candidate < today:
        candidate = date(today.year + 1, month, day)
    return candidate

# ── Amazon gift link builder ────────────────────────────────────────────────
def gift_search_link(contact, config):
    """Build an Amazon search URL with affiliate tag and keywords based on contact interests."""
    tag = config.get("amazon_affiliate_tag", "").strip()
    base = "https://www.amazon.com/s?k="
    # Combine name, notes, interests into a search string
    parts = []
    if contact.get("name"):
        parts.append(contact["name"])
    if contact.get("interests"):
        parts.append(contact["interests"])
    if contact.get("notes"):
        parts.append(contact["notes"])
    # Fallback generic gift words
    if not parts:
        parts = config.get("gift_search_keywords", ["gift"])
    query = " ".join(parts)[:300]  # reasonable length
    url = base + quote(query)
    if tag:
        url += f"&tag={tag}"
    return url

# ── Reminder logic ──────────────────────────────────────────────────────────
def check_upcoming_events(config):
    """Scan contacts and events, post reminders for those within the lead window."""
    lead_days = config.get("reminder_lead_days", 14)
    today = date.today()
    reminder_date = today + timedelta(days=lead_days)

    contacts = load_json(CONTACTS_FILE, [])
    events = load_json(EVENTS_FILE, [])

    # Process contacts (birthdays)
    for contact in contacts:
        bday = contact.get("birthday", "").strip()
        if not bday:
            continue
        month, day, year = parse_birthday(bday)
        if not month or not day:
            continue
        next_bday = next_occurrence(month, day)
        if next_bday == reminder_date:
            name = contact.get("name", "Someone")
            gift_url = gift_search_link(contact, config)
            summary = f"🎂 {lead_days} days until {name}'s birthday! Gift ideas: {gift_url}"
            post_to_hub(summary, "warning", {
                "contact": name,
                "birthday": bday,
                "reminder_date": reminder_date.isoformat(),
                "gift_search_url": gift_url
            })

    # Process events (one‑time)
    for event in events:
        event_date_str = event.get("date", "")
        try:
            event_date = datetime.strptime(event_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if event_date == reminder_date:
            desc = event.get("description", "Event")
            name = event.get("name", "Unnamed")
            summary = f"📅 {lead_days} days until {name} – {desc}"
            post_to_hub(summary, "info", {"event": name, "date": event_date_str})

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

INDEX_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Social Calendar</title>
<style>
  body { font-family:Arial; max-width:800px; margin:40px auto; background:#fafafa; color:#222; }
  h1 { color:#c44569; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input[type=text], input[type=date], textarea { width:100%; padding:8px; margin:4px 0 10px; border:1px solid #ccc; border-radius:4px; }
  button { background:#c44569; color:white; padding:10px 20px; border:none; border-radius:6px; cursor:pointer; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px; border-bottom:1px solid #eee; }
  a { color:#c44569; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>📅 Social Calendar Memory</h1>
<p>Manage birthdays, anniversaries, holidays. Alerts will fire 14 days ahead.</p>

<div class="card">
  <h2>Add Contact</h2>
  <form method="POST" action="/add_contact">
    <label>Name</label>
    <input type="text" name="name" required>
    <label>Birthday (YYYY-MM-DD or MM-DD)</label>
    <input type="text" name="birthday" placeholder="e.g. 1990-05-20 or 05-20" required>
    <label>Interests / Gift Hints</label>
    <input type="text" name="interests" placeholder="e.g. books, gardening">
    <label>Notes</label>
    <textarea name="notes" rows="2"></textarea>
    <button type="submit">Save Contact</button>
  </form>
</div>

<div class="card">
  <h2>Add Event / Holiday</h2>
  <form method="POST" action="/add_event">
    <label>Event Name</label>
    <input type="text" name="name" required>
    <label>Date</label>
    <input type="date" name="date" required>
    <label>Description</label>
    <input type="text" name="description">
    <button type="submit">Save Event</button>
  </form>
</div>

<h2>Contacts</h2>
<table>
<tr><th>Name</th><th>Birthday</th><th>Interests</th><th>Next Occurrence</th></tr>
{% for c in contacts %}
  <tr>
    <td>{{ c.name }}</td>
    <td>{{ c.birthday }}</td>
    <td>{{ c.interests }}</td>
    <td>{{ c.next_bday }}</td>
  </tr>
{% endfor %}
</table>

<h2>Upcoming Events (next 30 days)</h2>
<table>
<tr><th>Name</th><th>Date</th><th>Description</th></tr>
{% for e in upcoming_events %}
  <tr>
    <td>{{ e.name }}</td>
    <td>{{ e.date }}</td>
    <td>{{ e.description }}</td>
  </tr>
{% endfor %}
</table>

<p class="small">Gift links include Amazon affiliate tag (if configured).</p>
</body>
</html>
"""

@app.route("/")
def index():
    contacts = load_json(CONTACTS_FILE, [])
    events = load_json(EVENTS_FILE, [])
    # Compute next birthday for display
    today = date.today()
    for c in contacts:
        bday = c.get("birthday", "")
        month, day, _ = parse_birthday(bday)
        if month and day:
            next_bday = next_occurrence(month, day)
            c["next_bday"] = next_bday.isoformat()
        else:
            c["next_bday"] = "unknown"
    # Upcoming events within 30 days
    upcoming = []
    for e in events:
        try:
            ev_date = datetime.strptime(e["date"], "%Y-%m-%d").date()
            if today <= ev_date <= today + timedelta(days=30):
                upcoming.append(e)
        except:
            pass
    upcoming.sort(key=lambda x: x["date"])
    return render_template_string(INDEX_TEMPLATE,
                                  contacts=contacts,
                                  upcoming_events=upcoming)

@app.route("/add_contact", methods=["POST"])
def add_contact():
    name = request.form.get("name", "").strip()
    birthday = request.form.get("birthday", "").strip()
    interests = request.form.get("interests", "").strip()
    notes = request.form.get("notes", "").strip()
    if not name or not birthday:
        return "Name and birthday required", 400
    contacts = load_json(CONTACTS_FILE, [])
    contacts.append({
        "name": name,
        "birthday": birthday,
        "interests": interests,
        "notes": notes
    })
    save_json(CONTACTS_FILE, contacts)
    post_to_hub(f"➕ Contact added: {name}, birthday {birthday}", "info")
    return redirect(url_for("index"))

@app.route("/add_event", methods=["POST"])
def add_event():
    name = request.form.get("name", "").strip()
    date_str = request.form.get("date", "").strip()
    description = request.form.get("description", "").strip()
    if not name or not date_str:
        return "Name and date required", 400
    events = load_json(EVENTS_FILE, [])
    events.append({
        "name": name,
        "date": date_str,
        "description": description
    })
    save_json(EVENTS_FILE, events)
    post_to_hub(f"📅 Event added: {name} on {date_str}", "info")
    return redirect(url_for("index"))

# ── Background scanner thread ──────────────────────────────────────────────
def scanner_loop(config):
    interval = config.get("check_interval_minutes", 60) * 60
    # Run immediately once
    check_upcoming_events(config)
    while True:
        time.sleep(interval)
        check_upcoming_events(config)

# ── Entry point ────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config created at {CFG_FILE}. Add your Amazon Affiliate tag for gift links.",
            "warning"
        )
        # Initialize empty files
        save_json(CONTACTS_FILE, [])
        save_json(EVENTS_FILE, [])

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config

    # Start scanner background thread
    threading.Thread(target=scanner_loop, args=(config,), daemon=True).start()

    # Heartbeat thread
    def heartbeat():
        while True:
            time.sleep(20)
            try:
                requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
                    "bot_name": BOT_NAME,
                    "status": "online",
                }, timeout=3)
            except Exception:
                pass
    threading.Thread(target=heartbeat, daemon=True).start()

    port = config.get("web_port", 5069)
    post_to_hub(f"📅 Social Calendar Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
