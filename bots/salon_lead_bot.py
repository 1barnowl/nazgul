#!/usr/bin/env python3
"""
salon_lead_bot.py — Salon Appointment Lead Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Captures women searching for hair, nails, lash services
via a local landing page. Saves every lead and posts it
to the BotController hub so you can sell them to local salons.

Requirements:
    pip install flask requests

Configuration:
    A file `salon_lead_config.json` is created on first run.
    Edit the web port, optional SMTP for salon notifications,
    and your lead value note.
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
BOT_ID   = "salon_lead_gen"
BOT_NAME = "Salon Lead Bot"

CFG_FILE   = Path(__file__).with_name("salon_lead_config.json")
LEADS_FILE = Path(__file__).with_name("salon_leads.json")

DEFAULT_CONFIG = {
    "web_port": 5075,
    "thank_you_redirect": "",            # optional URL to redirect after sign‑up
    "lead_value_note": "Leads are saved in salon_leads.json. Sell them to local salons for $5‑$15 each.",
    "smtp": {                            # optional – forward leads to salon automatically
        "enabled": False,
        "server": "smtp.gmail.com",
        "port": 587,
        "username": "",
        "password": "",
        "from_email": "",
        "salon_email": ""                # where to send new lead notifications
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

# ── Lead storage ────────────────────────────────────────────────────────────
def load_leads():
    if LEADS_FILE.exists():
        with open(LEADS_FILE, "r") as f:
            return json.load(f)
    return []

def save_lead(lead):
    leads = load_leads()
    leads.append(lead)
    with open(LEADS_FILE, "w") as f:
        json.dump(leads, f, indent=2)

# ── Email sender (optional) ─────────────────────────────────────────────────
def notify_salon(lead, config):
    smtp = config.get("smtp", {})
    if not smtp.get("enabled"):
        return
    import smtplib
    from email.mime.text import MIMEText
    subject = f"New Lead: {lead['name']} wants {lead['service']} in {lead['location']}"
    body = f"""A new client is looking for:
    Name: {lead['name']}
    Email: {lead['email']}
    Phone: {lead['phone']}
    Service: {lead['service']}
    Location: {lead['location']}
    Timestamp: {lead['timestamp']}
    Contact them quickly to book!
    """
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = smtp["from_email"]
    msg["To"] = smtp.get("salon_email", "")
    try:
        with smtplib.SMTP(smtp["server"], smtp["port"], timeout=10) as server:
            server.starttls()
            server.login(smtp["username"], smtp["password"])
            server.send_message(msg)
    except Exception as e:
        post_to_hub(f"Failed to notify salon via email: {e}", "error")

# ── Landing page HTML ───────────────────────────────────────────────────────
LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Find a Top‑Rated Salon Near You</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 500px; margin: 50px auto;
           background: #fdf8f5; color: #1e2b2e; padding: 20px; }
    .card { background: white; padding: 30px; border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.05); }
    h1 { color: #9b5c3a; margin-bottom: 10px; }
    p { color: #555; }
    label { font-weight: bold; display: block; margin: 15px 0 5px; }
    input, select, button { width: 100%; padding: 12px; border: 1px solid #ccc;
                            border-radius: 6px; font-size: 16px; margin-bottom: 5px; }
    button { background: #9b5c3a; color: white; font-weight: bold; border: none;
             cursor: pointer; margin-top: 20px; }
    .small { font-size: 0.8em; color: #888; text-align: center; margin-top: 20px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>💇‍♀️ Find a Top‑Rated Salon</h1>
    <p>Looking for hair, nails, or lash services? Enter your details and
       we’ll match you with the best local salons.</p>
    <form method="POST" action="/submit">
      <label for="name">Full Name</label>
      <input type="text" name="name" placeholder="Your name" required>

      <label for="email">Email Address</label>
      <input type="email" name="email" placeholder="you@example.com" required>

      <label for="phone">Phone Number (optional)</label>
      <input type="tel" name="phone" placeholder="555-123-4567">

      <label for="service">Service Needed</label>
      <select name="service">
        <option value="hair">Haircut / Colour / Styling</option>
        <option value="nails">Manicure / Pedicure / Nail Art</option>
        <option value="lashes">Lash Extensions / Lash Lift</option>
        <option value="multiple">Multiple Services</option>
      </select>

      <label for="location">Your City / Zip</label>
      <input type="text" name="location" placeholder="e.g. Austin, TX" required>

      <button type="submit">Find My Salon Match</button>
    </form>
  </div>
  <p class="small">We respect your privacy. No spam, only salon matches.</p>
</body>
</html>
"""

THANK_YOU_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Thank You</title>
<style>body { font-family: Arial; text-align: center; padding: 50px; background: #fdf8f5; }</style></head>
<body><h2>Thank you! 💖</h2><p>A local salon will reach out to you shortly.</p>
<p><a href="/">← Back</a></p></body></html>
"""

# ── Flask handler ───────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return render_template_string(LANDING_HTML)

@app.route("/submit", methods=["POST"])
def submit():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    service = request.form.get("service", "").strip()
    location = request.form.get("location", "").strip()

    if not name or not email:
        return "Name and email required.", 400

    lead = {
        "name": name,
        "email": email,
        "phone": phone,
        "service": service,
        "location": location,
        "timestamp": datetime.utcnow().isoformat()
    }
    save_lead(lead)

    # Post to hub
    post_to_hub(
        f"💇 New lead: {name} wants {service} in {location} – {email}",
        "info",
        lead
    )

    # Optionally notify salon via email
    cfg = app.config.get("SALON_CFG", {})
    notify_salon(lead, cfg)

    # Redirect or thank you
    redirect_url = cfg.get("thank_you_redirect", "")
    if redirect_url:
        return redirect(redirect_url)
    return render_template_string(THANK_YOU_HTML)

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

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config file created at {CFG_FILE}. Edit web port, SMTP, etc.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["SALON_CFG"] = config
    start_heartbeat()

    port = config.get("web_port", 5075)
    post_to_hub(f"💇 Salon Lead Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
