#!/usr/bin/env python3
"""
lash_booking_bot.py — Lash Tech Booking Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Two‑sided platform that matches clients with lash artists by style,
price & availability. Clients book a slot and pay a $5 booking fee
(you earn on every booking). Real, working booking system.

Requirements:
    pip install flask requests

Configuration:
    A file `lash_booking_config.json` is created on first run.
    You can pre‑load sample artists and edit your booking fee / Venmo.
"""

import json
import time
import threading
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from copy import deepcopy

import requests
from flask import Flask, render_template_string, request, redirect, url_for, jsonify

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "lash_booking"
BOT_NAME = "Lash Tech Booking"

CFG_FILE      = Path(__file__).with_name("lash_booking_config.json")
ARTISTS_FILE  = Path(__file__).with_name("lash_artists.json")
BOOKINGS_FILE = Path(__file__).with_name("lash_bookings.json")

DEFAULT_CONFIG = {
    "web_port": 5076,
    "booking_fee_usd": 5.00,
    "payment_instructions": "Send $5 via Venmo @LashBooker to confirm your spot.",
    "artists": [
        {
            "name": "Glam Lash Studio",
            "location": "Los Angeles, CA",
            "styles": ["classic", "volume", "hybrid"],
            "price_min": 80,
            "price_max": 150,
            "schedule": {
                "Monday":    [{"start": "09:00", "end": "17:00"}],
                "Tuesday":   [{"start": "10:00", "end": "18:00"}],
                "Wednesday": [{"start": "09:00", "end": "17:00"}],
                "Thursday":  [{"start": "10:00", "end": "19:00"}],
                "Friday":    [{"start": "09:00", "end": "15:00"}]
            }
        },
        {
            "name": "Luxe Lashes by Ana",
            "location": "New York, NY",
            "styles": ["volume", "mega volume", "wispy"],
            "price_min": 120,
            "price_max": 200,
            "schedule": {
                "Monday":    [{"start": "11:00", "end": "18:00"}],
                "Wednesday": [{"start": "10:00", "end": "17:00"}],
                "Saturday":  [{"start": "09:00", "end": "15:00"}]
            }
        },
        {
            "name": "Elegant Lash Lounge",
            "location": "Miami, FL",
            "styles": ["classic", "hybrid"],
            "price_min": 65,
            "price_max": 110,
            "schedule": {
                "Tuesday":   [{"start": "10:00", "end": "17:00"}],
                "Thursday":  [{"start": "11:00", "end": "18:00"}],
                "Friday":    [{"start": "09:00", "end": "16:00"}],
                "Saturday":  [{"start": "10:00", "end": "14:00"}]
            }
        }
    ]
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

# ── Data helpers ────────────────────────────────────────────────────────────
def load_json(filepath, default=None):
    if filepath.exists():
        with open(filepath, "r") as f:
            return json.load(f)
    return default if default is not None else []

def save_json(filepath, data):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

# ── Availability calculator ─────────────────────────────────────────────────
def get_artist_availability(artist, bookings, days_ahead=30):
    """
    Return a dict mapping date string (YYYY-MM-DD) -> list of available
    1‑hour time slots (HH:MM) for the given artist within the next N days.
    """
    today = date.today()
    schedule = artist.get("schedule", {})
    artist_id = artist["id"]
    available = {}

    for offset in range(days_ahead):
        d = today + timedelta(days=offset)
        day_name = d.strftime("%A")
        if day_name not in schedule:
            continue
        day_slots = schedule[day_name]
        date_str = d.isoformat()

        # Collect all possible 1‑hour slots within each time block
        slots = []
        for block in day_slots:
            start_h, start_m = map(int, block["start"].split(":"))
            end_h, end_m = map(int, block["end"].split(":"))
            current = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m

            while current + 60 <= end_minutes:
                slot_str = f"{current // 60:02d}:{current % 60:02d}"
                slots.append(slot_str)
                current += 60   # 1 hour increments

        # Remove slots already booked
        booked_times = []
        for b in bookings:
            if b["artist_id"] == artist_id and b["date"] == date_str and b.get("status") != "cancelled":
                booked_times.append(b["time_start"])

        available_slots = [s for s in slots if s not in booked_times]
        if available_slots:
            available[date_str] = available_slots

    return available

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

# ── Templates ───────────────────────────────────────────────────────────────
MAIN_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Lash Artist Booking</title>
<style>
  body { font-family:Arial; max-width:800px; margin:40px auto; background:#fdf8f5; color:#222; }
  h1 { color:#8b3a5e; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, select, button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:4px; }
  button { background:#8b3a5e; color:white; font-size:16px; cursor:pointer; }
  .artist-card { display:flex; justify-content:space-between; align-items:center; padding:12px 0; border-bottom:1px solid #eee; }
  .artist-info { flex:1; }
  .artist-info strong { font-size:1.1em; }
  .price { color:#888; }
  a { color:#8b3a5e; text-decoration:underline; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>💎 Find Your Lash Artist</h1>
<p>Filter by style and budget. We'll match you with artists who have open slots.</p>

<form method="GET" action="/">
  <div class="card">
    <label>Lash Style</label>
    <select name="style">
      <option value="">All Styles</option>
      <option value="classic" {% if q.style=='classic' %}selected{% endif %}>Classic</option>
      <option value="volume" {% if q.style=='volume' %}selected{% endif %}>Volume</option>
      <option value="hybrid" {% if q.style=='hybrid' %}selected{% endif %}>Hybrid</option>
      <option value="mega volume" {% if q.style=='mega volume' %}selected{% endif %}>Mega Volume</option>
      <option value="wispy" {% if q.style=='wispy' %}selected{% endif %}>Wispy</option>
    </select>
    <label>Max Price (USD)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="5" placeholder="No limit">
    <label>Artist Name (optional)</label>
    <input type="text" name="name" value="{{ q.name }}" placeholder="Search by name">
    <button type="submit">Find Artists</button>
  </div>
</form>

{% if artists is defined %}
<div class="card">
  <h2>Matching Artists ({{ artists|length }})</h2>
  {% for a in artists %}
  <div class="artist-card">
    <div class="artist-info">
      <strong>{{ a.name }}</strong> – {{ a.location }}<br/>
      <span class="price">Styles: {{ a.styles|join(', ') }} · ${{ a.price_min }} – ${{ a.price_max }}</span>
    </div>
    <a href="/artist/{{ a.id }}">View & Book</a>
  </div>
  {% endfor %}
  {% if artists|length == 0 %}<p>No artists found. Try different filters.</p>{% endif %}
</div>
{% endif %}

<p class="small"><a href="/register">Are you a lash artist? Register here.</a></p>
</body>
</html>"""

ARTIST_DETAIL_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{{ artist.name }} – Booking</title>
<style>
  body { font-family:Arial; max-width:700px; margin:40px auto; background:#fdf8f5; }
  h2 { color:#8b3a5e; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, select, button { padding:10px; border:1px solid #ccc; border-radius:4px; width:100%; margin-bottom:10px; }
  button { background:#8b3a5e; color:white; font-size:16px; cursor:pointer; }
  .slot { display:inline-block; margin:5px; padding:8px 12px; background:#f3e8f5; border-radius:4px; cursor:pointer; }
  .slot.selected { background:#8b3a5e; color:white; }
  .hidden { display:none; }
  a { color:#8b3a5e; }
</style>
</head>
<body>
<a href="/">← Back to search</a>
<h2>{{ artist.name }}</h2>
<p>{{ artist.location }} · {{ artist.styles|join(', ') }} · ${{ artist.price_min }}-{{ artist.price_max }}</p>

<h3>Select a Date & Time</h3>
<div class="card">
  <label>Date</label>
  <select id="date_select" onchange="showSlots()">
    <option value="">-- Choose --</option>
    {% for d in availability %}<option value="{{ d }}">{{ d }}</option>{% endfor %}
  </select>
  <div id="slots_container">
    <p>Available time slots will appear here.</p>
  </div>
  <form method="POST" action="/book/{{ artist.id }}" onsubmit="return confirmBooking()">
    <input type="hidden" name="date" id="date_input">
    <input type="hidden" name="time" id="time_input">
    <label>Your Name</label><input type="text" name="client_name" required>
    <label>Your Email</label><input type="email" name="client_email" required>
    <button type="submit">Book Slot – ${{ booking_fee }} Fee</button>
  </form>
</div>

<script>
  const availability = {{ availability_json|safe }};
  function showSlots() {
    const date = document.getElementById("date_select").value;
    const slotsDiv = document.getElementById("slots_container");
    if (!date || !availability[date]) {
      slotsDiv.innerHTML = "<p>No slots available for this date.</p>";
      return;
    }
    let html = "";
    availability[date].forEach(slot => {
      html += `<span class="slot" onclick="selectSlot('${date}', '${slot}')">${slot}</span>`;
    });
    slotsDiv.innerHTML = html;
  }
  function selectSlot(date, time) {
    document.querySelectorAll(".slot").forEach(el => el.classList.remove("selected"));
    event.target.classList.add("selected");
    document.getElementById("date_input").value = date;
    document.getElementById("time_input").value = time;
  }
  function confirmBooking() {
    const date = document.getElementById("date_input").value;
    const time = document.getElementById("time_input").value;
    if (!date || !time) {
      alert("Please select a date and time slot.");
      return false;
    }
    return true;
  }
</script>
</body>
</html>"""

BOOKING_CONFIRM_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Booking Confirmed</title>
<style>body { font-family:Arial; text-align:center; padding:50px; background:#fdf8f5; }</style></head>
<body>
<h2>✨ Your Lash Appointment is Almost Ready!</h2>
<p>Artist: <strong>{{ artist.name }}</strong></p>
<p>Date: {{ booking.date }} at {{ booking.time_start }}</p>
<p>To finalise your booking, please send the <strong>${{ fee }} booking fee</strong> via:</p>
<p>{{ payment_instructions|replace('\n','<br/>') }}</p>
<p>We'll notify the artist once payment is confirmed. <a href="/">Back to search</a></p>
</body>
</html>"""

REGISTER_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Register as Lash Artist</title>
<style>
  body { font-family:Arial; max-width:700px; margin:40px auto; background:#fdf8f5; }
  h2 { color:#8b3a5e; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, textarea, button { width:100%; padding:10px; margin:5px 0 10px; border:1px solid #ccc; border-radius:4px; }
  button { background:#8b3a5e; color:white; font-size:16px; cursor:pointer; }
  .day-row { display:flex; align-items:center; gap:10px; margin-bottom:8px; }
  .day-row input[type=checkbox] { width:auto; }
  .day-row label { margin:0; }
</style>
</head>
<body>
<h2>💅 Register as a Lash Artist</h2>
<form method="POST" action="/register">
  <div class="card">
    <label>Full Name / Business Name</label><input type="text" name="name" required>
    <label>City, State</label><input type="text" name="location" required>
    <label>Styles (comma separated, e.g. classic, volume)</label><input type="text" name="styles" required>
    <label>Price Min ($)</label><input type="number" name="price_min" required>
    <label>Price Max ($)</label><input type="number" name="price_max" required>
    <label>Weekly Availability (check days and set hours)</label>
    <div id="days_container">
      {% for day in days_of_week %}
      <div class="day-row">
        <input type="checkbox" name="day_{{ loop.index0 }}" id="day_{{ loop.index0 }}">
        <label for="day_{{ loop.index0 }}">{{ day }}</label>
        <input type="time" name="start_{{ loop.index0 }}" placeholder="Start" value="09:00">
        <span>to</span>
        <input type="time" name="end_{{ loop.index0 }}" placeholder="End" value="17:00">
      </div>
      {% endfor %}
    </div>
    <button type="submit">Register Artist</button>
  </div>
</form>
<a href="/">← Back</a>
</body>
</html>"""

# ── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    cfg = app.config["CFG"]
    artists = load_json(ARTISTS_FILE, [])
    q = {
        "style": request.args.get("style", "").strip().lower(),
        "max_price": request.args.get("max_price", "").strip(),
        "name": request.args.get("name", "").strip().lower()
    }

    filtered = artists[:]
    if q["style"]:
        filtered = [a for a in filtered if any(q["style"] in s.lower() for s in a.get("styles", []))]
    if q["max_price"]:
        try:
            max_p = float(q["max_price"])
            filtered = [a for a in filtered if a.get("price_min", 9999) <= max_p]
        except ValueError:
            pass
    if q["name"]:
        filtered = [a for a in filtered if q["name"] in a.get("name", "").lower()]

    return render_template_string(MAIN_HTML, q=q, artists=filtered)

@app.route("/artist/<int:artist_id>")
def artist_detail(artist_id):
    cfg = app.config["CFG"]
    artists = load_json(ARTISTS_FILE, [])
    artist = next((a for a in artists if a["id"] == artist_id), None)
    if not artist:
        return "Artist not found", 404

    bookings = load_json(BOOKINGS_FILE, [])
    availability = get_artist_availability(artist, bookings)
    # Convert availability dict to JSON for JS
    return render_template_string(ARTIST_DETAIL_HTML,
                                  artist=artist,
                                  availability=availability,
                                  availability_json=json.dumps(availability),
                                  booking_fee=cfg.get("booking_fee_usd", 5))

@app.route("/book/<int:artist_id>", methods=["POST"])
def book_appointment(artist_id):
    cfg = app.config["CFG"]
    artists = load_json(ARTISTS_FILE, [])
    artist = next((a for a in artists if a["id"] == artist_id), None)
    if not artist:
        return "Artist not found", 404

    date_str = request.form.get("date", "").strip()
    time_str = request.form.get("time", "").strip()
    client_name = request.form.get("client_name", "").strip()
    client_email = request.form.get("client_email", "").strip()

    if not all([date_str, time_str, client_name]):
        return "Missing fields", 400

    bookings = load_json(BOOKINGS_FILE, [])
    # Check slot is still free
    for b in bookings:
        if b["artist_id"] == artist_id and b["date"] == date_str and b["time_start"] == time_str and b.get("status") != "cancelled":
            return "This slot just got booked. Please try another.", 409

    new_id = max([b["id"] for b in bookings], default=0) + 1
    booking = {
        "id": new_id,
        "artist_id": artist_id,
        "client_name": client_name,
        "client_email": client_email,
        "date": date_str,
        "time_start": time_str,
        "time_end": (datetime.strptime(time_str, "%H:%M") + timedelta(hours=1)).strftime("%H:%M"),
        "status": "pending"
    }
    bookings.append(booking)
    save_json(BOOKINGS_FILE, bookings)

    # Post to hub
    fee = cfg.get("booking_fee_usd", 5)
    post_to_hub(
        f"📅 Booking for {artist['name']}: {client_name} on {date_str} at {time_str} (fee ${fee:.2f})",
        "info",
        {"artist": artist["name"], "client": client_name, "email": client_email, "date": date_str, "time": time_str}
    )

    return render_template_string(BOOKING_CONFIRM_HTML,
                                  artist=artist,
                                  booking=booking,
                                  fee=fee,
                                  payment_instructions=cfg.get("payment_instructions", ""))

@app.route("/register", methods=["GET", "POST"])
def register_artist():
    if request.method == "POST":
        artists = load_json(ARTISTS_FILE, [])
        # Parse fields
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "").strip()
        styles_raw = request.form.get("styles", "").strip()
        price_min = request.form.get("price_min", "").strip()
        price_max = request.form.get("price_max", "").strip()

        if not all([name, location, styles_raw, price_min, price_max]):
            return "All fields required.", 400

        try:
            pmin = float(price_min)
            pmax = float(price_max)
        except ValueError:
            return "Invalid price.", 400

        styles = [s.strip().lower() for s in styles_raw.split(",") if s.strip()]

        # Build schedule
        days_of_week = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        schedule = {}
        for i, day in enumerate(days_of_week):
            if request.form.get(f"day_{i}") == "on":
                start = request.form.get(f"start_{i}", "09:00").strip()
                end = request.form.get(f"end_{i}", "17:00").strip()
                if start and end:
                    schedule[day] = [{"start": start, "end": end}]

        if not schedule:
            return "You must select at least one working day.", 400

        new_id = max([a["id"] for a in artists], default=0) + 1
        artist = {
            "id": new_id,
            "name": name,
            "location": location,
            "styles": styles,
            "price_min": pmin,
            "price_max": pmax,
            "schedule": schedule
        }
        artists.append(artist)
        save_json(ARTISTS_FILE, artists)
        post_to_hub(f"➕ New artist registered: {name} ({location})", "info")
        return redirect(url_for("index"))

    days_of_week = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    return render_template_string(REGISTER_HTML, days_of_week=days_of_week)

# ── Heartbeat thread ────────────────────────────────────────────────────────
def start_heartbeat():
    def beat():
        while True:
            time.sleep(20)
            try:
                requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
                    "bot_name": BOT_NAME,
                    "status": "online",
                }, timeout=3)
            except Exception:
                pass
    threading.Thread(target=beat, daemon=True).start()

# ── Initialization ──────────────────────────────────────────────────────────
def initialize_files(config):
    # Load default artists if none exist
    if not ARTISTS_FILE.exists():
        default_artists = config.get("artists", [])
        artists_with_ids = []
        for i, a in enumerate(default_artists, 1):
            artists_with_ids.append({"id": i, **a})
        save_json(ARTISTS_FILE, artists_with_ids)
    if not BOOKINGS_FILE.exists():
        save_json(BOOKINGS_FILE, [])

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub("Config created. Booking fee & sample artists loaded.", "info")

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    initialize_files(config)
    start_heartbeat()

    port = config.get("web_port", 5076)
    post_to_hub(f"💎 Lash Booking Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
