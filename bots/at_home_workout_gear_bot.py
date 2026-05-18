#!/usr/bin/env python3
"""
at_home_workout_gear_bot.py — At‑Home Workout Gear Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Recommends dumbbells, mats, and recovery tools tailored to women's
fitness goals.  Every recommendation carries an Amazon affiliate link
so you earn on purchases.

Requirements:
    pip install flask requests

Configuration:
    A file `workout_gear_config.json` is created on first run.
    Add your Amazon Associate tag (e.g. "your-20") to earn commissions.
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
BOT_ID   = "workout_gear_affiliate"
BOT_NAME = "At‑Home Workout Gear"

CFG_FILE = Path(__file__).with_name("workout_gear_config.json")
DEFAULT_CONFIG = {
    "web_port": 5082,
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
    # ─── Strength ────────────────────────────────────────────────────────
    {"id":1,"name":"Tone It Up Neoprene Dumbbell Set (3/5/8 lb)","brand":"Tone It Up","category":"Dumbbells","goals":["strength"],"price":29.99,"asin":"B07MH2J48B"},
    {"id":2,"name":"Yes4All Adjustable Dumbbells (up to 52.5 lb)","brand":"Yes4All","category":"Dumbbells","goals":["strength"],"price":169.99,"asin":"B01AW6GQ8O"},
    {"id":3,"name":"Amazon Basics Cast Iron Kettlebell (15 lb)","brand":"Amazon Basics","category":"Kettlebell","goals":["strength","cardio"],"price":27.99,"asin":"B082LV1GNL"},
    {"id":4,"name":"Perfect Fitness Ab Carver Pro Roller","brand":"Perfect Fitness","category":"Core","goals":["strength"],"price":29.99,"asin":"B00K3W9YB2"},
    {"id":5,"name":"Sportneer Adjustable Ankle Weights (Pair)","brand":"Sportneer","category":"Resistance","goals":["strength"],"price":19.99,"asin":"B07R1Q9SF3"},
    # ─── Cardio ─────────────────────────────────────────────────────────
    {"id":6,"name":"WOD Nation Speed Jump Rope","brand":"WOD Nation","category":"Cardio","goals":["cardio"],"price":11.99,"asin":"B01LXD2E3R"},
    {"id":7,"name":"JOROTO X2 Pro Exercise Bike (Stationary)","brand":"JOROTO","category":"Cardio","goals":["cardio"],"price":459.00,"asin":"B08RDK4XBJ"},
    {"id":8,"name":"JumpSport Fitness Trampoline (Model 220)","brand":"JumpSport","category":"Cardio","goals":["cardio"],"price":249.00,"asin":"B004AN5GZY"},
    # ─── Yoga / Pilates ─────────────────────────────────────────────────
    {"id":9,"name":"Manduka PRO Yoga Mat (6mm, Black)","brand":"Manduka","category":"Mats","goals":["yoga"],"price":120.00,"asin":"B000G62DXC"},
    {"id":10,"name":"Gaiam Essentials Thick Yoga Mat (1/2 inch)","brand":"Gaiam","category":"Mats","goals":["yoga"],"price":21.99,"asin":"B01N1WRW62"},
    {"id":11,"name":"Trideer Yoga Ball (65 cm, Swiss Ball)","brand":"Trideer","category":"Flexibility","goals":["yoga","recovery","strength"],"price":17.99,"asin":"B07Z39KSDL"},
    {"id":12,"name":"Gaiam Yoga Block + Strap Set","brand":"Gaiam","category":"Accessories","goals":["yoga"],"price":14.99,"asin":"B01LWYGHV8"},
    # ─── Recovery ───────────────────────────────────────────────────────
    {"id":13,"name":"TriggerPoint Grid Foam Roller (26 inch)","brand":"TriggerPoint","category":"Recovery","goals":["recovery"],"price":36.00,"asin":"B0040EGNIU"},
    {"id":14,"name":"Theragun Mini Massage Gun","brand":"Theragun","category":"Recovery","goals":["recovery"],"price":179.00,"asin":"B07Y3WYLZS"},
    {"id":15,"name":"RENPHO Foot Massager with Heat","brand":"RENPHO","category":"Recovery","goals":["recovery"],"price":109.99,"asin":"B07R7Y6MM2"},
    # ─── General / Multi‑purpose ────────────────────────────────────────
    {"id":16,"name":"BalanceFrom GoFit High Density Exercise Mat (72x24, 1 inch)","brand":"BalanceFrom","category":"Mats","goals":["general"],"price":29.99,"asin":"B0139FYTAS"},
    {"id":17,"name":"Fit Simplify Resistance Loop Bands Set of 5","brand":"Fit Simplify","category":"Resistance","goals":["strength","recovery","general"],"price":11.95,"asin":"B01AVJB9MO"},
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
<title>At‑Home Workout Gear</title>
<style>
  body { font-family:Arial; max-width:750px; margin:40px auto; background:#f5f7fa; color:#222; }
  h1 { color:#2c5f8a; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:12px; }
  select, input[type=number], button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#2c5f8a; color:white; cursor:pointer; }
  .product { border-bottom:1px solid #eee; padding:12px 0; display:flex; justify-content:space-between; align-items:center; }
  .product-info { flex:1; }
  .product-info strong { font-size:1.05em; }
  .price { color:#888; }
  a { color:#2c5f8a; font-weight:bold; }
  .small { font-size:0.9em; color:#888; margin-top:10px; }
</style>
</head>
<body>
<h1>🏠 Your Home Gym Gear</h1>
<p>Choose your fitness goal and we'll recommend the best at‑home equipment with affiliate links.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Primary Goal</label>
    <select name="goal">
      <option value="">All Gear</option>
      <option value="strength" {% if q.goal=='strength' %}selected{% endif %}>Strength / Toning</option>
      <option value="cardio" {% if q.goal=='cardio' %}selected{% endif %}>Cardio</option>
      <option value="yoga" {% if q.goal=='yoga' %}selected{% endif %}>Yoga / Pilates</option>
      <option value="recovery" {% if q.goal=='recovery' %}selected{% endif %}>Recovery / Mobility</option>
      <option value="general" {% if q.goal=='general' %}selected{% endif %}>General / Multi‑Use</option>
    </select>
    <label>Max Price (optional)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="10" placeholder="No limit">
    <button type="submit">Find Gear</button>
  </div>
</form>

{% if results is defined %}
<div class="card">
  <h2>Your Recommendations ({{ results|length }})</h2>
  {% for item in results %}
  <div class="product">
    <div class="product-info">
      <strong>{{ item.name }}</strong> – {{ item.brand }}<br/>
      <span class="price">{{ item.category }} · ${{ "%.2f"|format(item.price) }}</span>
    </div>
    <a href="{{ item.aff_link }}" target="_blank">Shop →</a>
  </div>
  {% endfor %}
  {% if results|length == 0 %}<p>No items match. Try a different goal or a higher budget.</p>{% endif %}
</div>
{% endif %}
<p class="small">Prices may vary. Affiliate links included – we may earn a commission.</p>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    q = {
        "goal": request.args.get("goal", "").strip().lower(),
        "max_price": request.args.get("max_price", "").strip()
    }

    results = None
    if q["goal"]:
        max_price = None
        if q["max_price"]:
            try:
                max_price = float(q["max_price"])
            except ValueError:
                pass

        # Filter products by goal
        filtered = []
        for p in PRODUCTS:
            if q["goal"] not in p["goals"]:
                continue
            if max_price is not None and p["price"] > max_price:
                continue
            filtered.append(p)

        affiliate_tag = cfg.get("amazon_affiliate_tag", "")
        for item in filtered:
            item["aff_link"] = amazon_link(item["asin"], affiliate_tag)

        post_to_hub(
            f"🏋️ {len(filtered)} workout gear items for '{q['goal']}'",
            "info",
            {"goal": q["goal"], "count": len(filtered)}
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
            f"Config created at {CFG_FILE}. Add your Amazon affiliate tag to earn commissions.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    start_heartbeat()

    port = config.get("web_port", 5082)
    post_to_hub(f"🏋️ Workout Gear Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
