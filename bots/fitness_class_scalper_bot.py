#!/usr/bin/env python3
"""
fitness_class_scalper_bot.py — Fitness Class Capacity‑Booking & Resale Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Targets in‑demand fitness classes (SoulCycle, Barry’s, Solidcore, hot yoga,
reformer Pilates) in major cities where spots sell out instantly.

Uses the Mindbody Public API to monitor studio schedules, automatically
books a spot the instant a cancellation happens (using stored user accounts),
and then relists the booking at a markup on a local secondary marketplace
web page.

Real API calls – no simulation. You must obtain a Mindbody API key
(free from mindbodyonline.com) and add studio site IDs and user logins.

Requirements:
    pip install flask requests

Configuration:
    On first run, `class_scalper_config.json` is created.
    Fill in your Mindbody API key, studio IDs, user credentials, markup, etc.
"""

import json
import time
import threading
import uuid
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime, timedelta

import requests
from flask import Flask, render_template_string, request, redirect, url_for

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "fitness_class_scalper"
BOT_NAME = "Fitness Class Scalper"

CFG_FILE       = Path(__file__).with_name("class_scalper_config.json")
LISTINGS_FILE  = Path(__file__).with_name("resale_listings.json")

DEFAULT_CONFIG = {
    "mindbody": {
        "api_key": "",                     # ★ REQUIRED
        "site_ids": [                      # Add your target studio IDs
            {"site_id": -99, "name": "SoulCycle SoHo"},
            {"site_id": -99, "name": "Barry's Tribeca"},
            {"site_id": -99, "name": "Solidcore Union Square"},
        ],
        "class_names_to_watch": [
            "SoulCycle",
            "Barry's",
            "Solidcore",
            "Hot Power Yoga",
            "Reformer Pilates"
        ],
        "lookahead_hours": 48,            # how far ahead to scan
        "poll_interval_seconds": 30
    },
    "booking_accounts": [                 # multiple Mindbody accounts (username/password)
        {
            "email": "your_account1@gmail.com",
            "password": "your_password",
            "note": "primary account"
        },
        {
            "email": "your_account2@gmail.com",
            "password": "your_password",
            "note": "backup account"
        }
    ],
    "resale": {
        "markup_usd": 15,                 # how much extra to charge
        "web_port": 5067,
        "payment_instructions": "Send $ via Venmo @YourHandle or PayPal your@email.com",
        "buyer_notify_email": False,      # set True to email buyer booking details
        "smtp": {                         # optional, for email notifications
            "server": "smtp.gmail.com",
            "port": 587,
            "username": "you@gmail.com",
            "password": "app_password_here",
            "from_email": "you@gmail.com"
        }
    }
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

# ── Mindbody API helpers ────────────────────────────────────────────────────
MINDBODY_API = "https://api.mindbodyonline.com/public/v6"

class MindbodyClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.headers = {
            "Content-Type": "application/json",
            "Api-Key": api_key,
        }

    def get_sites(self):
        # Not used but helpful for debug
        pass

    def get_classes(self, site_id, start_dt, end_dt):
        """Search classes within a time range. Returns list of class objects."""
        url = f"{MINDBODY_API}/class/classes"
        params = {
            "siteId": site_id,
            "StartDateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "EndDateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Limit": 100,
        }
        try:
            resp = requests.get(url, headers=self.headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("Classes", [])
        except Exception as e:
            post_to_hub(f"Mindbody API error for site {site_id}: {e}", "error")
            return []

    def get_class_visits(self, class_id):
        """Get class details including enrollment count."""
        url = f"{MINDBODY_API}/class/classes/{class_id}"
        try:
            resp = requests.get(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def get_user_token(self, username, password):
        """Authenticate a user and get a token for booking."""
        url = f"{MINDBODY_API}/user/token"
        payload = {
            "Username": username,
            "Password": password,
        }
        try:
            resp = requests.post(url, headers=self.headers, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("AccessToken")
        except Exception as e:
            post_to_hub(f"Login failed for {username}: {e}", "error")
            return None

    def book_client_to_class(self, access_token, class_id):
        """Book the authenticated user into a class. Returns success bool."""
        url = f"{MINDBODY_API}/class/AddClientToClass"
        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {access_token}"
        payload = {
            "ClassId": class_id,
            "ClientId": "me",       # books the authenticated user
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            post_to_hub(f"Booking failed for class {class_id}: {e}", "error")
            return False

# ── Resale listings storage ─────────────────────────────────────────────────
class ListingsManager:
    def __init__(self, filepath):
        self.filepath = filepath
        if not self.filepath.exists():
            with open(self.filepath, 'w') as f:
                json.dump({}, f)

    def load(self):
        with open(self.filepath, 'r') as f:
            return json.load(f)

    def save(self, data):
        with open(self.filepath, 'w') as f:
            json.dump(data, f, indent=2)

    def add_listing(self, listing):
        data = self.load()
        listing_id = listing['id']
        data[listing_id] = listing
        self.save(data)

    def get_listing(self, listing_id):
        return self.load().get(listing_id)

    def remove_listing(self, listing_id):
        data = self.load()
        if listing_id in data:
            del data[listing_id]
            self.save(data)

# ── Booking & Resale orchestrator ───────────────────────────────────────────
class Scalper:
    def __init__(self, config):
        self.config = config
        self.client = MindbodyClient(config["mindbody"]["api_key"])
        self.listings = ListingsManager(LISTINGS_FILE)
        self.watched_classes = {}  # class_id -> {full: bool, last_seen: time}
        self.lock = threading.Lock()

    def scan(self):
        """Look for classes that were previously full and now have an opening."""
        config = self.config
        mindbody = config["mindbody"]
        site_ids = [s["site_id"] for s in mindbody["site_ids"]]
        lookahead = mindbody["lookahead_hours"]
        now = datetime.utcnow()
        end = now + timedelta(hours=lookahead)
        class_names = [c.lower() for c in mindbody["class_names_to_watch"]]

        new_opportunities = []

        for site_id in site_ids:
            classes = self.client.get_classes(site_id, now, end)
            for cls in classes:
                class_name = cls.get("Name", "").lower()
                # Check if it's a watched class
                if not any(wanted in class_name for wanted in class_names):
                    continue

                class_id = cls["Id"]
                # Get current enrollment
                visits = self.client.get_class_visits(class_id)
                if not visits:
                    continue
                total_booked = visits.get("TotalBooked", 0)
                max_capacity = visits.get("MaxCapacity", 0)
                is_full = (total_booked >= max_capacity) if max_capacity > 0 else False

                with self.lock:
                    prev = self.watched_classes.get(class_id, {"full": True})  # assume full by default
                    # If it was full and now has space, it's an opportunity
                    if prev.get("full", True) and not is_full:
                        # Spots opened! Try to book immediately
                        spot_opened = True
                    else:
                        spot_opened = False
                    # Update memory
                    self.watched_classes[class_id] = {"full": is_full, "last_seen": time.time()}

                if spot_opened:
                    # Attempt to book with the first available account
                    booking_success = self._auto_book(class_id, visits)
                    if booking_success:
                        opportunity = {
                            "class_id": class_id,
                            "site_id": site_id,
                            "class_name": visits.get("Name", ""),
                            "start_time": visits.get("StartDateTime", ""),
                            "end_time": visits.get("EndDateTime", ""),
                            "instructor": visits.get("Staff", {}).get("Name", ""),
                            "location": self._get_site_name(site_id),
                            "booked_at": datetime.utcnow().isoformat(),
                        }
                        # Create resale listing
                        listing = self._create_resale_listing(opportunity)
                        new_opportunities.append(listing)

        return new_opportunities

    def _get_site_name(self, site_id):
        for site in self.config["mindbody"]["site_ids"]:
            if site["site_id"] == site_id:
                return site["name"]
        return f"Studio {site_id}"

    def _auto_book(self, class_id, class_details):
        """Try to book the class using each configured account until one succeeds."""
        accounts = self.config.get("booking_accounts", [])
        for acc in accounts:
            username = acc["email"]
            password = acc["password"]
            token = self.client.get_user_token(username, password)
            if token:
                success = self.client.book_client_to_class(token, class_id)
                if success:
                    post_to_hub(
                        f"🎟️ Booked class {class_details.get('Name')} with {username}",
                        "info"
                    )
                    return True
                else:
                    post_to_hub(
                        f"Booking failed for {username} (maybe already booked, or no credits)",
                        "warning"
                    )
            else:
                post_to_hub(f"Could not authenticate {username}", "warning")
        post_to_hub("No accounts could book the class.", "error")
        return False

    def _create_resale_listing(self, opportunity):
        """Generate a unique listing ID and store it."""
        listing_id = str(uuid.uuid4())[:8]
        markup = self.config["resale"]["markup_usd"]
        price = markup
        listing = {
            "id": listing_id,
            "class_name": opportunity["class_name"],
            "start_time": opportunity["start_time"],
            "end_time": opportunity.get("end_time", ""),
            "location": opportunity["location"],
            "instructor": opportunity.get("instructor", ""),
            "price": price,
            "booked": True,
            "sold": False,
            "buyer_email": None,
        }
        self.listings.add_listing(listing)
        post_to_hub(
            f"💰 Resale listing created: {listing['class_name']} at {listing['location']} – ${price}",
            "warning",
            listing
        )
        return listing

# ── Flask web marketplace ──────────────────────────────────────────────────
app = Flask(__name__)
app.config["scalper"] = None
app.config["config"] = {}

INDEX_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Class Resale Marketplace</title>
<style>
  body { font-family: Arial; max-width: 700px; margin: 40px auto; background: #f8f9fa; color: #222; }
  h1 { color: #2c3e50; }
  .card { background: #fff; padding: 20px; margin: 15px 0; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
  .listing { border-left: 4px solid #27ae60; padding: 10px; margin: 10px 0; background: #f0faf3; }
  button { background: #2c3e50; color: white; padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; }
  input { padding: 8px; margin: 5px; }
  .sold { border-left-color: #e74c3c; background: #fdf2f2; }
  .small { font-size: 0.9em; color: #777; }
  a { color: #2c3e50; }
</style>
</head>
<body>
<h1>🎟️ Hot Fitness Class Resale</h1>
<p>Last‑minute spots for sold‑out classes, at a small markup. First come, first served.</p>

{% if listings %}
  {% for listing in listings %}
    <div class="card listing {% if listing.sold %}sold{% endif %}">
      <h3>{{ listing.class_name }}</h3>
      <p><strong>When:</strong> {{ listing.start_time[:16] }}</p>
      <p><strong>Where:</strong> {{ listing.location }} | <em>{{ listing.instructor }}</em></p>
      <p><strong>Price:</strong> ${{ listing.price }} (via {{ payment_instructions }})</p>
      {% if not listing.sold %}
        <form method="POST" action="/buy/{{ listing.id }}">
          <input type="email" name="email" placeholder="Your email to receive details" required>
          <button type="submit">Buy & Get Spot</button>
        </form>
      {% else %}
        <p class="sold"><strong>SPOT SOLD</strong></p>
      {% endif %}
    </div>
  {% endfor %}
{% else %}
  <p>No spots available right now. Check back soon!</p>
{% endif %}
</body>
</html>
"""

@app.route("/")
def home():
    data = app.config["scalper"].listings.load()
    listings = list(data.values())
    return render_template_string(INDEX_TEMPLATE,
                                  listings=listings,
                                  payment_instructions=app.config["config"]["resale"]["payment_instructions"])

@app.route("/buy/<listing_id>", methods=["POST"])
def buy(listing_id):
    listing = app.config["scalper"].listings.get_listing(listing_id)
    if not listing or listing.get("sold"):
        return "Spot not available anymore.", 400
    email = request.form.get("email", "").strip()
    if not email:
        return "Email required.", 400
    # Mark as sold
    listing["sold"] = True
    listing["buyer_email"] = email
    app.config["scalper"].listings.add_listing(listing)
    post_to_hub(
        f"✅ Spot sold to {email}: {listing['class_name']} at {listing['location']}",
        "info",
        listing
    )
    # Send email with booking details (optional)
    if app.config["config"]["resale"].get("buyer_notify_email"):
        send_booking_email(email, listing, app.config["config"])
    return f"<h2>Thank you! You purchased {listing['class_name']}. We'll send the booking info to {email} shortly.</h2><a href='/'>Back to marketplace</a>"

# ── Email helper ────────────────────────────────────────────────────────────
def send_booking_email(to_email, listing, config):
    smtp = config["resale"]["smtp"]
    subject = f"Your spot for {listing['class_name']} is confirmed!"
    body = f"""You successfully purchased a spot for:
    Class: {listing['class_name']}
    Date/Time: {listing['start_time']}
    Location: {listing['location']}
    Instructor: {listing.get('instructor', 'TBA')}

    Please show this email at the studio to check in.

    Enjoy your workout!
    """
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = smtp['from_email']
    msg['To'] = to_email
    try:
        with smtplib.SMTP(smtp['server'], smtp['port'], timeout=10) as server:
            server.starttls()
            server.login(smtp['username'], smtp['password'])
            server.send_message(msg)
    except Exception as e:
        post_to_hub(f"Failed to send email to {to_email}: {e}", "error")

# ── Main loop ──────────────────────────────────────────────────────────────
def run_scan_loop(scalper, config):
    interval = config["mindbody"]["poll_interval_seconds"]
    while True:
        try:
            new = scalper.scan()
            if new:
                post_to_hub(f"🔄 Scan found {len(new)} new resale opportunities.")
        except Exception as e:
            post_to_hub(f"Scan error: {e}", "error")
        time.sleep(interval)

# ── Entry point ────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config created at {CFG_FILE}. Add Mindbody API key, studio IDs, and user accounts.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    scalper = Scalper(config)
    app.config["scalper"] = scalper
    app.config["config"] = config

    # Start scanning in background thread
    threading.Thread(target=run_scan_loop, args=(scalper, config), daemon=True).start()

    # Heartbeat
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

    port = config["resale"]["web_port"]
    post_to_hub(f"🏋️ Class Scalper live at http://localhost:{port}", "info")
    import webbrowser
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
