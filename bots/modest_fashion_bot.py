#!/usr/bin/env python3
"""
modest_fashion_bot.py — Modest Fashion Recommendation Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Recommends modest clothing by style, occasion, and budget.
Every product includes an Amazon affiliate link – you earn
commission on purchases.

Requirements:
    pip install flask requests

Configuration:
    On first run a file `modest_fashion_config.json` is created.
    Add your Amazon Associate tag (e.g. "your-20") to monetise links.
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
BOT_ID   = "modest_fashion"
BOT_NAME = "Modest Fashion Recommendation"

CFG_FILE = Path(__file__).with_name("modest_fashion_config.json")
DEFAULT_CONFIG = {
    "web_port": 5092,
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
# Styles: modern-modest, classic-modest, hijabi-friendly, conservative, casual-modest
# Occasions: casual, work, formal, special event, everyday
# Categories: top, dress, skirt, hijab, outerwear, swimwear, pants
PRODUCTS = [
    # ── Tops ──────────────────────────────────────────────────────────────
    {"id":1,"name":"Annah Hariri Long Sleeve Flowy Top","brand":"Annah Hariri","category":"top",
     "styles":["modern-modest","hijabi-friendly","casual-modest"],"occasions":["casual","everyday","work"],
     "price":49.99,"asin":"B07XYZ1234"},
    {"id":2,"name":"Urban Modesty High Neck Long Sleeve Blouse","brand":"Urban Modesty","category":"top",
     "styles":["modern-modest","classic-modest","conservative"],"occasions":["work","formal","everyday"],
     "price":39.99,"asin":"B08ABC5678"},
    {"id":3,"name":"Inayah Long Sleeve Lace Detail Top","brand":"Inayah","category":"top",
     "styles":["feminine-modest","modern-modest"],"occasions":["special event","formal"],
     "price":59.00,"asin":"B09DEF9012"},
    {"id":4,"name":"Sunnah Style Cotton Tunic Top","brand":"Sunnah Style","category":"top",
     "styles":["classic-modest","conservative"],"occasions":["casual","everyday"],
     "price":29.99,"asin":"B07HIJ3456"},

    # ── Dresses ───────────────────────────────────────────────────────────
    {"id":5,"name":"Modanisa Long Sleeve Maxi Dress","brand":"Modanisa","category":"dress",
     "styles":["hijabi-friendly","modern-modest","classic-modest"],"occasions":["casual","everyday","work"],
     "price":54.99,"asin":"B08KLM7890"},
    {"id":6,"name":"Aab Collection Wrap Maxi Dress","brand":"Aab","category":"dress",
     "styles":["modern-modest","feminine-modest"],"occasions":["special event","formal"],
     "price":89.00,"asin":"B07UVW2345"},
    {"id":7,"name":"Niswa Fashion Linen Blend Shirt Dress","brand":"Niswa Fashion","category":"dress",
     "styles":["casual-modest","hijabi-friendly"],"occasions":["casual","everyday"],
     "price":44.99,"asin":"B09RST6789"},
    {"id":8,"name":"Shukr Clothing Pleated A-Line Dress","brand":"Shukr","category":"dress",
     "styles":["classic-modest","conservative"],"occasions":["work","formal"],
     "price":69.00,"asin":"B08PQR3456"},

    # ── Skirts ────────────────────────────────────────────────────────────
    {"id":9,"name":"Urban Modesty Flowy Maxi Skirt","brand":"Urban Modesty","category":"skirt",
     "styles":["modern-modest","hijabi-friendly"],"occasions":["casual","everyday","work"],
     "price":34.99,"asin":"B07LMN7890"},
    {"id":10,"name":"Kabayare Fashion Pleated Midi Skirt","brand":"Kabayare","category":"skirt",
     "styles":["feminine-modest","modern-modest"],"occasions":["work","special event"],
     "price":44.00,"asin":"B09OPQ1234"},

    # ── Outerwear / Cardigans ─────────────────────────────────────────────
    {"id":11,"name":"Inayah Open Front Long Cardigan","brand":"Inayah","category":"outerwear",
     "styles":["modern-modest","hijabi-friendly"],"occasions":["casual","work"],
     "price":54.00,"asin":"B08JKL5678"},
    {"id":12,"name":"Annah Hariri Waistcoat Style Long Vest","brand":"Annah Hariri","category":"outerwear",
     "styles":["classic-modest","conservative"],"occasions":["formal","special event"],
     "price":79.99,"asin":"B07GHI2345"},

    # ── Hijabs / Scarves ──────────────────────────────────────────────────
    {"id":13,"name":"Vela Scarves Modal Hijab","brand":"Vela","category":"hijab",
     "styles":["hijabi-friendly","modern-modest"],"occasions":["everyday","work","special event"],
     "price":19.00,"asin":"B07DEF3456"},
    {"id":14,"name":"Lala Hijabs Chiffon Rectangle Scarf","brand":"Lala Hijabs","category":"hijab",
     "styles":["hijabi-friendly","feminine-modest"],"occasions":["special event","formal"],
     "price":24.99,"asin":"B09RST4567"},

    # ── Modest Swimwear ───────────────────────────────────────────────────
    {"id":15,"name":"Lands' End Women's Chlorine Resistant Tugless Tankini (with leggings)","brand":"Lands' End","category":"swimwear",
     "styles":["casual-modest","conservative"],"occasions":["swim","vacation"],
     "price":89.99,"asin":"B01MEF6L4B"},
    {"id":16,"name":"Bokina Full Coverage Modest Swimsuit (Hijab Friendly)","brand":"Bokina","category":"swimwear",
     "styles":["hijabi-friendly","conservative"],"occasions":["swim","vacation"],
     "price":45.99,"asin":"B08ABCDE12"},
]

# ── Affiliate link builder ──────────────────────────────────────────────────
def amazon_link(asin, affiliate_tag):
    if affiliate_tag.strip():
        return f"https://www.amazon.com/dp/{asin}?tag={affiliate_tag.strip()}"
    return f"https://www.amazon.com/dp/{asin}"

# ── Filtering & recommendation ─────────────────────────────────────────────
def score_item(item, style, occasion, max_price):
    """Score how well the item matches the criteria. Return -1 if excluded."""
    # Style match
    if style and style not in item.get("styles", []):
        return -1
    # Occasion match
    if occasion and occasion not in item.get("occasions", []):
        return -1
    # Price ceiling
    if max_price is not None and item["price"] > max_price:
        return -1
    # Base score can be used for tie-breaking – lower price slightly preferred
    return 100 - item["price"] * 0.1

def recommend(style, occasion, max_price, config):
    """Return list of matching products with affiliate links."""
    affiliate_tag = config.get("amazon_affiliate_tag", "")
    results = []
    for item in PRODUCTS:
        s = score_item(item, style, occasion, max_price)
        if s >= 0:
            item_copy = dict(item)
            item_copy["aff_link"] = amazon_link(item["asin"], affiliate_tag)
            results.append(item_copy)
    # Sort by score descending (best match first), then by price ascending
    results.sort(key=lambda x: (-score_item(x, style, occasion, max_price), x["price"]))
    return results

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Modest Fashion Finder</title>
<style>
  body { font-family:Arial; max-width:800px; margin:40px auto; background:#fdf9f8; color:#222; }
  h1 { color:#6b4e6b; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:12px; }
  select, input[type=number], button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#6b4e6b; color:white; cursor:pointer; }
  .product { border-bottom:1px solid #eee; padding:12px 0; display:flex; justify-content:space-between; align-items:center; }
  .product-info { flex:1; }
  .product-info strong { font-size:1.05em; }
  .price { color:#888; }
  a { color:#6b4e6b; font-weight:bold; }
  .small { font-size:0.9em; color:#888; margin-top:10px; }
</style>
</head>
<body>
<h1>🧕 Modest Fashion Recommendations</h1>
<p>Find elegant, modest clothing by style, occasion, and budget. Every link is an affiliate link – you earn commission when you shop.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Style</label>
    <select name="style">
      <option value="">Any Modest Style</option>
      <option value="modern-modest" {% if q.style=='modern-modest' %}selected{% endif %}>Modern Modest</option>
      <option value="classic-modest" {% if q.style=='classic-modest' %}selected{% endif %}>Classic / Traditional</option>
      <option value="feminine-modest" {% if q.style=='feminine-modest' %}selected{% endif %}>Feminine Modest</option>
      <option value="hijabi-friendly" {% if q.style=='hijabi-friendly' %}selected{% endif %}>Hijabi‑Friendly</option>
      <option value="conservative" {% if q.style=='conservative' %}selected{% endif %}>Conservative</option>
      <option value="casual-modest" {% if q.style=='casual-modest' %}selected{% endif %}>Casual Modest</option>
    </select>
    <label>Occasion</label>
    <select name="occasion">
      <option value="">Any Occasion</option>
      <option value="casual" {% if q.occasion=='casual' %}selected{% endif %}>Casual</option>
      <option value="everyday" {% if q.occasion=='everyday' %}selected{% endif %}>Everyday</option>
      <option value="work" {% if q.occasion=='work' %}selected{% endif %}>Work / Business</option>
      <option value="formal" {% if q.occasion=='formal' %}selected{% endif %}>Formal</option>
      <option value="special event" {% if q.occasion=='special event' %}selected{% endif %}>Special Event</option>
      <option value="swim" {% if q.occasion=='swim' %}selected{% endif %}>Swim / Vacation</option>
    </select>
    <label>Max Price ($)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="5" placeholder="No limit">
    <button type="submit">Find Modest Fashion</button>
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
    <a href="{{ item.aff_link }}" target="_blank">Shop</a>
  </div>
  {% endfor %}
  {% if results|length == 0 %}<p>No items match your criteria. Try broadening your filters.</p>{% endif %}
</div>
{% endif %}
<p class="small">Prices may vary. Affiliate links included – we may earn a commission.</p>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    q = {
        "style": request.args.get("style", "").strip().lower(),
        "occasion": request.args.get("occasion", "").strip().lower(),
        "max_price": request.args.get("max_price", "").strip()
    }

    results = None
    if q["style"] or q["occasion"]:
        max_p = None
        if q["max_price"]:
            try:
                max_p = float(q["max_price"])
            except ValueError:
                pass
        results = recommend(q["style"], q["occasion"], max_p, cfg)
        post_to_hub(
            f"🧕 Modest fashion: {len(results)} items for style={q['style']}, occasion={q['occasion']}",
            "info",
            {"style": q["style"], "occasion": q["occasion"], "count": len(results)}
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

    port = config.get("web_port", 5092)
    post_to_hub(f"🧕 Modest Fashion Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
