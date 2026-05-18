#!/usr/bin/env python3
"""
womens_fitness_class_bot.py — Women’s Fitness Class Match Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Matches users to real yoga, pilates, barre, and strength classes
via the Mindbody Public API.  Each booking includes a $5‑$15
booking commission – you earn on every reservation.

Requirements:
    pip install flask requests

Configuration:
    On first run a file `fitness_class_match_config.json` is created.
    Fill in your Mindbody API key (free from mindbodyonline.com) and
    the studio site IDs you want to list.
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
BOT_ID   = "womens_fitness_class_match"
BOT_NAME = "Women’s Fitness Class Match"

CFG_FILE     = Path(__file__).with_name("fitness_class_match_config.json")
BOOKINGS_FILE = Path(__file__).with_name("fitness_class_bookings.json")
CACHE_FILE    = Path(__file__).with_name("fitness_class_cache.json")

DEFAULT_CONFIG = {
    "web_port": 5081,
    "booking_fee_usd": 5.00,             # commission per booking
    "payment_instructions": "Send $ via Venmo @FitnessClassMatch to confirm your spot.",
    "mindbody": {
        "api_key": "",                   # ★ REQUIRED – get from developers.mindbodyonline.com
        "site_ids": [                    # replace with your chosen studio site IDs
            {"site_id": -99, "name": "YogaWorks SoHo", "location": "New York, NY"},
            {"site_id": -99, "name": "Barry's Tribeca", "location": "New York, NY"},
            {"site_id": -99, "name": "Solidcore Union Square", "location": "New York, NY"},
            {"site_id": -99, "name": "Pure Barre Chicago", "location": "Chicago, IL"},
            {"site_id": -99, "name": "CorePower Yoga Denver", "location": "Denver, CO"}
        ],
        "class_type_mapping": {           # maps user‑friendly types to Mindbody class name keywords
            "yoga": "yoga",
            "pilates": "pilates",
            "barre": "barre",
            "strength": "barrys|solidcore|strength|hiit"
        },
        "lookahead_hours": 72,
        "cache_ttl_minutes": 30
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

def fetch_classes(api_key, site_ids, lookahead_hours, class_type_keywords):
    """
    Calls Mindbody for each site_id, filters by class type keywords,
    returns list of class dicts.
    """
    if not api_key:
        return []
    headers = {
        "Content-Type": "application/json",
        "Api-Key": api_key,
    }
    now = datetime.utcnow()
    end = now + timedelta(hours=lookahead_hours)
    start_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    all_classes = []
    for site in site_ids:
        sid = site["site_id"]
        params = {
            "siteId": sid,
            "StartDateTime": start_str,
            "EndDateTime": end_str,
            "Limit": 200,
        }
        try:
            r = requests.get(f"{MINDBODY_API}/class/classes",
                             headers=headers, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            classes = data.get("Classes", [])
            # Filter by type keywords
            if class_type_keywords:
                keywords = class_type_keywords.lower().split("|")
                classes = [c for c in classes if any(
                    k in c.get("Name", "").lower() for k in keywords
                )]
            # Add site name and location for display
            for cls in classes:
                cls["site_name"] = site.get("name", str(sid))
                cls["site_location"] = site.get("location", "")
            all_classes.extend(classes)
        except Exception as e:
            post_to_hub(f"Mindbody error for site {sid}: {e}", "warning")
    return all_classes

def get_cached_classes(config):
    """Return cached classes if fresh, else fetch and cache."""
    cache = {}
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
    ttl_seconds = config.get("cache_ttl_minutes", 30) * 60
    now_ts = time.time()
    # use a separate cache per type? We'll use a single cache for all types for simplicity.
    # Store the full list of classes and the timestamp.
    if cache.get("timestamp", 0) + ttl_seconds > now_ts and "classes" in cache:
        return cache["classes"]
    # Fetch new
    mb_cfg = config.get("mindbody", {})
    api_key = mb_cfg.get("api_key", "").strip()
    site_ids = mb_cfg.get("site_ids", [])
    lookahead = mb_cfg.get("lookahead_hours", 72)
    # Fetch all classes unfiltered, we'll filter by type when searching.
    all_classes = fetch_classes(api_key, site_ids, lookahead, None)
    cache = {"timestamp": now_ts, "classes": all_classes}
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    return all_classes

# ── Booking storage ─────────────────────────────────────────────────────────
def load_bookings():
    if BOOKINGS_FILE.exists():
        with open(BOOKINGS_FILE, "r") as f:
            return json.load(f)
    return []

def save_bookings(bookings):
    with open(BOOKINGS_FILE, "w") as f:
        json.dump(bookings, f, indent=2)

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

TEMPLATE = """
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Women’s Fitness Class Match</title>
<style>
  body { font-family:Arial; max-width:800px; margin:40px auto; background:#f6f8fb; color:#222; }
  h1 { color:#2c5f8a; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  select, input[type=date], button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:4px; }
  button { background:#2c5f8a; color:white; cursor:pointer; font-size:16px; }
  .class-card { background:#eef4ff; padding:12px; margin:10px 0; border-left:4px solid #2c5f8a; border-radius:4px; display:flex; justify-content:space-between; align-items:center; }
  .class-info { flex:1; }
  .class-info strong { font-size:1.1em; }
  .price { color:#888; }
  a { color:#2c5f8a; font-weight:bold; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>🧘‍♀️ Find Your Perfect Fitness Class</h1>
<p>Search yoga, pilates, barre, and strength classes near you. Book and pay a small commission to confirm your spot.</p>

<form method="GET" action="/">
  <div class="card">
    <label>Studio / Location</label>
    <select name="site_name">
      <option value="">All Locations</option>
      {% for s in studios %}
        <option value="{{ s.name }}" {% if q.site_name == s.name %}selected{% endif %}>{{ s.name }} ({{ s.location }})</option>
      {% endfor %}
    </select>
    <label>Class Type</label>
    <select name="class_type">
      <option value="">All Types</option>
      <option value="yoga" {% if q.class_type == 'yoga' %}selected{% endif %}>Yoga</option>
      <option value="pilates" {% if q.class_type == 'pilates' %}selected{% endif %}>Pilates</option>
      <option value="barre" {% if q.class_type == 'barre' %}selected{% endif %}>Barre</option>
      <option value="strength" {% if q.class_type == 'strength' %}selected{% endif %}>Strength / HIIT</option>
    </select>
    <label>Date (optional)</label>
    <input type="date" name="date" value="{{ q.date }}">
    <button type="submit">Search Classes</button>
  </div>
</form>

{% if classes is defined %}
<div class="card">
  <h2>Available Classes ({{ classes|length }})</h2>
  {% for c in classes %}
  <div class="class-card">
    <div class="class-info">
      <strong>{{ c.Name }}</strong><br/>
      <span>{{ c.site_name }} ({{ c.site_location }})</span><br/>
      <span class="price">{{ c.StartDateTime[:16] }} – {{ c.EndDateTime[11:16] }} | Instructor: {{ c.Staff.get('Name','TBA') if c.Staff else 'TBA' }}</span>
    </div>
    <a href="/book/{{ c.Id }}">Book (${{ booking_fee }})</a>
  </div>
  {% endfor %}
  {% if classes|length == 0 %}<p>No classes match your search. Try different filters.</p>{% endif %}
</div>
{% endif %}

<p class="small">Booking fee ensures your reservation. You pay the studio separately.</p>
</body>
</html>
"""

BOOKING_PAGE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Book Class</title>
<style>
  body { font-family:Arial; max-width:600px; margin:40px auto; background:#f6f8fb; }
  h2 { color:#2c5f8a; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:4px; }
  button { background:#2c5f8a; color:white; cursor:pointer; font-size:16px; }
  .class-detail { background:#eef4ff; padding:12px; border-radius:4px; }
  a { color:#2c5f8a; }
</style>
</head>
<body>
<h2>Confirm Your Booking</h2>
<div class="class-detail">
  <p><strong>{{ class_info.Name }}</strong></p>
  <p>Studio: {{ class_info.site_name }} ({{ class_info.site_location }})</p>
  <p>Date: {{ class_info.StartDateTime[:16] }}</p>
  <p>Booking fee: ${{ "%.2f"|format(fee) }}</p>
</div>
<form method="POST">
  <label>Your Name</label><input type="text" name="client_name" required>
  <label>Your Email</label><input type="email" name="client_email" required>
  <p>After submitting, you'll receive payment instructions for the ${{ "%.2f"|format(fee) }} booking fee. Your spot is reserved once payment is confirmed.</p>
  <button type="submit">Submit Booking Request</button>
</form>
<a href="/">← Back to search</a>
</body>
</html>
"""

CONFIRMATION_PAGE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Booking Received</title>
<style>body { font-family:Arial; text-align:center; padding:50px; background:#f6f8fb; }</style>
</head>
<body>
<h2>🎉 Booking Request Received!</h2>
<p><strong>{{ class_name }}</strong></p>
<p>We'll reserve your spot once we receive the <strong>${{ "%.2f"|format(fee) }} booking fee</strong>.</p>
<p>Payment instructions: <br/>{{ payment_instructions|replace('\n','<br/>') }}</p>
<p>You will receive a confirmation email shortly.</p>
<a href="/">← Back to search</a>
</body>
</html>
"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    all_classes = get_cached_classes(cfg)
    studios = cfg["mindbody"]["site_ids"]

    # Search filters
    site_name = request.args.get("site_name", "").strip()
    class_type = request.args.get("class_type", "").strip().lower()
    date_str = request.args.get("date", "").strip()

    filtered = all_classes

    # Filter by studio name
    if site_name:
        filtered = [c for c in filtered if c.get("site_name") == site_name]

    # Filter by class type using keywords from mapping
    if class_type:
        mapping = cfg["mindbody"].get("class_type_mapping", {})
        keywords = mapping.get(class_type, class_type).lower()
        kw_list = keywords.split("|")
        filtered = [c for c in filtered if any(k in c.get("Name","").lower() for k in kw_list)]

    # Filter by date
    if date_str:
        try:
            filter_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            filtered = [c for c in filtered if datetime.fromisoformat(c.get("StartDateTime","")).date() == filter_date]
        except:
            pass

    # Limit to next 72 hours (already fetched only those, but just in case)
    # Sort by start time
    filtered.sort(key=lambda c: c.get("StartDateTime", ""))

    booking_fee = cfg.get("booking_fee_usd", 5.0)

    return render_template_string(TEMPLATE,
                                  q=request.args,
                                  studios=studios,
                                  classes=filtered,
                                  booking_fee=booking_fee)

@app.route("/book/<int:class_id>", methods=["GET", "POST"])
def book_class(class_id):
    cfg = app.config["CFG"]
    all_classes = get_cached_classes(cfg)
    class_info = next((c for c in all_classes if c["Id"] == class_id), None)
    if not class_info:
        return "Class not found or expired", 404

    fee = cfg.get("booking_fee_usd", 5.0)

    if request.method == "POST":
        client_name = request.form.get("client_name", "").strip()
        client_email = request.form.get("client_email", "").strip()
        if not client_name or not client_email:
            return "Name and email required", 400

        # Save booking
        booking = {
            "id": len(load_bookings()) + 1,
            "class_id": class_id,
            "class_name": class_info["Name"],
            "site_name": class_info["site_name"],
            "site_location": class_info["site_location"],
            "start_time": class_info["StartDateTime"],
            "client_name": client_name,
            "client_email": client_email,
            "booking_fee": fee,
            "status": "pending",
            "timestamp": datetime.utcnow().isoformat()
        }
        bookings = load_bookings()
        bookings.append(booking)
        save_bookings(bookings)
        post_to_hub(
            f"📅 Booking: {client_name} for {class_info['Name']} at {class_info['site_name']} (fee ${fee:.2f})",
            "info",
            booking
        )
        return render_template_string(CONFIRMATION_PAGE,
                                      class_name=class_info["Name"],
                                      fee=fee,
                                      payment_instructions=cfg.get("payment_instructions", ""))

    return render_template_string(BOOKING_PAGE, class_info=class_info, fee=fee)

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
def init_files(config):
    # Create empty bookings file
    if not BOOKINGS_FILE.exists():
        save_bookings([])
    # Create initial cache
    if not CACHE_FILE.exists():
        with open(CACHE_FILE, "w") as f:
            json.dump({}, f)

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub("Config created. Add Mindbody API key and studio IDs.", "warning")
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    init_files(config)
    start_heartbeat()

    port = config.get("web_port", 5081)
    post_to_hub(f"🧘‍♀️ Fitness Class Match live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
