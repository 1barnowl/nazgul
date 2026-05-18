#!/usr/bin/env python3
"""
womens_travel_safety_bot.py — Women’s Travel Safety Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Recommends safety gear, SIM/eSIM plans, and travel tools for solo
female travellers. Every product links to Amazon with your affiliate
tag so you earn on purchases.

Requirements:
    pip install flask requests

Configuration:
    A file `travel_safety_config.json` is created on first run.
    Add your Amazon Associate tag (e.g. "yourtag-20") to earn commissions.
"""

import json
import time
import threading
import webbrowser
from pathlib import Path

import requests
from flask import Flask, render_template_string, request

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "womens_travel_safety"
BOT_NAME = "Women’s Travel Safety"

CFG_FILE = Path(__file__).with_name("travel_safety_config.json")
DEFAULT_CONFIG = {
    "web_port": 5085,
    "amazon_affiliate_tag": ""          # e.g. "yourtag-20"
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

# ── Product database (real ASINs) ──────────────────────────────────────────
PRODUCTS = [
    # Safety Gear
    {"id":1,"name":"Addalock Original Portable Door Lock","brand":"Addalock","category":"Safety Gear","concerns":["theft","accommodation"],"price":15.95,"asin":"B004Y4TQ64"},
    {"id":2,"name":"She's Birdie Personal Safety Alarm (130dB)","brand":"She's Birdie","category":"Safety Gear","concerns":["theft","street safety"],"price":29.99,"asin":"B09B9XM4RF"},
    {"id":3,"name":"Lewis N Clark Travel Door Alarm + Flashlight","brand":"Lewis N Clark","category":"Safety Gear","concerns":["theft","accommodation"],"price":14.99,"asin":"B002SX4XJO"},
    {"id":4,"name":"Trekology Ultralight Portable Lock Box","brand":"Trekology","category":"Safety Gear","concerns":["theft","beach","accommodation"],"price":29.95,"asin":"B07T3M7NKR"},
    {"id":5,"name":"Sabre Red Pepper Gel Spray (Police Strength)","brand":"Sabre","category":"Safety Gear","concerns":["street safety","theft"],"price":11.99,"asin":"B0007ZG9K6"},
    # Anti‑theft Bags
    {"id":6,"name":"Travelon Anti‑Theft Classic Messenger Bag","brand":"Travelon","category":"Safety Gear","concerns":["theft","street safety"],"price":49.99,"asin":"B00FZV7PSW"},
    {"id":7,"name":"Pacsafe Citysafe CX Anti‑Theft Satchel","brand":"Pacsafe","category":"Safety Gear","concerns":["theft","street safety"],"price":89.95,"asin":"B01NBKXF4O"},
    # SIM / Communication
    {"id":8,"name":"Skyroam Solis Lite 4G LTE WiFi Hotspot (Global)","brand":"Skyroam","category":"Communication","concerns":["communication","connectivity"],"price":119.99,"asin":"B08GCMQY6Y"},
    {"id":9,"name":"Keepgo Lifetime Prepaid eSIM Data (1GB)","brand":"Keepgo","category":"Communication","concerns":["communication"],"price":8.00,"asin":"B08ZW4CJVP"},
    {"id":10,"name":"GlocalMe G4 Pro 4G LTE WiFi Hotspot (Pay‑as‑You‑Go)","brand":"GlocalMe","category":"Communication","concerns":["communication"],"price":159.99,"asin":"B089RYKK5S"},
    # Health & First Aid
    {"id":11,"name":"Adventure Medical Kits Ultralight/Watertight .7","brand":"Adventure Medical Kits","category":"Health","concerns":["health","hiking","remote"],"price":19.99,"asin":"B00FZQB7TW"},
    {"id":12,"name":"LifeStraw Personal Water Filter","brand":"LifeStraw","category":"Health","concerns":["health","remote","water"],"price":19.95,"asin":"B006QF3TW4"},
    {"id":13,"name":"Nite Ize SpotLit Clip‑On LED Safety Light (4‑Pack)","brand":"Nite Ize","category":"Safety Gear","concerns":["street safety","visibility"],"price":6.99,"asin":"B0015IS08K"},
    # Navigation & Power
    {"id":14,"name":"Anker PowerCore 10000mAh Portable Charger","brand":"Anker","category":"Accessories","concerns":["power","connectivity"],"price":21.99,"asin":"B07XS7FTN8"},
    {"id":15,"name":"Garmin inReach Mini 2 Satellite Communicator","brand":"Garmin","category":"Communication","concerns":["remote","communication","emergency"],"price":399.99,"asin":"B09QNYLSSQ"},
    {"id":16,"name":"Compass + Whistle + Thermometer (Multifunction)","brand":"Coghlin","category":"Accessories","concerns":["navigation","remote"],"price":9.99,"asin":"B07H8LC7DL"},
    {"id":17,"name":"Tile Pro (4‑Pack) Bluetooth Trackers","brand":"Tile","category":"Accessories","concerns":["theft","luggage"],"price":79.99,"asin":"B09WHVYFGB"},
    # Miscellaneous
    {"id":18,"name":"Carry‑on Travel Backpack with USB Charging Port","brand":"Ecohub","category":"Accessories","concerns":["theft","organization"],"price":49.99,"asin":"B0BSRCF2JN"},
    {"id":19,"name":"First Aid Only 299 Piece All‑Purpose Kit","brand":"First Aid Only","category":"Health","concerns":["health"],"price":19.99,"asin":"B000069HNC"},
    {"id":20,"name":"Go Girl Female Urination Device","brand":"Go Girl","category":"Accessories","concerns":["health","remote"],"price":9.99,"asin":"B002GJNDQ6"},
]

# ── Affiliate link builder ──────────────────────────────────────────────────
def amazon_link(asin, affiliate_tag):
    if affiliate_tag.strip():
        return f"https://www.amazon.com/dp/{asin}?tag={affiliate_tag.strip()}"
    return f"https://www.amazon.com/dp/{asin}"

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Solo Female Travel Safety</title>
<style>
  body { font-family:Arial; max-width:800px; margin:40px auto; background:#f3f7fa; color:#222; }
  h1 { color:#1d5a6d; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:12px; }
  .checkbox-group { margin:8px 0; }
  input[type=checkbox] { margin-right:8px; }
  select, input[type=number], button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#1d5a6d; color:white; cursor:pointer; }
  .product { border-bottom:1px solid #eee; padding:12px 0; display:flex; justify-content:space-between; align-items:center; }
  .product-info { flex:1; }
  .product-info strong { font-size:1.05em; }
  .price { color:#888; }
  a { color:#1d5a6d; font-weight:bold; }
  .small { font-size:0.9em; color:#888; margin-top:10px; }
</style>
</head>
<body>
<h1>🛡️ Solo Female Travel Safety Gear</h1>
<p>Select your top concerns and we'll recommend the best safety & communication tools with Amazon affiliate links.</p>
<form method="GET" action="/">
  <div class="card">
    <label>What are your main worries?</label>
    <div class="checkbox-group">
      <input type="checkbox" name="concern" value="theft" {% if 'theft' in q.concerns %}checked{% endif %}> Theft / Pickpocketing<br/>
      <input type="checkbox" name="concern" value="street safety" {% if 'street safety' in q.concerns %}checked{% endif %}> Walking alone at night<br/>
      <input type="checkbox" name="concern" value="accommodation" {% if 'accommodation' in q.concerns %}checked{% endif %}> Unsecured hotel/airbnb<br/>
      <input type="checkbox" name="concern" value="communication" {% if 'communication' in q.concerns %}checked{% endif %}> No internet / SIM<br/>
      <input type="checkbox" name="concern" value="health" {% if 'health' in q.concerns %}checked{% endif %}> Minor injuries / hygiene<br/>
      <input type="checkbox" name="concern" value="remote" {% if 'remote' in q.concerns %}checked{% endif %}> Off‑grid / Hiking<br/>
    </div>
    <label>Max Price ($)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="5" placeholder="No limit">
    <button type="submit">Get Recommendations</button>
  </div>
</form>

{% if results is defined %}
<div class="card">
  <h2>Your Safety Kit ({{ results|length }})</h2>
  {% for item in results %}
  <div class="product">
    <div class="product-info">
      <strong>{{ item.name }}</strong> – {{ item.brand }}<br/>
      <span class="price">{{ item.category }} · ${{ "%.2f"|format(item.price) }}</span>
    </div>
    <a href="{{ item.aff_link }}" target="_blank">Shop →</a>
  </div>
  {% endfor %}
  {% if results|length == 0 %}<p>No items match. Try different concerns or a higher budget.</p>{% endif %}
</div>
{% endif %}
<p class="small">Prices may vary. Affiliate links included – we may earn a commission.</p>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    # Get list of selected concerns
    concerns = request.args.getlist("concern")
    max_price = request.args.get("max_price", "").strip()

    q = {"concerns": concerns, "max_price": max_price}

    results = None
    if concerns:
        max_p = None
        if max_price:
            try:
                max_p = float(max_price)
            except ValueError:
                pass

        filtered = []
        for p in PRODUCTS:
            # Must match at least one selected concern (the "concerns" field is a list)
            if not any(c in p["concerns"] for c in concerns):
                continue
            if max_p is not None and p["price"] > max_p:
                continue
            filtered.append(p)

        affiliate_tag = cfg.get("amazon_affiliate_tag", "")
        for item in filtered:
            item["aff_link"] = amazon_link(item["asin"], affiliate_tag)

        post_to_hub(
            f"🛡️ Travel safety: {len(filtered)} items for {', '.join(concerns)}",
            "info",
            {"concerns": concerns, "count": len(filtered)}
        )
        results = filtered

    return render_template_string(HTML_PAGE, q=q, results=results)

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
            f"Config created at {CFG_FILE}. Add your Amazon affiliate tag to earn.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    start_heartbeat()

    port = config.get("web_port", 5085)
    post_to_hub(f"🛡️ Travel Safety Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
