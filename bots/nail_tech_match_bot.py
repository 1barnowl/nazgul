#!/usr/bin/env python3
"""
nail_tech_match_bot.py — Nail Tech Match Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Connects users with nearby nail techs and mobile manicurists.
Users search by location and service, book a slot, and pay a
$5 booking fee (you earn on every booking). Nail techs can
register with their schedule.

Requirements:
    pip install flask requests

Configuration:
    A file `nail_match_config.json` is created on first run.
    Edit the booking fee, payment instructions, and sample techs.
"""

import json
import time
import threading
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, render_template_string, request, redirect, url_for

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "nail_tech_match"
BOT_NAME = "Nail Tech Match"

CFG_FILE    = Path(__file__).with_name("nail_match_config.json")
TECHS_FILE  = Path(__file__).with_name("nail_techs.json")
BOOKINGS_FILE = Path(__file__).with_name("nail_bookings.json")

DEFAULT_CONFIG = {
    "web_port": 5077,
    "booking_fee_usd": 5.00,
    "payment_instructions": "Send $5 via Venmo @NailMatch to confirm your spot.",
    "techs": [
        {
            "name": "Polished by Paige",
            "location": "Austin, TX",
            "zip_codes": ["78701", "78702", "78703"],
            "mobile": True,
            "services": ["manicure", "gel manicure", "pedicure"],
            "price_min": 35,
            "price_max": 75,
            "schedule": {
                "Monday":    [{"start": "09:00", "end": "17:00"}],
                "Tuesday":   [{"start": "10:00", "end": "18:00"}],
                "Wednesday": [{"start": "09:00", "end": "17:00"}],
                "Thursday":  [{"start": "10:00", "end": "19:00"}],
                "Friday":    [{"start": "09:00", "end": "15:00"}]
            }
        },
        {
            "name": "Luxe Nails by Mia",
            "location": "Chicago, IL",
            "zip_codes": ["60601", "60602", "60603"],
            "mobile": False,
            "services": ["manicure", "dipping powder", "nail art"],
            "price_min": 40,
            "price_max": 100,
            "schedule": {
                "Monday":    [{"start": "11:00", "end": "18:00"}],
                "Wednesday": [{"start": "10:00", "end": "17:00"}],
                "Saturday":  [{"start": "09:00", "end": "15:00"}]
            }
        },
        {
            "name": "Traveling Nail Bar",
            "location": "Los Angeles, CA",
            "zip_codes": ["90001", "90002", "90003", "90004"],
            "mobile": True,
            "services": ["manicure", "pedicure", "gel", "acrylic"],
            "price_min": 50,
            "price_max": 120,
            "schedule": {
                "Tuesday":   [{"start": "10:00", "end": "17:00"}],
                "Thursday":  [{"start": "11:00", "end": "18:00"}],
                "Friday":    [{"start": "09:00", "end": "16:00"}],
                "Sunday":    [{"start": "10:00", "end": "14:00"}]
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
def get_tech_availability(tech, bookings, days_ahead=30):
    """Return dict mapping date string (YYYY-MM-DD) -> list of available 1‑hour slots."""
    today = date.today()
    schedule = tech.get("schedule", {})
    tech_id = tech["id"]
    available = {}

    for offset in range(days_ahead):
        d = today + timedelta(days=offset)
        day_name = d.strftime("%A")
        if day_name not in schedule:
            continue
        day_slots = schedule[day_name]
        date_str = d.isoformat()

        slots = []
        for block in day_slots:
            start_h, start_m = map(int, block["start"].split(":"))
            end_h, end_m = map(int, block["end"].split(":"))
            current = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m

            while current + 60 <= end_minutes:
                slot_str = f"{current // 60:02d}:{current % 60:02d}"
                slots.append(slot_str)
                current += 60   # 1-hour increments

        booked_times = []
        for b in bookings:
            if b["tech_id"] == tech_id and b["date"] == date_str and b.get("status") != "cancelled":
                booked_times.append(b["time_start"])

        available_slots = [s for s in slots if s not in booked_times]
        if available_slots:
            available[date_str] = available_slots

    return available

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

# ── HTML Templates ──────────────────────────────────────────────────────────
MAIN_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Nail Tech Match</title>
<style>
  body { font-family:Arial; max-width:800px; margin:40px auto; background:#fef9f5; color:#222; }
  h1 { color:#b2665c; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, select, button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:4px; }
  button { background:#b2665c; color:white; font-size:16px; cursor:pointer; }
  .tech-card { display:flex; justify-content:space-between; align-items:center; padding:12px 0; border-bottom:1px solid #eee; }
  .tech-info { flex:1; }
  .tech-info strong { font-size:1.1em; }
  .price { color:#888; }
  .mobile-tag { background:#b2665c; color:white; padding:2px 6px; border-radius:4px; font-size:0.8em; }
  a { color:#b2665c; text-decoration:underline; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>💅 Find Your Nail Tech</h1>
<p>Search by location and service. We'll match you with top nail artists near you.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Your Zip Code or City</label>
    <input type="text" name="location" value="{{ q.location }}" placeholder="e.g. 60601 or Austin" required>
    <label>Service</label>
    <select name="service">
      <option value="">All Services</option>
      <option value="manicure" {% if q.service=='manicure' %}selected{% endif %}>Manicure</option>
      <option value="gel" {% if q.service=='gel' %}selected{% endif %}>Gel Manicure</option>
      <option value="pedicure" {% if q.service=='pedicure' %}selected{% endif %}>Pedicure</option>
      <option value="acrylic" {% if q.service=='acrylic' %}selected{% endif %}>Acrylic</option>
      <option value="dipping powder" {% if q.service=='dipping powder' %}selected{% endif %}>Dipping Powder</option>
      <option value="nail art" {% if q.service=='nail art' %}selected{% endif %}>Nail Art</option>
    </select>
    <label>Mobile Only?</label>
    <select name="mobile">
      <option value="">Any</option>
      <option value="yes" {% if q.mobile=='yes' %}selected{% endif %}>Yes</option>
      <option value="no" {% if q.mobile=='no' %}selected{% endif %}>No</option>
    </select>
    <button type="submit">Find Nail Techs</button>
  </div>
</form>

{% if techs is defined %}
<div class="card">
  <h2>Matching Nail Techs ({{ techs|length }})</h2>
  {% for t in techs %}
  <div class="tech-card">
    <div class="tech-info">
      <strong>{{ t.name }}</strong> – {{ t.location }}
      {% if t.mobile %}<span class="mobile-tag">Mobile</span>{% endif %}
      <br/>
      <span class="price">Services: {{ t.services|join(', ') }} · ${{ t.price_min }} – ${{ t.price_max }}</span>
    </div>
    <a href="/tech/{{ t.id }}">View & Book</a>
  </div>
  {% endfor %}
  {% if techs|length == 0 %}<p>No techs found. Try a different area or service.</p>{% endif %}
</div>
{% endif %}

<p class="small"><a href="/register">Are you a nail tech? Register here.</a></p>
</body>
</html>"""

TECH_DETAIL_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{{ tech.name }} – Booking</title>
<style>
  body { font-family:Arial; max-width:700px; margin:40px auto; background:#fef9f5; }
  h2 { color:#b2665c; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, select, button { padding:10px; border:1px solid #ccc; border-radius:4px; width:100%; margin-bottom:10px; }
  button { background:#b2665c; color:white; font-size:16px; cursor:pointer; }
  .slot { display:inline-block; margin:5px; padding:8px 12px; background:#f4e9e9; border-radius:4px; cursor:pointer; }
  .slot.selected { background:#b2665c; color:white; }
  .hidden { display:none; }
  a { color:#b2665c; }
</style>
</head>
<body>
<a href="/">← Back to search</a>
<h2>{{ tech.name }}</h2>
<p>{{ tech.location }} · {% if tech.mobile %}Mobile{% else %}Salon{% endif %} · {{ tech.services|join(', ') }} · ${{ tech.price_min }}-{{ tech.price_max }}</p>

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
  <form method="POST" action="/book/{{ tech.id }}" onsubmit="return confirmBooking()">
    <input type="hidden" name="date" id="date_input">
    <input type="hidden" name="time" id="time_input">
    <label>Your Name</label><input type="text" name="client_name" required>
    <label>Your Email</label><input type="email" name="client_email" required>
    <label>Service</label>
    <select name="service" required>
      {% for svc in tech.services %}<option value="{{ svc }}">{{ svc.title() }}</option>{% endfor %}
    </select>
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
<style>body { font-family:Arial; text-align:center; padding:50px; background:#fef9f5; }</style></head>
<body>
<h2>💅 Your Nail Appointment is Almost Ready!</h2>
<p>Tech: <strong>{{ tech.name }}</strong></p>
<p>Service: {{ booking.service }}</p>
<p>Date: {{ booking.date }} at {{ booking.time_start }}</p>
<p>To finalise your booking, please send the <strong>${{ fee }} booking fee</strong> via:</p>
<p>{{ payment_instructions|replace('\n','<br/>') }}</p>
<p>We'll notify {{ tech.name }} once payment is confirmed. <a href="/">Back to search</a></p>
</body>
</html>"""

REGISTER_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Register as Nail Tech</title>
<style>
  body { font-family:Arial; max-width:700px; margin:40px auto; background:#fef9f5; }
  h2 { color:#b2665c; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, textarea, button { width:100%; padding:10px; margin:5px 0 10px; border:1px solid #ccc; border-radius:4px; }
  button { background:#b2665c; color:white; font-size:16px; cursor:pointer; }
  .day-row { display:flex; align-items:center; gap:10px; margin-bottom:8px; }
  .day-row input[type=checkbox] { width:auto; }
  .day-row label { margin:0; }
</style>
</head>
<body>
<h2>💅 Register as a Nail Tech</h2>
<form method="POST" action="/register">
  <div class="card">
    <label>Full Name / Business Name</label><input type="text" name="name" required>
    <label>City, State</label><input type="text" name="location" required>
    <label>Zip Codes You Serve (comma separated, for mobile)</label><input type="text" name="zip_codes" required>
    <label>Mobile Technician? <input type="checkbox" name="mobile" checked></label>
    <label>Services (comma separated, e.g. manicure, gel)</label><input type="text" name="services" required>
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
    <button type="submit">Register Tech</button>
  </div>
</form>
<a href="/">← Back</a>
</body>
</html>"""

# ── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    cfg = app.config["CFG"]
    techs = load_json(TECHS_FILE, [])
    q = {
        "location": request.args.get("location", "").strip().lower(),
        "service": request.args.get("service", "").strip().lower(),
        "mobile": request.args.get("mobile", "").strip()
    }

    filtered = techs[:]
    if q["location"]:
        # Simple location matching: check if location appears in tech's zip_codes or city name
        filtered = [t for t in filtered if (
            q["location"] in t.get("location", "").lower() or
            any(q["location"] in zip_code.lower() for zip_code in t.get("zip_codes", []))
        )]
    if q["service"]:
        filtered = [t for t in filtered if any(q["service"] in s.lower() for s in t.get("services", []))]
    if q["mobile"] == "yes":
        filtered = [t for t in filtered if t.get("mobile")]
    elif q["mobile"] == "no":
        filtered = [t for t in filtered if not t.get("mobile")]

    return render_template_string(MAIN_HTML, q=q, techs=filtered)

@app.route("/tech/<int:tech_id>")
def tech_detail(tech_id):
    cfg = app.config["CFG"]
    techs = load_json(TECHS_FILE, [])
    tech = next((t for t in techs if t["id"] == tech_id), None)
    if not tech:
        return "Tech not found", 404

    bookings = load_json(BOOKINGS_FILE, [])
    availability = get_tech_availability(tech, bookings)
    return render_template_string(TECH_DETAIL_HTML,
                                  tech=tech,
                                  availability=availability,
                                  availability_json=json.dumps(availability),
                                  booking_fee=cfg.get("booking_fee_usd", 5))

@app.route("/book/<int:tech_id>", methods=["POST"])
def book_appointment(tech_id):
    cfg = app.config["CFG"]
    techs = load_json(TECHS_FILE, [])
    tech = next((t for t in techs if t["id"] == tech_id), None)
    if not tech:
        return "Tech not found", 404

    date_str = request.form.get("date", "").strip()
    time_str = request.form.get("time", "").strip()
    client_name = request.form.get("client_name", "").strip()
    client_email = request.form.get("client_email", "").strip()
    service = request.form.get("service", "").strip()

    if not all([date_str, time_str, client_name, service]):
        return "Missing fields", 400

    bookings = load_json(BOOKINGS_FILE, [])
    # Check slot is still free
    for b in bookings:
        if b["tech_id"] == tech_id and b["date"] == date_str and b["time_start"] == time_str and b.get("status") != "cancelled":
            return "This slot just got booked. Please try another.", 409

    new_id = max([b["id"] for b in bookings], default=0) + 1
    booking = {
        "id": new_id,
        "tech_id": tech_id,
        "client_name": client_name,
        "client_email": client_email,
        "service": service,
        "date": date_str,
        "time_start": time_str,
        "time_end": (datetime.strptime(time_str, "%H:%M") + timedelta(hours=1)).strftime("%H:%M"),
        "status": "pending"
    }
    bookings.append(booking)
    save_json(BOOKINGS_FILE, bookings)

    fee = cfg.get("booking_fee_usd", 5)
    post_to_hub(
        f"📅 Booking for {tech['name']}: {client_name} – {service} on {date_str} at {time_str} (fee ${fee:.2f})",
        "info",
        {"tech": tech["name"], "client": client_name, "email": client_email, "date": date_str, "time": time_str}
    )

    return render_template_string(BOOKING_CONFIRM_HTML,
                                  tech=tech,
                                  booking=booking,
                                  fee=fee,
                                  payment_instructions=cfg.get("payment_instructions", ""))

@app.route("/register", methods=["GET", "POST"])
def register_tech():
    if request.method == "POST":
        techs = load_json(TECHS_FILE, [])
        # Parse fields
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "").strip()
        zip_codes_raw = request.form.get("zip_codes", "").strip()
        mobile = request.form.get("mobile") == "on"
        services_raw = request.form.get("services", "").strip()
        price_min = request.form.get("price_min", "").strip()
        price_max = request.form.get("price_max", "").strip()

        if not all([name, location, zip_codes_raw, services_raw, price_min, price_max]):
            return "All fields required.", 400

        try:
            pmin = float(price_min)
            pmax = float(price_max)
        except ValueError:
            return "Invalid price.", 400

        zip_codes = [z.strip() for z in zip_codes_raw.split(",") if z.strip()]
        services = [s.strip().lower() for s in services_raw.split(",") if s.strip()]

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

        new_id = max([t["id"] for t in techs], default=0) + 1
        tech = {
            "id": new_id,
            "name": name,
            "location": location,
            "zip_codes": zip_codes,
            "mobile": mobile,
            "services": services,
            "price_min": pmin,
            "price_max": pmax,
            "schedule": schedule
        }
        techs.append(tech)
        save_json(TECHS_FILE, techs)
        post_to_hub(f"➕ New nail tech registered: {name} ({location})", "info")
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
    if not TECHS_FILE.exists():
        default_techs = config.get("techs", [])
        techs_with_ids = []
        for i, t in enumerate(default_techs, 1):
            techs_with_ids.append({"id": i, **t})
        save_json(TECHS_FILE, techs_with_ids)
    if not BOOKINGS_FILE.exists():
        save_json(BOOKINGS_FILE, [])

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub("Config created. Sample nail techs and booking fee loaded.", "info")

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    initialize_files(config)
    start_heartbeat()

    port = config.get("web_port", 5077)
    post_to_hub(f"💅 Nail Tech Match live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
