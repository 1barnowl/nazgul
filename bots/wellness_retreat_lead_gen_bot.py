#!/usr/bin/env python3
"""
wellness_retreat_lead_gen_bot.py — Wellness Retreat Lead Gen Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Captures emails of women interested in detox/yoga retreats via a
simple landing page. Saves every lead to a local file and posts it
to the BotController hub so you can sell the leads to retreat organisers.

Requirements:
    pip install requests

Configuration:
    A file named `retreat_lead_gen_config.json` is created on first run.
    Edit the web port and optionally add a redirect URL after sign‑up.
"""

import json
import time
import threading
import webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import requests

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "wellness_retreat_lead_gen"
BOT_NAME = "Wellness Retreat Lead Gen"

CFG_FILE = Path(__file__).with_name("retreat_lead_gen_config.json")
LEADS_FILE = Path(__file__).with_name("retreat_leads.json")

DEFAULT_CONFIG = {
    "web_port": 5061,
    "thank_you_redirect": "",        # optional URL to redirect after sign‑up
    "lead_value_note": "Leads are saved in retreat_leads.json. Sell them to retreat organisers."
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

# ── Landing page HTML ───────────────────────────────────────────────────────
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Find Your Perfect Wellness Retreat</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 500px; margin: 50px auto;
           background: #f0f5f0; color: #1e2b2e; padding: 20px; }
    .card { background: white; padding: 30px; border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.05); }
    h1 { color: #2e5a4c; margin-bottom: 10px; }
    p { color: #555; }
    label { font-weight: bold; display: block; margin: 15px 0 5px; }
    input, select, button { width: 100%; padding: 12px; border: 1px solid #ccc;
                            border-radius: 6px; font-size: 16px; margin-bottom: 5px; }
    button { background: #2e5a4c; color: white; font-weight: bold; border: none;
             cursor: pointer; margin-top: 20px; }
    .small { font-size: 0.8em; color: #888; text-align: center; margin-top: 20px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>🌿 Discover Your Dream Retreat</h1>
    <p>We match you with exclusive detox, yoga, and mindfulness retreats.
       Enter your details and a retreat specialist will reach out.</p>
    <form method="POST" action="/submit">
      <label for="name">First Name</label>
      <input type="text" name="name" placeholder="Your first name" required>

      <label for="email">Email Address</label>
      <input type="email" name="email" placeholder="you@example.com" required>

      <label for="interest">What are you looking for?</label>
      <select name="interest">
        <option value="yoga">Yoga Retreat</option>
        <option value="detox">Detox / Cleanse</option>
        <option value="meditation">Meditation & Mindfulness</option>
        <option value="spa">Luxury Spa Retreat</option>
        <option value="any">Open to Suggestions</option>
      </select>

      <label for="budget">Budget per person (USD)</label>
      <select name="budget">
        <option value="under1000">Under $1,000</option>
        <option value="1000-2000">$1,000 - $2,000</option>
        <option value="2000-5000">$2,000 - $5,000</option>
        <option value="5000+">$5,000+</option>
      </select>

      <button type="submit">Get Matched</button>
    </form>
  </div>
  <p class="small">We respect your privacy. No spam, only retreat offers.</p>
</body>
</html>
"""

THANK_YOU_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Thank You</title>
<style>body {font-family: Arial; text-align: center; padding: 50px; background: #f0f5f0;}</style></head>
<body><h2>Thank you! 🌸</h2><p>A retreat specialist will reach out shortly.</p>
<p><a href="/">← Back</a></p></body></html>
"""

# ── HTTP handler ────────────────────────────────────────────────────────────
class LeadHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/submit":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            params = parse_qs(body)

            name = params.get("name", [""])[0].strip()
            email = params.get("email", [""])[0].strip()
            interest = params.get("interest", [""])[0].strip()
            budget = params.get("budget", [""])[0].strip()

            if not email or not name:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Name and email required.")
                return

            lead = {
                "name": name,
                "email": email,
                "interest": interest,
                "budget": budget,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            # Save locally
            save_lead(lead)
            # Post to hub
            post_to_hub(
                f"📨 New lead: {name} ({email}) — {interest}, budget {budget}",
                "info",
                lead
            )

            # Redirect or thank you page
            redirect_url = self.server.config.get("thank_you_redirect", "")
            if redirect_url:
                self.send_response(302)
                self.send_header("Location", redirect_url)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(THANK_YOU_HTML.encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass

def start_server(config):
    port = config.get("web_port", 5061)
    server = HTTPServer(("127.0.0.1", port), LeadHandler)
    server.config = config
    post_to_hub(f"🌿 Retreat Lead Gen Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")
    server.serve_forever()

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config file created at {CFG_FILE}. Edit port / redirect and restart.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    # Heartbeat
    def heartbeat_loop():
        while True:
            time.sleep(20)
            try:
                requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
                    "bot_name": BOT_NAME, "status": "online",
                }, timeout=3)
            except Exception:
                pass
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    start_server(config)

if __name__ == "__main__":
    main()
