#!/usr/bin/env python3
"""
appointment_retention_bot.py — Appointment Retention Bot (B2B White‑Label)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tracks client service lifecycles for salons, spas, and clinics.
Triggers re‑engagement alerts when a treatment’s effect typically wears off.

Integrates with BotController hub – every alert is a high‑priority notification
that a client needs to be contacted. Web interface for managing clients, services,
and logging appointments.

Requirements:
    pip install flask requests

Configuration:
    On first run, `retention_config.json` is created. Edit service retention
    windows, SMTP (optional), and the lead time for alerts.
"""

import json
import os
import time
import threading
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, render_template_string, request, redirect, url_for

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "appointment_retention"
BOT_NAME = "Appointment Retention"

CFG_FILE         = Path(__file__).with_name("retention_config.json")
CLIENTS_FILE     = Path(__file__).with_name("retention_clients.json")
SERVICES_FILE    = Path(__file__).with_name("retention_services.json")
APPOINTMENTS_FILE = Path(__file__).with_name("retention_appointments.json")

DEFAULT_CONFIG = {
    "web_port": 5070,
    "check_interval_hours": 24,
    "alert_lead_days": 0,               # how many days before/after retention window to alert (0 = exactly after window)
    "reminder_message_template": "Hi {client_name}, it's been {days} days since your last {service_name}. We'd love to see you again! Book now: {booking_link}",
    "booking_link": "https://your-booking-page.com",
    "smtp": {                           # optional – for sending client emails
        "server": "smtp.gmail.com",
        "port": 587,
        "username": "",
        "password": "",
        "from_email": ""
    },
    "services": [                       # default services with typical retention windows (days)
        {"name": "Haircut", "retention_days": 42},
        {"name": "Hair Colour", "retention_days": 42},
        {"name": "Massage", "retention_days": 28},
        {"name": "Facial", "retention_days": 30},
        {"name": "Manicure", "retention_days": 14},
        {"name": "Pedicure", "retention_days": 21},
        {"name": "Botox", "retention_days": 90},
        {"name": "Filler", "retention_days": 90}
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
def load_json(filepath, default=[]):
    if filepath.exists():
        with open(filepath, "r") as f:
            return json.load(f)
    return default

def save_json(filepath, data):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

# ── Core logic ──────────────────────────────────────────────────────────────
def get_last_appointment(client_id, service_id, appointments):
    """Return the most recent appointment (dict) for a client and service."""
    relevant = [a for a in appointments
                if a["client_id"] == client_id and a["service_id"] == service_id]
    if not relevant:
        return None
    return max(relevant, key=lambda a: a["date"])

def days_since_last(last_appointment):
    """Return number of days from last appointment date to today."""
    if not last_appointment:
        return None
    last_date = datetime.strptime(last_appointment["date"], "%Y-%m-%d").date()
    return (date.today() - last_date).days

def check_retention(config):
    """Scan all clients and services, post alerts for re‑engagement."""
    clients = load_json(CLIENTS_FILE, [])
    services = load_json(SERVICES_FILE, [])
    appointments = load_json(APPOINTMENTS_FILE, [])

    alert_lead = config.get("alert_lead_days", 0)

    for client in clients:
        for service in services:
            last_appt = get_last_appointment(client["id"], service["id"], appointments)
            if not last_appt:
                continue   # client never had this service, skip
            days = days_since_last(last_appt)
            if days is None:
                continue
            retention_days = service.get("retention_days", 30)
            # Trigger alert when days_since_last >= retention_days + alert_lead
            threshold = retention_days + alert_lead
            if days >= threshold:
                # Build reminder message
                template = config.get("reminder_message_template", "")
                msg = template.format(
                    client_name=client["name"],
                    days=days,
                    service_name=service["name"],
                    booking_link=config.get("booking_link", "")
                )
                payload = {
                    "client_name": client["name"],
                    "client_email": client.get("email"),
                    "client_phone": client.get("phone"),
                    "service_name": service["name"],
                    "last_appointment_date": last_appt["date"],
                    "days_since": days,
                    "retention_days": retention_days,
                    "reminder_message": msg
                }
                post_to_hub(
                    f"🔔 Re‑engage {client['name']}: last {service['name']} was {days}d ago (window {retention_days}d).",
                    "warning",
                    payload
                )
                # Optionally send email to client if SMTP configured
                if config.get("smtp", {}).get("username"):
                    send_reminder_email(client, service, days, config)

def send_reminder_email(client, service, days, config):
    """Send a reminder email to the client using configured SMTP."""
    smtp = config["smtp"]
    if not smtp.get("server") or not smtp.get("username"):
        return
    import smtplib
    from email.mime.text import MIMEText

    msg_body = config["reminder_message_template"].format(
        client_name=client["name"],
        days=days,
        service_name=service["name"],
        booking_link=config["booking_link"]
    )
    msg = MIMEText(msg_body)
    msg["Subject"] = f"It's time for your {service['name']}!"
    msg["From"] = smtp["from_email"]
    msg["To"] = client["email"]
    try:
        with smtplib.SMTP(smtp["server"], smtp["port"], timeout=10) as server:
            server.starttls()
            server.login(smtp["username"], smtp["password"])
            server.send_message(msg)
        post_to_hub(f"✉️ Email sent to {client['email']} about {service['name']}.", "info")
    except Exception as e:
        post_to_hub(f"Failed to send email to {client['email']}: {e}", "error")

# ── Flask web interface ─────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

HTML_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Appointment Retention</title>
<style>
  body { font-family:Arial; max-width:900px; margin:40px auto; background:#f9f9f9; color:#222; }
  h1 { color:#2c3e50; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:8px; }
  input, select, button { padding:8px; margin:4px 0 10px; border:1px solid #ccc; border-radius:4px; width:100%; }
  button { background:#2c3e50; color:white; font-size:16px; cursor:pointer; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px; border-bottom:1px solid #eee; }
  a { color:#2c3e50; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>📋 Appointment Retention Manager</h1>
<p>Track clients and service lifecycles. Alerts fire when it's time for a re‑booking.</p>

<div class="card">
  <h2>Add Client</h2>
  <form method="POST" action="/add_client">
    <label>Name</label><input type="text" name="name" required>
    <label>Email</label><input type="email" name="email">
    <label>Phone</label><input type="text" name="phone">
    <button type="submit">Save Client</button>
  </form>
</div>

<div class="card">
  <h2>Log Appointment</h2>
  <form method="POST" action="/log_appointment">
    <label>Client</label>
    <select name="client_id" required>
      {% for c in clients %}<option value="{{ c.id }}">{{ c.name }}</option>{% endfor %}
    </select>
    <label>Service</label>
    <select name="service_id" required>
      {% for s in services %}<option value="{{ s.id }}">{{ s.name }} ({{ s.retention_days }}d)</option>{% endfor %}
    </select>
    <label>Date</label><input type="date" name="date" required>
    <button type="submit">Log Appointment</button>
  </form>
</div>

<h2>Clients</h2>
<table>
  <tr><th>ID</th><th>Name</th><th>Email</th><th>Phone</th></tr>
  {% for c in clients %}
    <tr><td>{{ c.id }}</td><td>{{ c.name }}</td><td>{{ c.email }}</td><td>{{ c.phone }}</td></tr>
  {% endfor %}
</table>

<h2>Services</h2>
<table>
  <tr><th>ID</th><th>Name</th><th>Retention (days)</th></tr>
  {% for s in services %}
    <tr><td>{{ s.id }}</td><td>{{ s.name }}</td><td>{{ s.retention_days }}</td></tr>
  {% endfor %}
</table>

<h2>Recent Appointments (last 50)</h2>
<table>
  <tr><th>Client</th><th>Service</th><th>Date</th></tr>
  {% for a in recent_appts %}
    <tr><td>{{ a.client_name }}</td><td>{{ a.service_name }}</td><td>{{ a.date }}</td></tr>
  {% endfor %}
</table>

<p class="small">Alerts are evaluated every {{ check_interval }} hours.</p>
</body>
</html>
"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    clients = load_json(CLIENTS_FILE, [])
    services = load_json(SERVICES_FILE, [])
    appointments = load_json(APPOINTMENTS_FILE, [])

    # Build recent appointments with names
    def get_name(collection, id, field="name"):
        for item in collection:
            if item["id"] == id:
                return item.get(field, str(id))
        return str(id)

    recent = []
    for a in appointments[-50:]:
        recent.append({
            "client_name": get_name(clients, a["client_id"]),
            "service_name": get_name(services, a["service_id"]),
            "date": a["date"]
        })
    recent.reverse()

    return render_template_string(HTML_TEMPLATE,
                                  clients=clients,
                                  services=services,
                                  recent_appts=recent,
                                  check_interval=cfg.get("check_interval_hours", 24))

@app.route("/add_client", methods=["POST"])
def add_client():
    clients = load_json(CLIENTS_FILE, [])
    # Generate new ID
    new_id = max([c["id"] for c in clients], default=0) + 1
    client = {
        "id": new_id,
        "name": request.form["name"].strip(),
        "email": request.form.get("email", "").strip(),
        "phone": request.form.get("phone", "").strip()
    }
    clients.append(client)
    save_json(CLIENTS_FILE, clients)
    post_to_hub(f"➕ Client added: {client['name']}", "info")
    return redirect(url_for("index"))

@app.route("/log_appointment", methods=["POST"])
def log_appointment():
    appointments = load_json(APPOINTMENTS_FILE, [])
    new_id = max([a["id"] for a in appointments], default=0) + 1
    appt = {
        "id": new_id,
        "client_id": int(request.form["client_id"]),
        "service_id": int(request.form["service_id"]),
        "date": request.form["date"]
    }
    appointments.append(appt)
    save_json(APPOINTMENTS_FILE, appointments)
    post_to_hub(f"📆 Appointment logged for client {appt['client_id']} on {appt['date']}", "info")
    return redirect(url_for("index"))

# ── Scanner thread ─────────────────────────────────────────────────────────
def scanner_loop(config):
    interval = config.get("check_interval_hours", 24) * 3600
    while True:
        check_retention(config)
        time.sleep(interval)

# ── Initialization ──────────────────────────────────────────────────────────
def initialize_files(config):
    # Create default services if none exist
    if not SERVICES_FILE.exists():
        default_svcs = config.get("services", [])
        svcs_with_ids = []
        for i, s in enumerate(default_svcs, 1):
            svcs_with_ids.append({"id": i, **s})
        save_json(SERVICES_FILE, svcs_with_ids)
    # Ensure clients and appointments files exist
    if not CLIENTS_FILE.exists():
        save_json(CLIENTS_FILE, [])
    if not APPOINTMENTS_FILE.exists():
        save_json(APPOINTMENTS_FILE, [])

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub("Config created. Default services and retention windows loaded.", "info")
    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    initialize_files(config)

    # Start background scanner
    threading.Thread(target=scanner_loop, args=(config,), daemon=True).start()

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

    port = config.get("web_port", 5070)
    post_to_hub(f"📋 Retention Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
