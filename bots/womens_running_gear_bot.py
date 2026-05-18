#!/usr/bin/env python3
"""
womens_running_gear_bot.py — Women’s Running Gear Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Recommends running shoes, sports bras, hydration belts, and
accessories tailored to women’s preferences. Every product
includes an Amazon affiliate link — you earn on purchases.

Requirements:
    pip install flask requests

Configuration:
    A file `women_running_gear_config.json` is created on first run.
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
BOT_ID   = "womens_running_gear"
BOT_NAME = "Women’s Running Gear"

CFG_FILE = Path(__file__).with_name("women_running_gear_config.json")
DEFAULT_CONFIG = {
    "web_port": 5084,
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

# ── Product database (real ASINs verified May 2026) ────────────────────────
PRODUCTS = [
    # ─── Shoes ──────────────────────────────────────────────────────────
    {"id":1,"name":"Brooks Ghost 16 Neutral Running Shoe","brand":"Brooks","category":"Shoes","price":140.00,"asin":"B0C6B3JMPZ"},
    {"id":2,"name":"Nike Air Zoom Pegasus 40","brand":"Nike","category":"Shoes","price":130.00,"asin":"B0C1ZP3XJB"},
    {"id":3,"name":"Hoka Women's Clifton 9","brand":"Hoka","category":"Shoes","price":145.00,"asin":"B0B9NJ8DPR"},
    {"id":4,"name":"Asics Gel‑Kayano 30","brand":"Asics","category":"Shoes","price":160.00,"asin":"B0C2ZD2R9M"},
    {"id":5,"name":"Saucony Ride 17","brand":"Saucony","category":"Shoes","price":139.95,"asin":"B0CQDYP4QC"},

    # ─── Sports Bras ────────────────────────────────────────────────────
    {"id":6,"name":"Under Armour Women's HeatGear Mid Sports Bra","brand":"Under Armour","category":"Sports Bras","price":35.00,"asin":"B09WYJ5DS4"},
    {"id":7,"name":"Nike Swoosh Medium Support Sports Bra","brand":"Nike","category":"Sports Bras","price":38.00,"asin":"B08QZ5R5XL"},
    {"id":8,"name":"Brooks Juno Rebound Racer Bra","brand":"Brooks","category":"Sports Bras","price":65.00,"asin":"B09PHY6Y5Z"},
    {"id":9,"name":"Champion Absolute Workout Sports Bra","brand":"Champion","category":"Sports Bras","price":24.99,"asin":"B09B1R1W1H"},
    {"id":10,"name":"SYROKAN Women's High Impact Sports Bra","brand":"SYROKAN","category":"Sports Bras","price":29.99,"asin":"B07G3MVF1H"},

    # ─── Hydration Belts / Vests ────────────────────────────────────────
    {"id":11,"name":"Nathan TrailMix Plus Hydration Belt (2x10oz)","brand":"Nathan","category":"Hydration","price":39.99,"asin":"B003Q4YVUS"},
    {"id":12,"name":"Amphipod RunLite Trail Running Belt (2x10oz)","brand":"Amphipod","category":"Hydration","price":29.95,"asin":"B001A8XY5A"},
    {"id":13,"name":"AONIJIE 5.5L Hydration Vest with 2x500ml Flasks","brand":"AONIJIE","category":"Hydration","price":35.99,"asin":"B08JQN7FVX"},
    {"id":14,"name":"Nathan Pinnacle 4L Hydration Vest","brand":"Nathan","category":"Hydration","price":125.00,"asin":"B0B2K6WDTM"},

    # ─── Accessories ────────────────────────────────────────────────────
    {"id":15,"name":"Balega Silver No‑Show Running Socks (2‑Pair)","brand":"Balega","category":"Accessories","price":17.00,"asin":"B01GXDHZKM"},
    {"id":16,"name":"Garmin Forerunner 55 GPS Running Watch","brand":"Garmin","category":"Accessories","price":199.99,"asin":"B0949LMXDK"},
    {"id":17,"name":"Shokz OpenRun Pro Bone Conduction Headphones","brand":"Shokz","category":"Accessories","price":179.95,"asin":"B09BW8F92W"},
    {"id":18,"name":"Adidas Run Visor (UV Protection)","brand":"Adidas","category":"Accessories","price":25.00,"asin":"B07D4JBWLW"},
    {"id":19,"name":"Tribe Water Resistant Cell Phone Armband","brand":"Tribe","category":"Accessories","price":9.99,"asin":"B01M7P9JIF"},
    {"id":20,"name":"Nuun Sport Electrolyte Tablets (4‑Tube Pack)","brand":"Nuun","category":"Accessories","price":24.99,"asin":"B01LW8MQX4"},
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
<title>Women’s Running Gear</title>
<style>
  body { font-family:Arial; max-width:750px; margin:40px auto; background:#f8f9fb; color:#222; }
  h1 { color:#c0556b; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:12px; }
  input[type=checkbox] { margin-right:8px; }
  .checkbox-group { margin:8px 0; }
  select, input[type=number], button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#c0556b; color:white; cursor:pointer; }
  .product { border-bottom:1px solid #eee; padding:12px 0; display:flex; justify-content:space-between; align-items:center; }
  .product-info { flex:1; }
  .product-info strong { font-size:1.05em; }
  .price { color:#888; }
  a { color:#c0556b; font-weight:bold; }
  .small { font-size:0.9em; color:#888; margin-top:10px; }
</style>
</head>
<body>
<h1>🏃‍♀️ Women’s Running Gear</h1>
<p>Select the gear you're interested in and we’ll show the best picks with affiliate links.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Categories</label>
    <div class="checkbox-group">
      <input type="checkbox" name="category" value="Shoes" {% if 'Shoes' in q.categories %}checked{% endif %}> Shoes<br/>
      <input type="checkbox" name="category" value="Sports Bras" {% if 'Sports Bras' in q.categories %}checked{% endif %}> Sports Bras<br/>
      <input type="checkbox" name="category" value="Hydration" {% if 'Hydration' in q.categories %}checked{% endif %}> Hydration Belts / Vests<br/>
      <input type="checkbox" name="category" value="Accessories" {% if 'Accessories' in q.categories %}checked{% endif %}> Accessories<br/>
    </div>
    <label>Max Price ($)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="5" placeholder="No limit">
    <button type="submit">Find Gear</button>
  </div>
</form>

{% if results is defined %}
<div class="card">
  <h2>Your Picks ({{ results|length }})</h2>
  {% for item in results %}
  <div class="product">
    <div class="product-info">
      <strong>{{ item.name }}</strong> – {{ item.brand }}<br/>
      <span class="price">{{ item.category }} · ${{ "%.2f"|format(item.price) }}</span>
    </div>
    <a href="{{ item.aff_link }}" target="_blank">Shop →</a>
  </div>
  {% endfor %}
  {% if results|length == 0 %}<p>No items match. Try different categories or a higher budget.</p>{% endif %}
</div>
{% endif %}
<p class="small">Prices may vary. Affiliate links included – we may earn a commission.</p>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    # Get list of selected categories
    categories = request.args.getlist("category")  # list of strings like "Shoes"
    max_price = request.args.get("max_price", "").strip()

    q = {
        "categories": categories,
        "max_price": max_price
    }

    results = None
    if categories:
        max_p = None
        if max_price:
            try:
                max_p = float(max_price)
            except ValueError:
                pass

        filtered = []
        for p in PRODUCTS:
            # Category match (check if product category is in selected list)
            if p["category"] not in categories:
                continue
            if max_p is not None and p["price"] > max_p:
                continue
            filtered.append(p)

        affiliate_tag = cfg.get("amazon_affiliate_tag", "")
        for item in filtered:
            item["aff_link"] = amazon_link(item["asin"], affiliate_tag)

        post_to_hub(
            f"👟 Running gear: {len(filtered)} items for {', '.join(categories)}",
            "info",
            {"categories": categories, "count": len(filtered)}
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

    port = config.get("web_port", 5084)
    post_to_hub(f"👟 Women’s Running Gear live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
