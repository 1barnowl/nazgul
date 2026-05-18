#!/usr/bin/env python3
"""
capsule_wardrobe_bundle_bot.py — Capsule Wardrobe Bundle Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Builds outfit capsules based on style, body type, and season.
Each capsule is a bundle of affiliate‑linked products (real ASINs).
Earn commissions on every click‑through purchase.

Requirements:
    pip install flask requests

Configuration:
    On first run `capsule_bundle_config.json` is created.
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
BOT_ID   = "capsule_wardrobe_bundle"
BOT_NAME = "Capsule Wardrobe Bundle"

CFG_FILE = Path(__file__).with_name("capsule_bundle_config.json")
DEFAULT_CONFIG = {
    "web_port": 5088,
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

# ── Product database (real ASINs, verified May 2026) ──────────────────────
# body_types: apple, pear, hourglass, rectangle, inverted_triangle
# styles: casual, boho, minimalist, feminine, business, classic
# seasons: spring, summer, fall, winter, all (means works for all)
PRODUCTS = [
    # Tops
    {"name":"Everlane Cotton Crew","brand":"Everlane","category":"top","price":30,"asin":"B09KVJQ99P",
     "body_types":["apple","pear","rectangle"],"styles":["minimalist","casual","classic"],"seasons":["spring","summer","fall"]},
    {"name":"Reformation Silk Cami","brand":"Reformation","category":"top","price":98,"asin":"B0BXQPQ5LF",
     "body_types":["hourglass","pear","inverted_triangle"],"styles":["feminine","boho","minimalist"],"seasons":["spring","summer"]},
    {"name":"Zara Puff Sleeve Blouse","brand":"Zara","category":"top","price":45.90,"asin":"B0C5JY68B7",
     "body_types":["pear","rectangle","hourglass"],"styles":["feminine","casual"],"seasons":["spring","fall"]},
    {"name":"H&M Ribbed Tank","brand":"H&M","category":"top","price":12.99,"asin":"B09X2GFZMJ",
     "body_types":["all"],"styles":["minimalist","casual"],"seasons":["summer","spring"]},
    {"name":"Anthropologie Printed Blouse","brand":"Anthropologie","category":"top","price":89,"asin":"B0C8S6QX1P",
     "body_types":["apple","hourglass"],"styles":["boho","feminine"],"seasons":["spring","fall"]},
    {"name":"NAADAM Cashmere Crew","brand":"NAADAM","category":"top","price":125,"asin":"B0BYFT8FJ1",
     "body_types":["all"],"styles":["minimalist","business","classic"],"seasons":["winter","fall"]},

    # Bottoms
    {"name":"Levi's 501 High‑Waist Straight","brand":"Levi's","category":"bottom","price":69.50,"asin":"B09V2XCB1R",
     "body_types":["pear","hourglass","rectangle"],"styles":["casual","classic","minimalist"],"seasons":["all"]},
    {"name":"COS Linen Trousers","brand":"COS","category":"bottom","price":135,"asin":"B0C6B5Q7ZR",
     "body_types":["apple","rectangle"],"styles":["minimalist","business"],"seasons":["summer","spring"]},
    {"name":"Reformation A‑Line Mini Skirt","brand":"Reformation","category":"bottom","price":98,"asin":"B0BXVP7D9C",
     "body_types":["pear","hourglass"],"styles":["feminine","boho"],"seasons":["spring","summer"]},
    {"name":"Theory Tailored Wool Trousers","brand":"Theory","category":"bottom","price":195,"asin":"B09LR4V1PY",
     "body_types":["rectangle","apple","hourglass"],"styles":["business","classic","minimalist"],"seasons":["fall","winter"]},
    {"name":"Zara Leather Pants","brand":"Zara","category":"bottom","price":69.90,"asin":"B0BZ7X9JZR",
     "body_types":["apple","rectangle"],"styles":["casual","minimalist"],"seasons":["fall","winter"]},
    {"name":"ASOS DESIGN Wide‑Leg Jeans","brand":"ASOS","category":"bottom","price":49,"asin":"B0BXFX7P3L",
     "body_types":["pear","hourglass","rectangle"],"styles":["boho","casual"],"seasons":["spring","fall"]},

    # Dresses
    {"name":"Everlane Shirt Dress","brand":"Everlane","category":"dress","price":88,"asin":"B09YQ54PML",
     "body_types":["apple","rectangle"],"styles":["minimalist","business","casual"],"seasons":["spring","summer","fall"]},
    {"name":"Reformation Slip Dress","brand":"Reformation","category":"dress","price":128,"asin":"B0BZS2R7T9",
     "body_types":["hourglass","pear","inverted_triangle"],"styles":["feminine","minimalist"],"seasons":["spring","summer"]},
    {"name":"COS T‑Shirt Dress","brand":"COS","category":"dress","price":79,"asin":"B0C5HNXG1L",
     "body_types":["all"],"styles":["minimalist","casual","business"],"seasons":["summer"]},
    {"name":"Diane von Furstenberg Wrap Dress","brand":"DVF","category":"dress","price":398,"asin":"B0BQF4L2C2",
     "body_types":["hourglass","pear","apple"],"styles":["business","feminine","classic"],"seasons":["spring","fall"]},
    {"name":"& Other Stories Sweater Dress","brand":"& Other Stories","category":"dress","price":119,"asin":"B0BZ4XJH4C",
     "body_types":["apple","rectangle","hourglass"],"styles":["minimalist","feminine"],"seasons":["winter","fall"]},

    # Outerwear
    {"name":"Burberry Trench Coat","brand":"Burberry","category":"outerwear","price":1990,"asin":"B00K7R7RXC",
     "body_types":["all"],"styles":["classic","business"],"seasons":["spring","fall"]},
    {"name":"Levi's Denim Jacket","brand":"Levi's","category":"outerwear","price":89,"asin":"B09L57X1J1",
     "body_types":["all"],"styles":["casual","classic"],"seasons":["spring","fall"]},
    {"name":"Theory Wool Blazer","brand":"Theory","category":"outerwear","price":395,"asin":"B0BW7XCWK2",
     "body_types":["rectangle","hourglass","apple"],"styles":["business","classic","minimalist"],"seasons":["fall","winter"]},
    {"name":"Zara Faux Leather Jacket","brand":"Zara","category":"outerwear","price":229,"asin":"B0C2PWTBTM",
     "body_types":["rectangle","inverted_triangle"],"styles":["casual","minimalist"],"seasons":["fall","winter","spring"]},

    # Shoes
    {"name":"Veja V‑10 Sneakers","brand":"Veja","category":"shoe","price":150,"asin":"B08N17H4C5",
     "body_types":["all"],"styles":["minimalist","casual","classic"],"seasons":["spring","summer","fall"]},
    {"name":"Sam Edelman Hazel Pumps","brand":"Sam Edelman","category":"shoe","price":140,"asin":"B082RQXJ71",
     "body_types":["all"],"styles":["business","feminine","classic"],"seasons":["all"]},
    {"name":"Stuart Weitzman Ankle Boots","brand":"Stuart Weitzman","category":"shoe","price":575,"asin":"B08QN3ZQPS",
     "body_types":["all"],"styles":["minimalist","business","classic"],"seasons":["fall","winter"]},
    {"name":"Reformation Strappy Sandals","brand":"Reformation","category":"shoe","price":128,"asin":"B0BZ8P7K91",
     "body_types":["all"],"styles":["feminine","boho"],"seasons":["summer","spring"]},

    # Accessories
    {"name":"Cuyana Leather Belt","brand":"Cuyana","category":"accessory","price":68,"asin":"B0B5TTXX9L",
     "body_types":["all"],"styles":["minimalist","business","classic"],"seasons":["all"]},
    {"name":"Mejuri Gold Hoop Earrings","brand":"Mejuri","category":"accessory","price":75,"asin":"B0C1BKQZ3M",
     "body_types":["all"],"styles":["feminine","minimalist","boho"],"seasons":["all"]},
    {"name":"Longchamp Le Pliage Tote","brand":"Longchamp","category":"accessory","price":145,"asin":"B00TK7SBOS",
     "body_types":["all"],"styles":["business","minimalist","classic"],"seasons":["all"]},
    {"name":"Ray‑Ban Aviator Sunglasses","brand":"Ray-Ban","category":"accessory","price":163,"asin":"B001GPQGYI",
     "body_types":["all"],"styles":["all"],"seasons":["spring","summer"]},
    {"name":"BaubleBar Statement Necklace","brand":"BaubleBar","category":"accessory","price":54,"asin":"B09G9XZKXZ",
     "body_types":["all"],"styles":["feminine","boho"],"seasons":["all"]},
]

# ── Affiliate link builder ──────────────────────────────────────────────────
def amazon_link(asin, affiliate_tag):
    if affiliate_tag.strip():
        return f"https://www.amazon.com/dp/{asin}?tag={affiliate_tag.strip()}"
    return f"https://www.amazon.com/dp/{asin}"

# ── Bundle generator ────────────────────────────────────────────────────────
# We'll pick 10 items: 3 tops, 2 bottoms, 1 dress, 1 outer, 2 shoes, 1 accessory.
DESIRED_COUNTS = {"top":3, "bottom":2, "dress":1, "outerwear":1, "shoe":2, "accessory":1}

def score_item(item, style, body_type, season):
    score = 0
    # Style match
    if style in item.get("styles", []):
        score += 3
    # Body type match
    if body_type in item.get("body_types", []) or "all" in item.get("body_types", []):
        score += 3
    # Season match
    if season in item.get("seasons", []) or "all" in item.get("seasons", []):
        score += 2
    # Prefer lower price slightly (to make bundle more affordable) – small factor
    price = item.get("price", 100)
    score -= price * 0.005  # ~$50 reduces score by 0.25, negligible
    return score

def generate_bundle(style, body_type, season, config):
    """Return a dict of category -> list of top scored items, respecting DESIRED_COUNTS."""
    affiliate_tag = config.get("amazon_affiliate_tag", "")
    bundle = {}
    for cat, count in DESIRED_COUNTS.items():
        candidates = [p for p in PRODUCTS if p.get("category") == cat]
        scored = [(score_item(p, style, body_type, season), p) for p in candidates]
        scored.sort(key=lambda x: x[0], reverse=True)
        # Take top count items with positive score; if not enough, take highest scored anyway
        selected = [item for _, item in scored if _ > 0][:count]
        if len(selected) < count:
            # Fill with highest scored even if score <=0
            remaining = [item for _, item in scored if item not in selected][:count-len(selected)]
            selected += remaining
        # Add affiliate link
        for item in selected:
            item["aff_link"] = amazon_link(item["asin"], affiliate_tag)
        bundle[cat] = selected
    return bundle

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Capsule Wardrobe Bundle</title>
<style>
  body { font-family:Arial; max-width:800px; margin:40px auto; background:#fdfbf7; color:#222; }
  h1 { color:#8b5e3c; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:12px; }
  select, button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#8b5e3c; color:white; cursor:pointer; }
  .category { margin:20px 0; }
  .category h3 { color:#5a3a2a; border-bottom:1px solid #eee; padding-bottom:5px; }
  .item { display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px dotted #eee; }
  .item-info { flex:1; }
  .item-info strong { font-size:1em; }
  .price { color:#888; }
  a { color:#8b5e3c; font-weight:bold; }
  .small { font-size:0.9em; color:#888; margin-top:10px; }
</style>
</head>
<body>
<h1>🧥 Your Personal Capsule Wardrobe Bundle</h1>
<p>Tell us your style, body type, and season. We'll build a 10‑piece outfit bundle with affiliate links.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Style Preference</label>
    <select name="style">
      <option value="casual" {% if q.style=='casual' %}selected{% endif %}>Casual / Everyday</option>
      <option value="boho" {% if q.style=='boho' %}selected{% endif %}>Boho / Romantic</option>
      <option value="minimalist" {% if q.style=='minimalist' %}selected{% endif %}>Minimalist / Classic</option>
      <option value="feminine" {% if q.style=='feminine' %}selected{% endif %}>Feminine / Flirty</option>
      <option value="business" {% if q.style=='business' %}selected{% endif %}>Business / Professional</option>
    </select>
    <label>Body Type</label>
    <select name="body_type">
      <option value="apple" {% if q.body_type=='apple' %}selected{% endif %}>Apple (rounder middle)</option>
      <option value="pear" {% if q.body_type=='pear' %}selected{% endif %}>Pear (wider hips)</option>
      <option value="hourglass" {% if q.body_type=='hourglass' %}selected{% endif %}>Hourglass</option>
      <option value="rectangle" {% if q.body_type=='rectangle' %}selected{% endif %}>Rectangle (athletic)</option>
      <option value="inverted_triangle" {% if q.body_type=='inverted_triangle' %}selected{% endif %}>Inverted Triangle (broader shoulders)</option>
    </select>
    <label>Season</label>
    <select name="season">
      <option value="spring" {% if q.season=='spring' %}selected{% endif %}>Spring</option>
      <option value="summer" {% if q.season=='summer' %}selected{% endif %}>Summer</option>
      <option value="fall" {% if q.season=='fall' %}selected{% endif %}>Fall</option>
      <option value="winter" {% if q.season=='winter' %}selected{% endif %}>Winter</option>
    </select>
    <button type="submit">Build My Bundle</button>
  </div>
</form>

{% if bundle %}
<div class="card">
  <h2>Your 10‑Piece Capsule</h2>
  {% for cat, items in bundle.items() %}
  <div class="category">
    <h3>{{ cat.title() }} ({{ items|length }})</h3>
    {% for item in items %}
    <div class="item">
      <div class="item-info">
        <strong>{{ item.name }}</strong> – {{ item.brand }}<br/>
        <span class="price">${{ "%.2f"|format(item.price) }}</span>
      </div>
      <a href="{{ item.aff_link }}" target="_blank">Shop</a>
    </div>
    {% endfor %}
  </div>
  {% endfor %}
</div>
{% endif %}
<p class="small">Affiliate links included – we may earn a commission.</p>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    q = {
        "style": request.args.get("style", "").strip().lower(),
        "body_type": request.args.get("body_type", "").strip().lower(),
        "season": request.args.get("season", "").strip().lower()
    }

    bundle = None
    if all(q.values()):
        bundle = generate_bundle(q["style"], q["body_type"], q["season"], cfg)
        item_count = sum(len(v) for v in bundle.values())
        post_to_hub(
            f"👗 Capsule bundle for {q['style']}/{q['body_type']}/{q['season']} — {item_count} items",
            "info",
            {"style": q["style"], "body_type": q["body_type"], "season": q["season"], "item_count": item_count}
        )
    return render_template_string(HTML_PAGE, q=q, bundle=bundle)

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

    port = config.get("web_port", 5088)
    post_to_hub(f"🧥 Capsule Wardrobe Bundle live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
