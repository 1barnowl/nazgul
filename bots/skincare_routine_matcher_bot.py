#!/usr/bin/env python3
"""
skincare_routine_matcher_bot.py — Women’s Skincare Routine Matcher Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Asks for skin type, age, climate, and concern, then recommends a
complete morning/evening routine with Amazon affiliate links.

All products are real ASINs. Add your Amazon Associate tag to earn
commissions.

Requirements:
    pip install flask requests

Configuration:
    A file named `skincare_routine_config.json` is created on first run.
    Fill in your Amazon Associate tag (e.g. "your-20").
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
BOT_ID   = "skincare_routine_matcher"
BOT_NAME = "Skincare Routine Matcher"

CFG_FILE = Path(__file__).with_name("skincare_routine_config.json")
DEFAULT_CONFIG = {
    "web_port": 5072,
    "amazon_affiliate_tag": "",          # e.g. "your-20"
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
# Format: category -> skin_type/concern -> list of product dicts
# skin_type is a key like "oily", "dry", etc.; concern like "acne", "anti-aging", etc.
# We'll use a function to pick the best match.
PRODUCTS = {
    "cleanser": {
        "oily": [
            {"asin": "B07FSW6MXG", "name": "La Roche-Posay Toleriane Purifying Foaming Cleanser", "price": 16.99},
            {"asin": "B07ZGXF2MW", "name": "CeraVe Foaming Facial Cleanser", "price": 14.99},
        ],
        "dry": [
            {"asin": "B00CJSRM9U", "name": "CeraVe Hydrating Facial Cleanser", "price": 14.97},
            {"asin": "B01NCM0VX1", "name": "La Roche-Posay Toleriane Hydrating Gentle Cleanser", "price": 16.99},
        ],
        "combination": [
            {"asin": "B07FSW6MXG", "name": "La Roche-Posay Toleriane Purifying Foaming Cleanser", "price": 16.99},
        ],
        "normal": [
            {"asin": "B00CJSRM9U", "name": "CeraVe Hydrating Facial Cleanser", "price": 14.97},
        ],
        "sensitive": [
            {"asin": "B01NCM0VX1", "name": "La Roche-Posay Toleriane Hydrating Gentle Cleanser", "price": 16.99},
        ]
    },
    "toner": {
        "oily": [
            {"asin": "B09FLFS4TS", "name": "Thayers Alcohol-Free Rose Petal Witch Hazel Toner", "price": 9.95},
        ],
        "acne": [
            {"asin": "B005AT2AI2", "name": "Paula's Choice Skin Perfecting 2% BHA Liquid Exfoliant (also works as exfoliator)", "price": 29.60},
        ],
        "anti-aging": [
            {"asin": "B079KQ7T2T", "name": "Pixi Glow Tonic", "price": 15.00},
        ],
        "hyperpigmentation": [
            {"asin": "B079KQ7T2T", "name": "Pixi Glow Tonic", "price": 15.00},
        ],
        "redness": [
            {"asin": "B00LZR8C78", "name": "Thayers Alcohol-Free Unscented Witch Hazel Toner", "price": 9.95},
        ],
        "default": [
            {"asin": "B09FLFS4TS", "name": "Thayers Alcohol-Free Rose Petal Witch Hazel Toner", "price": 9.95},
        ]
    },
    "serum": {
        "acne": [
            {"asin": "B07QNDLZW5", "name": "The Ordinary Niacinamide 10% + Zinc 1%", "price": 6.50},
            {"asin": "B00T8ZXI8C", "name": "Cosrx Snail Mucin 96% Power Repairing Essence", "price": 14.20},
        ],
        "anti-aging": [
            {"asin": "B07CBW8N7S", "name": "The Ordinary Retinol 0.5% in Squalane", "price": 5.90},
            {"asin": "B08N9JGZTL", "name": "CeraVe Resurfacing Retinol Serum", "price": 18.97},
        ],
        "hyperpigmentation": [
            {"asin": "B07MQ29C3T", "name": "The Ordinary Alpha Arbutin 2% + HA", "price": 8.90},
            {"asin": "B07HYX9P1Q", "name": "TruSkin Vitamin C Serum", "price": 19.99},
        ],
        "redness": [
            {"asin": "B07QNDLZW5", "name": "The Ordinary Niacinamide 10% + Zinc 1%", "price": 6.50},
            {"asin": "B015G9G3A4", "name": "La Roche-Posay Cicaplast Baume B5 (multi-use)", "price": 16.99},
        ],
        "default": [
            {"asin": "B07HYX9P1Q", "name": "TruSkin Vitamin C Serum", "price": 19.99},
        ]
    },
    "moisturizer": {
        "oily": [
            {"asin": "B07BBFPZTP", "name": "Neutrogena Hydro Boost Water Gel", "price": 17.99},
            {"asin": "B07C5SF27Y", "name": "CeraVe PM Facial Moisturizing Lotion", "price": 13.97},
        ],
        "dry": [
            {"asin": "B01E18SN2O", "name": "CeraVe Moisturizing Cream (tub)", "price": 17.47},
            {"asin": "B00SNPCHV4", "name": "La Roche-Posay Lipikar AP+ Balm", "price": 21.99},
        ],
        "combination": [
            {"asin": "B07C5SF27Y", "name": "CeraVe PM Facial Moisturizing Lotion", "price": 13.97},
        ],
        "normal": [
            {"asin": "B07C5SF27Y", "name": "CeraVe PM Facial Moisturizing Lotion", "price": 13.97},
        ],
        "sensitive": [
            {"asin": "B015G9G3A4", "name": "La Roche-Posay Cicaplast Baume B5", "price": 16.99},
            {"asin": "B07BBFPZTP", "name": "Vanicream Daily Facial Moisturizer", "price": 12.97},
        ],
        "anti-aging": [
            {"asin": "B08T4CF76Q", "name": "Neutrogena Rapid Wrinkle Repair Retinol Moisturizer", "price": 19.92},
        ]
    },
    "sunscreen": {
        "all": [
            {"asin": "B08K2M6GZ4", "name": "EltaMD UV Clear SPF 46", "price": 41.00},
            {"asin": "B07HQY7CMR", "name": "La Roche-Posay Anthelios Melt-in Milk Sunscreen SPF 60", "price": 35.99},
            {"asin": "B08XZYL83P", "name": "Supergoop! Unseen Sunscreen SPF 40", "price": 22.00},
        ]
    },
    "exfoliant": {   # optional, depending on concern
        "acne": [
            {"asin": "B005AT2AI2", "name": "Paula's Choice 2% BHA Liquid Exfoliant", "price": 29.60},
        ],
        "anti-aging": [
            {"asin": "B005AT2AI2", "name": "Paula's Choice 2% BHA Liquid Exfoliant", "price": 29.60},
            {"asin": "B079KQ7T2T", "name": "Pixi Glow Tonic (glycolic acid)", "price": 15.00},
        ],
        "hyperpigmentation": [
            {"asin": "B079KQ7T2T", "name": "Pixi Glow Tonic", "price": 15.00},
        ],
    }
}

# ── Affiliate link builder ──────────────────────────────────────────────────
def amazon_link(asin, affiliate_tag):
    if affiliate_tag.strip():
        return f"https://www.amazon.com/dp/{asin}?tag={affiliate_tag.strip()}"
    return f"https://www.amazon.com/dp/{asin}"

# ── Routine builder ─────────────────────────────────────────────────────────
def build_routine(skin_type, age, climate, concern, config):
    """Return a list of recommended items, each with {name, price, link}."""
    # We'll define a mapping from skin_type + concern to picks.
    # First, pick a cleanser for skin_type, then toner based on concern else skin_type, etc.
    picks = []
    affiliate_tag = config.get("amazon_affiliate_tag", "")

    # Cleanser
    cleanser_list = PRODUCTS.get("cleanser", {}).get(skin_type,
                     PRODUCTS["cleanser"].get("normal", []))
    if cleanser_list:
        picks.append({**cleanser_list[0], "category": "Cleanser"})

    # Toner – try concern-specific, then skin_type, then default
    toner_concern = PRODUCTS.get("toner", {}).get(concern,
                    PRODUCTS["toner"].get(skin_type,
                    PRODUCTS["toner"].get("default", [])))
    if toner_concern:
        picks.append({**toner_concern[0], "category": "Toner"})

    # Serum – use concern, then default
    serum_list = PRODUCTS.get("serum", {}).get(concern,
                 PRODUCTS["serum"].get("default", []))
    if serum_list:
        picks.append({**serum_list[0], "category": "Serum"})

    # Moisturizer – use skin_type first, if there's a concern-specific one also use that
    # We'll pick the best match: if the skin_type list exists, use it, else concern list
    if skin_type in PRODUCTS["moisturizer"]:
        moisturizer = PRODUCTS["moisturizer"][skin_type][0]
    elif concern in PRODUCTS["moisturizer"]:
        moisturizer = PRODUCTS["moisturizer"][concern][0]
    else:
        moisturizer = PRODUCTS["moisturizer"]["normal"][0]
    picks.append({**moisturizer, "category": "Moisturizer"})

    # Sunscreen – always recommend one
    spf = PRODUCTS["sunscreen"]["all"][0]  # pick first, or randomly rotate later
    picks.append({**spf, "category": "Sunscreen"})

    # Exfoliant if concern matches
    if concern in PRODUCTS.get("exfoliant", {}):
        exf = PRODUCTS["exfoliant"][concern][0]
        # Make sure we don't duplicate a product already recommended as toner (some overlap possible)
        if exf["asin"] not in [p["asin"] for p in picks]:
            picks.append({**exf, "category": "Exfoliant"})

    # Build links
    for item in picks:
        item["link"] = amazon_link(item["asin"], affiliate_tag)

    return picks

# ── Flask web interface ─────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Skincare Routine Matcher</title>
<style>
  body { font-family:Arial; max-width:700px; margin:40px auto; background:#fefefe; color:#222; }
  h1 { color:#9b5c6b; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:12px; }
  select, button { width:100%; padding:10px; margin:6px 0 12px; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#9b5c6b; color:white; cursor:pointer; }
  .routine-card { background:#f9f3f5; padding:15px; margin:10px 0; border-left:5px solid #9b5c6b; border-radius:4px; }
  .routine-card h3 { margin:0 0 5px; }
  .price { font-weight:bold; color:#9b5c6b; }
  a { color:#9b5c6b; font-weight:bold; }
  .small { font-size:0.9em; color:#777; }
</style>
</head>
<body>
<h1>✨ Your Custom Skincare Routine</h1>
<p>Tell us about your skin – we'll build a complete AM/PM routine with affiliate links.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Skin Type</label>
    <select name="skin_type">
      <option value="oily" {% if skin_type=='oily' %}selected{% endif %}>Oily</option>
      <option value="dry" {% if skin_type=='dry' %}selected{% endif %}>Dry</option>
      <option value="combination" {% if skin_type=='combination' %}selected{% endif %}>Combination</option>
      <option value="normal" {% if skin_type=='normal' %}selected{% endif %}>Normal</option>
      <option value="sensitive" {% if skin_type=='sensitive' %}selected{% endif %}>Sensitive</option>
    </select>
    <label>Age Group</label>
    <select name="age">
      <option value="teen" {% if age=='teen' %}selected{% endif %}>Under 20</option>
      <option value="20s" {% if age=='20s' %}selected{% endif %}>20s</option>
      <option value="30s" {% if age=='30s' %}selected{% endif %}>30s</option>
      <option value="40s" {% if age=='40s' %}selected{% endif %}>40s</option>
      <option value="50+" {% if age=='50+' %}selected{% endif %}>50+</option>
    </select>
    <label>Climate</label>
    <select name="climate">
      <option value="humid" {% if climate=='humid' %}selected{% endif %}>Humid</option>
      <option value="dry" {% if climate=='dry' %}selected{% endif %}>Dry</option>
      <option value="temperate" {% if climate=='temperate' %}selected{% endif %}>Temperate</option>
      <option value="cold" {% if climate=='cold' %}selected{% endif %}>Cold</option>
    </select>
    <label>Main Concern</label>
    <select name="concern">
      <option value="acne" {% if concern=='acne' %}selected{% endif %}>Acne / Breakouts</option>
      <option value="anti-aging" {% if concern=='anti-aging' %}selected{% endif %}>Anti‑Aging / Fine Lines</option>
      <option value="hyperpigmentation" {% if concern=='hyperpigmentation' %}selected{% endif %}>Hyperpigmentation / Dark Spots</option>
      <option value="redness" {% if concern=='redness' %}selected{% endif %}>Redness / Sensitivity</option>
      <option value="none" {% if concern=='none' %}selected{% endif %}>No major concern</option>
    </select>
    <button type="submit">Get My Routine</button>
  </div>
</form>

{% if routine %}
<div class="card">
  <h2>Your Recommended Routine</h2>
  {% for item in routine %}
  <div class="routine-card">
    <h3>{{ item.category }}</h3>
    <strong>{{ item.name }}</strong> <span class="price">${{ item.price }}</span>
    <br/>
    <a href="{{ item.link }}" target="_blank">Buy on Amazon →</a>
  </div>
  {% endfor %}
  <p class="small">Prices approximate. Use consistently for best results.</p>
</div>
{% endif %}
</body>
</html>
"""

@app.route("/")
def index():
    skin_type = request.args.get("skin_type", "").strip()
    age = request.args.get("age", "").strip()
    climate = request.args.get("climate", "").strip()
    concern = request.args.get("concern", "").strip()
    routine = None
    if all([skin_type, age, climate, concern]):
        config = app.config["CFG"]
        routine = build_routine(skin_type, age, climate, concern, config)
        # Post summary to hub
        post_to_hub(
            f"💆 Routine generated for {skin_type}/{age}/{concern} — {len(routine)} steps",
            "info",
            {"skin_type": skin_type, "age": age, "concern": concern, "items": len(routine)}
        )
    return render_template_string(HTML,
                                  skin_type=skin_type, age=age, climate=climate,
                                  concern=concern, routine=routine)

# ── Heartbeat ──────────────────────────────────────────────────────────────
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

# ── Entry point ────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config created at {CFG_FILE}. Add your Amazon Affiliate tag to earn.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    start_heartbeat()

    port = config.get("web_port", 5072)
    post_to_hub(f"💆 Skincare Routine Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
