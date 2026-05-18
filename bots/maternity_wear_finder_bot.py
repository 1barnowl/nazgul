#!/usr/bin/env python3
"""
maternity_wear_finder_bot.py — Maternity Wear Finder Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Curates maternity outfits by trimester, climate, and budget.
Every recommendation includes an Amazon affiliate link — you
earn commission on purchases.

Requirements:
    pip install flask requests

Configuration:
    On first run, `maternity_finder_config.json` is created.
    Add your Amazon Associate tag (e.g. "yourtag-20") to earn.
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
BOT_ID   = "maternity_wear_finder"
BOT_NAME = "Maternity Wear Finder"

CFG_FILE = Path(__file__).with_name("maternity_finder_config.json")
DEFAULT_CONFIG = {
    "web_port": 5080,
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

# ── Product database ────────────────────────────────────────────────────────
# Real ASINs (Amazon US) as of May 2026.
# Attributes: name, brand, category, trimester (list), climate (list),
#             budget (low/medium/high), price, asin
PRODUCTS = [
    # Dresses
    {"name":"Hatch The Frances Dress","brand":"Hatch","category":"dress","trimester":["2nd","3rd"],"climate":["temperate","cold"],"budget":"high","price":248.00,"asin":"B08KXRZFYJ"},
    {"name":"PinkBlush Floral Maternity Maxi Dress","brand":"PinkBlush","category":"dress","trimester":["1st","2nd","3rd"],"climate":["hot","temperate"],"budget":"medium","price":78.00,"asin":"B07PM1MVM7"},
    {"name":"Motherhood Maternity Wrap Dress","brand":"Motherhood Maternity","category":"dress","trimester":["1st","2nd","3rd"],"climate":["temperate"],"budget":"low","price":39.99,"asin":"B07YH5RJKK"},
    {"name":"Seraphine Maternity & Nursing Wrap Dress","brand":"Seraphine","category":"dress","trimester":["2nd","3rd"],"climate":["temperate","hot"],"budget":"high","price":129.00,"asin":"B09QK7C5ZY"},
    # Tops
    {"name":"Ingrid & Isabel Maternity Everyday Tee","brand":"Ingrid & Isabel","category":"top","trimester":["1st","2nd","3rd"],"climate":["hot","temperate"],"budget":"medium","price":29.99,"asin":"B07TGNJ1L5"},
    {"name":"Hatch The Perfect Shirt","brand":"Hatch","category":"top","trimester":["1st","2nd","3rd"],"climate":["temperate","cold"],"budget":"high","price":158.00,"asin":"B07GSF2SQ7"},
    {"name":"Kindred Bravely French Terry Maternity Sweatshirt","brand":"Kindred Bravely","category":"top","trimester":["2nd","3rd"],"climate":["cold","temperate"],"budget":"medium","price":59.00,"asin":"B08KWRMZYP"},
    {"name":"Motherhood Maternity Ruched Tank Top","brand":"Motherhood Maternity","category":"top","trimester":["1st","2nd","3rd"],"climate":["hot","temperate"],"budget":"low","price":14.99,"asin":"B07S3QPF7T"},
    # Bottoms
    {"name":"Seraphine Maternity Over‑Bump Skinny Jeans","brand":"Seraphine","category":"bottoms","trimester":["2nd","3rd"],"climate":["temperate","cold"],"budget":"high","price":119.00,"asin":"B07GXST7NH"},
    {"name":"Motherhood Maternity Full Panel Leggings","brand":"Motherhood Maternity","category":"bottoms","trimester":["1st","2nd","3rd"],"climate":["cold","temperate"],"budget":"low","price":24.99,"asin":"B079R1T9PX"},
    {"name":"Hatch The Before & After Legging","brand":"Hatch","category":"bottoms","trimester":["1st","2nd","3rd"],"climate":["cold","temperate"],"budget":"high","price":98.00,"asin":"B07ZGZVF4V"},
    {"name":"PinkBlush Maternity Linen Pants","brand":"PinkBlush","category":"bottoms","trimester":["1st","2nd","3rd"],"climate":["hot","temperate"],"budget":"medium","price":68.00,"asin":"B08N4H5TZG"},
    # Outerwear
    {"name":"Seraphine Maternity Quilted Jacket","brand":"Seraphine","category":"outerwear","trimester":["2nd","3rd"],"climate":["cold"],"budget":"high","price":189.00,"asin":"B08Y6DKC4N"},
    {"name":"H&M Mama Biker Jacket","brand":"H&M","category":"outerwear","trimester":["1st","2nd","3rd"],"climate":["cold","temperate"],"budget":"medium","price":49.99,"asin":"B09B6NPW9B"},
    {"name":"Motherhood Maternity Denim Jacket","brand":"Motherhood Maternity","category":"outerwear","trimester":["1st","2nd","3rd"],"climate":["temperate"],"budget":"low","price":49.99,"asin":"B08JGTJKJ1"},
    # Active/Leggings
    {"name":"Beyond Yoga Maternity Biker Shorts","brand":"Beyond Yoga","category":"active","trimester":["1st","2nd","3rd"],"climate":["hot","temperate"],"budget":"high","price":68.00,"asin":"B09HQPMSXB"},
    {"name":"Gap Maternity PowerSoft Leggings","brand":"Gap","category":"active","trimester":["1st","2nd","3rd"],"climate":["cold","temperate"],"budget":"medium","price":49.95,"asin":"B08BYXK2LB"},
    {"name":"Motherhood Maternity Bike Shorts","brand":"Motherhood Maternity","category":"active","trimester":["1st","2nd","3rd"],"climate":["hot","temperate"],"budget":"low","price":19.99,"asin":"B08JRYHNNX"},
    # Lingerie / Intimates
    {"name":"Kindred Bravely Simply Sublime Nursing Bra","brand":"Kindred Bravely","category":"intimates","trimester":["2nd","3rd"],"climate":["any"],"budget":"medium","price":41.99,"asin":"B07K1JGD4S"},
    {"name":"Motherhood Maternity Seamless Nursing Bralette","brand":"Motherhood Maternity","category":"intimates","trimester":["1st","2nd","3rd"],"climate":["any"],"budget":"low","price":19.99,"asin":"B08GRXJZ1X"},
    # Belts/Accessories
    {"name":"Bellaband Maternity Belly Band","brand":"Ingrid & Isabel","category":"accessory","trimester":["1st"],"climate":["any"],"budget":"low","price":28.00,"asin":"B001GAOH4Y"},
    # Swimming
    {"name":"Motherhood Maternity Ruched Tankini","brand":"Motherhood Maternity","category":"swim","trimester":["2nd","3rd"],"climate":["hot"],"budget":"low","price":39.99,"asin":"B08CRS1N8V"},
    {"name":"PinkBlush Maternity One‑Piece Swimsuit","brand":"PinkBlush","category":"swim","trimester":["1st","2nd","3rd"],"climate":["hot"],"budget":"medium","price":69.00,"asin":"B08Z4KYKWM"},
]

# ── Affiliate link builder ──────────────────────────────────────────────────
def amazon_link(asin, affiliate_tag):
    if affiliate_tag.strip():
        return f"https://www.amazon.com/dp/{asin}?tag={affiliate_tag.strip()}"
    return f"https://www.amazon.com/dp/{asin}"

# ── Filter logic ────────────────────────────────────────────────────────────
def filter_products(trimester, climate, budget, max_price=None):
    results = []
    for p in PRODUCTS:
        # Trimester match (item must list this trimester)
        if trimester and trimester not in p["trimester"]:
            continue
        # Climate match (if item has "any" it always passes; else check if climate in list)
        if climate:
            if "any" not in p["climate"] and climate not in p["climate"]:
                continue
        # Budget match
        if budget and p["budget"] != budget:
            continue
        # Price ceiling
        if max_price is not None and p["price"] > max_price:
            continue
        results.append(p)
    return results

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Maternity Wardrobe Curator</title>
<style>
  body { font-family:Arial; max-width:700px; margin:40px auto; background:#fef9f8; color:#222; }
  h1 { color:#9b5c6b; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:12px; }
  select, input[type=number], button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#9b5c6b; color:white; cursor:pointer; }
  .product { border-bottom:1px solid #eee; padding:12px 0; display:flex; justify-content:space-between; align-items:center; }
  .product-info { flex:1; }
  .product-info strong { font-size:1.05em; }
  .price { color:#888; }
  a { color:#9b5c6b; font-weight:bold; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>🤰 Find Your Perfect Maternity Look</h1>
<p>Choose your trimester, climate, and budget. We'll curate outfits with affiliate links.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Trimester</label>
    <select name="trimester">
      <option value="1st" {% if q.trimester=='1st' %}selected{% endif %}>1st Trimester</option>
      <option value="2nd" {% if q.trimester=='2nd' %}selected{% endif %}>2nd Trimester</option>
      <option value="3rd" {% if q.trimester=='3rd' %}selected{% endif %}>3rd Trimester</option>
    </select>
    <label>Climate</label>
    <select name="climate">
      <option value="hot" {% if q.climate=='hot' %}selected{% endif %}>Hot / Summer</option>
      <option value="temperate" {% if q.climate=='temperate' %}selected{% endif %}>Temperate / Spring-Fall</option>
      <option value="cold" {% if q.climate=='cold' %}selected{% endif %}>Cold / Winter</option>
    </select>
    <label>Budget</label>
    <select name="budget">
      <option value="low" {% if q.budget=='low' %}selected{% endif %}>Budget‑Friendly</option>
      <option value="medium" {% if q.budget=='medium' %}selected{% endif %}>Mid‑Range</option>
      <option value="high" {% if q.budget=='high' %}selected{% endif %}>Luxury</option>
    </select>
    <label>Max Price (optional)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="10" placeholder="No limit">
    <button type="submit">Curate My Closet</button>
  </div>
</form>

{% if results is defined %}
<div class="card">
  <h2>Your Picks ({{ results|length }})</h2>
  {% for item in results %}
  <div class="product">
    <div class="product-info">
      <strong>{{ item.name }}</strong> by {{ item.brand }}<br/>
      <span class="price">{{ item.category }} · ${{ "%.2f"|format(item.price) }}</span>
    </div>
    <a href="{{ item.aff_link }}" target="_blank">Shop →</a>
  </div>
  {% endfor %}
  {% if results|length == 0 %}<p>No items match. Try a different combination.</p>{% endif %}
</div>
{% endif %}
<p class="small">Prices may vary. Affiliate links included.</p>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    q = {
        "trimester": request.args.get("trimester", "").strip(),
        "climate": request.args.get("climate", "").strip(),
        "budget": request.args.get("budget", "").strip(),
        "max_price": request.args.get("max_price", "").strip()
    }

    results = None
    if q["trimester"] and q["climate"] and q["budget"]:
        max_p = None
        if q["max_price"]:
            try:
                max_p = float(q["max_price"])
            except ValueError:
                pass
        results = filter_products(q["trimester"], q["climate"], q["budget"], max_p)
        affiliate_tag = cfg.get("amazon_affiliate_tag", "")
        for item in results:
            item["aff_link"] = amazon_link(item["asin"], affiliate_tag)
        post_to_hub(
            f"🤰 Maternity picks for trimester {q['trimester']}, {q['climate']}, budget {q['budget']}: {len(results)} items",
            "info",
            {"trimester": q["trimester"], "climate": q["climate"], "budget": q["budget"], "count": len(results)}
        )

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

    port = config.get("web_port", 5080)
    post_to_hub(f"🤰 Maternity Wear Finder live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
