#!/usr/bin/env python3
"""
personal_trainer_match_bot.py — Personal Trainer Match Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Connects women with trainers who specialise in weight loss,
postpartum recovery, or strength training.  Users search by
location, specialty, and budget, then request a trainer.
Each request includes a small lead fee – you earn every time.

Requirements:
    pip install flask requests

Configuration:
    On first run a file `personal_trainer_match_config.json` is created.
    Edit the lead fee, payment instructions, and sample trainers.
"""

import json
import time
import threading
import webbrowser
from pathlib import Path
from datetime import datetime

import requests
from flask import Flask, render_template_string, request, redirect, url_for

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "personal_trainer_match"
BOT_NAME = "Personal Trainer Match"

CFG_FILE     = Path(__file__).with_name("personal_trainer_match_config.json")
TRAINERS_FILE  = Path(__file__).with_name("trainers.json")
LEADS_FILE     = Path(__file__).with_name("trainer_leads.json")

DEFAULT_CONFIG = {
    "web_port": 5083,
    "lead_fee_usd": 5.00,
    "payment_instructions": "Send $5 via Venmo @TrainerMatch to confirm your lead. You'll then receive the trainer's direct contact info.",
    "trainers": [
        {
            "name": "Chloe Davis",
            "location": "Austin, TX",
            "zip_codes": ["78701","78702","78703","78704"],
            "specialties": ["weight loss","postpartum","strength"],
            "price_min": 60,
            "price_max": 90,
            "online": False,
            "certifications": "NASM-CPT, Pre/Postnatal Certified",
            "description": "Chloe helps new moms rebuild strength safely and sustainably."
        },
        {
            "name": "Morgan Kelly Fitness",
            "location": "Los Angeles, CA",
            "zip_codes": ["90001","90002","90003","90004"],
            "specialties": ["strength","weight loss"],
            "price_min": 85,
            "price_max": 130,
            "online": True,
            "certifications": "ACE, CSCS",
            "description": "Strength-focused coaching with custom meal plans."
        },
        {
            "name": "Lauren Strong",
            "location": "Chicago, IL",
            "zip_codes": ["60601","60602","60603","60604"],
            "specialties": ["postpartum","strength"],
            "price_min": 55,
            "price_max": 80,
            "online": True,
            "certifications": "Pre/Postnatal Corrective Exercise Specialist",
            "description": "Rebuild your core and confidence after baby."
        },
        {
            "name": "Sweat with Sarah",
            "location": "New York, NY",
            "zip_codes": ["10001","10002","10003","10004"],
            "specialties": ["weight loss","strength"],
            "price_min": 75,
            "price_max": 110,
            "online": False,
            "certifications": "NCSF-CPT, Nutrition Coach",
            "description": "High‑energy, results‑driven personal training in Manhattan."
        },
        {
            "name": "Emma Grace Coaching",
            "location": "Denver, CO",
            "zip_codes": ["80012","80013","80014","80202"],
            "specialties": ["postpartum","weight loss"],
            "price_min": 50,
            "price_max": 75,
            "online": True,
            "certifications": "AFPA Pre/Postnatal, Yoga Alliance",
            "description": "Holistic approach blending strength, yoga, and mindset."
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

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

MAIN_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Personal Trainer Match</title>
<style>
  body { font-family:Arial; max-width:800px; margin:40px auto; background:#f5f9fa; color:#222; }
  h1 { color:#306b7a; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, select, button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:4px; }
  button { background:#306b7a; color:white; cursor:pointer; font-size:16px; }
  .trainer-card { border-bottom:1px solid #eee; padding:12px 0; display:flex; justify-content:space-between; align-items:center; }
  .trainer-info { flex:1; }
  .trainer-info strong { font-size:1.05em; }
  .price { color:#888; }
  .online-tag { background:#306b7a; color:white; padding:2px 6px; border-radius:4px; font-size:0.8em; margin-left:6px; }
  a { color:#306b7a; font-weight:bold; }
  .small { font-size:0.9em; color:#888; margin-top:15px; }
</style>
</head>
<body>
<h1>💪 Find Your Perfect Personal Trainer</h1>
<p>Search for trainers who specialize in weight loss, postpartum recovery, or strength training.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Location (City or Zip)</label>
    <input type="text" name="location" value="{{ q.location }}" placeholder="e.g. Austin, TX or 60601">
    <label>Specialty</label>
    <select name="specialty">
      <option value="">All Specialties</option>
      <option value="weight loss" {% if q.specialty=='weight loss' %}selected{% endif %}>Weight Loss</option>
      <option value="postpartum" {% if q.specialty=='postpartum' %}selected{% endif %}>Postpartum Recovery</option>
      <option value="strength" {% if q.specialty=='strength' %}selected{% endif %}>Strength Training</option>
    </select>
    <label>Training Type</label>
    <select name="online">
      <option value="">Any</option>
      <option value="yes" {% if q.online=='yes' %}selected{% endif %}>Online Only</option>
      <option value="no" {% if q.online=='no' %}selected{% endif %}>In‑Person Only</option>
    </select>
    <label>Max Session Price ($)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="5" placeholder="No limit">
    <button type="submit">Find Trainers</button>
  </div>
</form>

{% if trainers is defined %}
<div class="card">
  <h2>Matching Trainers ({{ trainers|length }})</h2>
  {% for t in trainers %}
  <div class="trainer-card">
    <div class="trainer-info">
      <strong>{{ t.name }}</strong>
      {% if t.online %}<span class="online-tag">Online</span>{% endif %}
      <br/>
      <span class="price">{{ t.specialties|join(', ') }} · ${{ t.price_min }}–${{ t.price_max }}</span>
    </div>
    <a href="/trainer/{{ t.id }}">Request Lead</a>
  </div>
  {% endfor %}
  {% if trainers|length == 0 %}<p>No trainers found. Try different filters.</p>{% endif %}
</div>
{% endif %}

<p class="small"><a href="/register">Are you a trainer? Register here.</a></p>
</body>
</html>"""

TRAINER_DETAIL_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{{ trainer.name }} – Request Lead</title>
<style>
  body { font-family:Arial; max-width:600px; margin:40px auto; background:#f5f9fa; }
  h2 { color:#306b7a; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:4px; }
  button { background:#306b7a; color:white; cursor:pointer; font-size:16px; }
  .trainer-detail { background:#eef8fa; padding:15px; border-radius:6px; }
  a { color:#306b7a; }
</style>
</head>
<body>
<h2>Request a Lead for {{ trainer.name }}</h2>
<div class="trainer-detail">
  <p><strong>Location:</strong> {{ trainer.location }}{% if trainer.online %} (Online available){% endif %}</p>
  <p><strong>Specialties:</strong> {{ trainer.specialties|join(', ') }}</p>
  <p><strong>Price Range:</strong> ${{ trainer.price_min }} – ${{ trainer.price_max }}</p>
  <p>{{ trainer.description }}</p>
</div>
<div class="card">
  <p>To receive {{ trainer.name }}'s direct contact details, we ask for a one‑time ${{ fee }} lead fee.</p>
  <form method="POST" action="/request/{{ trainer.id }}">
    <label>Your Name</label><input type="text" name="client_name" required>
    <label>Your Email</label><input type="email" name="client_email" required>
    <button type="submit">Submit & Get Contact (${{ fee }})</button>
  </form>
</div>
<a href="/">← Back to search</a>
</body>
</html>"""

LEAD_CONFIRM_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Lead Request Received</title>
<style>body { font-family:Arial; text-align:center; padding:50px; background:#f5f9fa; }</style>
</head>
<body>
<h2>🎯 Lead Request Received!</h2>
<p><strong>{{ trainer_name }}</strong> will be notified.</p>
<p>To complete the connection, please send the <strong>${{ "%.2f"|format(fee) }} lead fee</strong> via:</p>
<p>{{ payment_instructions|replace('\n','<br/>') }}</p>
<p>Once payment is confirmed, we'll email you the trainer's contact details.</p>
<a href="/">← Back to search</a>
</body>
</html>"""

REGISTER_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Register as Personal Trainer</title>
<style>
  body { font-family:Arial; max-width:700px; margin:40px auto; background:#f5f9fa; }
  h2 { color:#306b7a; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, textarea, button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:4px; }
  button { background:#306b7a; color:white; cursor:pointer; font-size:16px; }
  a { color:#306b7a; }
</style>
</head>
<body>
<h2>💪 Register as a Personal Trainer</h2>
<form method="POST" action="/register">
  <div class="card">
    <label>Full Name / Brand</label><input type="text" name="name" required>
    <label>City, State</label><input type="text" name="location" required>
    <label>Zip Codes You Serve (comma separated)</label><input type="text" name="zip_codes" required>
    <label>Specialties (e.g. weight loss, postpartum, strength)</label><input type="text" name="specialties" required>
    <label>Price Min ($)</label><input type="number" name="price_min" required>
    <label>Price Max ($)</label><input type="number" name="price_max" required>
    <label>Offer Online Training?</label><input type="checkbox" name="online" checked>
    <label>Certifications</label><input type="text" name="certifications">
    <label>Short Description</label><textarea name="description" rows="3"></textarea>
    <button type="submit">Register</button>
  </div>
</form>
<a href="/">← Back</a>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    trainers = load_json(TRAINERS_FILE, [])
    q = {
        "location": request.args.get("location", "").strip().lower(),
        "specialty": request.args.get("specialty", "").strip().lower(),
        "online": request.args.get("online", "").strip(),
        "max_price": request.args.get("max_price", "").strip()
    }

    filtered = trainers[:]
    if q["location"]:
        filtered = [t for t in filtered if (
            q["location"] in t.get("location", "").lower() or
            any(q["location"] in zip_code.lower() for zip_code in t.get("zip_codes", []))
        )]
    if q["specialty"]:
        filtered = [t for t in filtered if q["specialty"] in [s.lower() for s in t.get("specialties", [])]]
    if q["online"] == "yes":
        filtered = [t for t in filtered if t.get("online")]
    elif q["online"] == "no":
        filtered = [t for t in filtered if not t.get("online")]

    if q["max_price"]:
        try:
            max_p = float(q["max_price"])
            filtered = [t for t in filtered if t.get("price_min", 9999) <= max_p]
        except ValueError:
            pass

    return render_template_string(MAIN_HTML, q=q, trainers=filtered)

@app.route("/trainer/<int:trainer_id>")
def trainer_detail(trainer_id):
    cfg = app.config["CFG"]
    trainers = load_json(TRAINERS_FILE, [])
    trainer = next((t for t in trainers if t["id"] == trainer_id), None)
    if not trainer:
        return "Trainer not found", 404
    fee = cfg.get("lead_fee_usd", 5.0)
    return render_template_string(TRAINER_DETAIL_HTML, trainer=trainer, fee=fee)

@app.route("/request/<int:trainer_id>", methods=["POST"])
def request_lead(trainer_id):
    cfg = app.config["CFG"]
    trainers = load_json(TRAINERS_FILE, [])
    trainer = next((t for t in trainers if t["id"] == trainer_id), None)
    if not trainer:
        return "Trainer not found", 404

    client_name = request.form.get("client_name", "").strip()
    client_email = request.form.get("client_email", "").strip()
    if not client_name or not client_email:
        return "Name and email required", 400

    lead = {
        "id": len(load_json(LEADS_FILE, [])) + 1,
        "trainer_id": trainer_id,
        "trainer_name": trainer["name"],
        "client_name": client_name,
        "client_email": client_email,
        "timestamp": datetime.utcnow().isoformat(),
        "lead_fee": cfg.get("lead_fee_usd", 5.0)
    }
    leads = load_json(LEADS_FILE, [])
    leads.append(lead)
    save_json(LEADS_FILE, leads)

    fee = lead["lead_fee"]
    post_to_hub(
        f"📋 New lead: {client_name} requested {trainer['name']} (fee ${fee:.2f})",
        "info",
        lead
    )
    return render_template_string(LEAD_CONFIRM_HTML,
                                  trainer_name=trainer["name"],
                                  fee=fee,
                                  payment_instructions=cfg.get("payment_instructions", ""))

@app.route("/register", methods=["GET", "POST"])
def register_trainer():
    if request.method == "POST":
        trainers = load_json(TRAINERS_FILE, [])
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "").strip()
        zip_codes_raw = request.form.get("zip_codes", "").strip()
        specialties_raw = request.form.get("specialties", "").strip()
        price_min = request.form.get("price_min", "").strip()
        price_max = request.form.get("price_max", "").strip()
        online = request.form.get("online") == "on"
        certifications = request.form.get("certifications", "").strip()
        description = request.form.get("description", "").strip()

        if not all([name, location, zip_codes_raw, specialties_raw, price_min, price_max]):
            return "All required fields are missing.", 400
        try:
            pmin = float(price_min)
            pmax = float(price_max)
        except ValueError:
            return "Invalid price.", 400

        zip_codes = [z.strip() for z in zip_codes_raw.split(",") if z.strip()]
        specialties = [s.strip().lower() for s in specialties_raw.split(",") if s.strip()]

        new_id = max([t["id"] for t in trainers], default=0) + 1
        trainer = {
            "id": new_id,
            "name": name,
            "location": location,
            "zip_codes": zip_codes,
            "specialties": specialties,
            "price_min": pmin,
            "price_max": pmax,
            "online": online,
            "certifications": certifications,
            "description": description
        }
        trainers.append(trainer)
        save_json(TRAINERS_FILE, trainers)
        post_to_hub(f"➕ New trainer registered: {name} ({location})", "info")
        return redirect(url_for("index"))

    return render_template_string(REGISTER_HTML)

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
    if not TRAINERS_FILE.exists():
        default_trainers = config.get("trainers", [])
        trainers_with_ids = []
        for i, t in enumerate(default_trainers, 1):
            trainers_with_ids.append({"id": i, **t})
        save_json(TRAINERS_FILE, trainers_with_ids)
    if not LEADS_FILE.exists():
        save_json(LEADS_FILE, [])

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub("Config created. Sample trainers loaded.", "info")

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    init_files(config)
    start_heartbeat()

    port = config.get("web_port", 5083)
    post_to_hub(f"💪 Personal Trainer Match live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
