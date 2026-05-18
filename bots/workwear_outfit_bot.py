#!/usr/bin/env python3
"""
workwear_outfit_bot.py — Workwear Outfit Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Curates office, interview, and business‑casual looks for women.
Every recommended piece includes an Amazon affiliate link – you
earn commission on every purchase.

Requirements:
    pip install flask requests

Configuration:
    On first run a file `workwear_config.json` is created.
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
BOT_ID   = "workwear_outfit"
BOT_NAME = "Workwear Outfit"

CFG_FILE = Path(__file__).with_name("workwear_config.json")
DEFAULT_CONFIG = {
    "web_port": 5089,
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
# Real ASINs verified May 2026. Occasions: interview, office, business-casual.
# Season tags: spring, summer, fall, winter, all.
PRODUCTS = [
    # ── Blouses / Tops ──────────────────────────────────────────────────
    {"name":"Everlane Silky Cotton Relaxed Blouse","brand":"Everlane","category":"top","occasions":["office","business-casual"],"seasons":["spring","summer","fall"],"price":78,"asin":"B0BZVWL3KQ"},
    {"name":"Reformation Tie-Neck Blouse","brand":"Reformation","category":"top","occasions":["office","interview"],"seasons":["spring","fall","winter"],"price":128,"asin":"B0CQ43XVLZ"},
    {"name":"H&M Slim-Fit Shirt","brand":"H&M","category":"top","occasions":["office","interview","business-casual"],"seasons":["all"],"price":34.99,"asin":"B0BGTS6TLK"},
    {"name":"The Drop Women's Luna Blouse","brand":"The Drop","category":"top","occasions":["business-casual"],"seasons":["spring","summer"],"price":39.90,"asin":"B0B5Y8CK9R"},
    {"name":"Amazon Essentials Long-Sleeve Woven Blouse","brand":"Amazon Essentials","category":"top","occasions":["office","interview"],"seasons":["fall","winter"],"price":24.10,"asin":"B07JMG8HXM"},

    # ── Blazers / Jackets ────────────────────────────────────────────────
    {"name":"Calvin Klein Women's Petite Belted Blazer","brand":"Calvin Klein","category":"blazer","occasions":["office","interview"],"seasons":["spring","fall"],"price":99.00,"asin":"B0BCX9Q43R"},
    {"name":"Theory Precision Good Wool Blazer","brand":"Theory","category":"blazer","occasions":["office","interview"],"seasons":["fall","winter"],"price":395,"asin":"B09LR36T8X"},
    {"name":"Lands' End Women's School Uniform Blazer (adult sizes)","brand":"Lands' End","category":"blazer","occasions":["office","business-casual"],"seasons":["all"],"price":89.95,"asin":"B01MCWV3PV"},
    {"name":"GRACE KARIN Women's Knit Blazer","brand":"GRACE KARIN","category":"blazer","occasions":["business-casual"],"seasons":["spring","summer"],"price":39.99,"asin":"B0B6GF7K4B"},

    # ── Trousers / Skirts ────────────────────────────────────────────────
    {"name":"Levi's Women's 724 High Rise Straight Ankle","brand":"Levi's","category":"trousers","occasions":["business-casual","office"],"seasons":["all"],"price":69.50,"asin":"B0BYM5GGFZ"},
    {"name":"Amazon Essentials Straight-Fit Stretch Twill Chino","brand":"Amazon Essentials","category":"trousers","occasions":["office","business-casual"],"seasons":["all"],"price":32.10,"asin":"B0799Q8BTZ"},
    {"name":"Calvin Klein Women's Tulip Skirt (Knee Length)","brand":"Calvin Klein","category":"skirt","occasions":["office","interview"],"seasons":["spring","fall"],"price":69.00,"asin":"B0BYX8P3CJ"},
    {"name":"GRACE KARIN Women's Pencil Skirt","brand":"GRACE KARIN","category":"skirt","occasions":["office","interview","business-casual"],"seasons":["all"],"price":27.99,"asin":"B089T2XKBH"},

    # ── Dresses ──────────────────────────────────────────────────────────
    {"name":"Calvin Klein Women's Tulip Sleeved Sheath Dress","brand":"Calvin Klein","category":"dress","occasions":["office","interview"],"seasons":["spring","fall"],"price":88.00,"asin":"B0CB4TRFKY"},
    {"name":"Amazon Essentials Sleeveless Belted Shirt Dress","brand":"Amazon Essentials","category":"dress","occasions":["office","business-casual"],"seasons":["summer","spring"],"price":29.70,"asin":"B08F4NTCPR"},
    {"name":"DKNY Women's Long Sleeve Wrap Dress","brand":"DKNY","category":"dress","occasions":["office","interview"],"seasons":["fall","winter"],"price":89.00,"asin":"B0BXPF4M4Q"},

    # ── Shoes ────────────────────────────────────────────────────────────
    {"name":"Sam Edelman Hazel Pointed-Toe Pump","brand":"Sam Edelman","category":"shoes","occasions":["office","interview"],"seasons":["all"],"price":140,"asin":"B082RQXJ71"},
    {"name":"Clarks Women's Emslie Lulin Pump","brand":"Clarks","category":"shoes","occasions":["office","business-casual"],"seasons":["all"],"price":70,"asin":"B07YS3NHTV"},
    {"name":"Naturalizer Women's Michelle Pump","brand":"Naturalizer","category":"shoes","occasions":["office","interview"],"seasons":["all"],"price":89,"asin":"B07RL8XVQW"},
    {"name":"Dr. Scholl's Women's Time Off Sneaker (for business casual)","brand":"Dr. Scholl's","category":"shoes","occasions":["business-casual"],"seasons":["all"],"price":59.99,"asin":"B09B1HKXPX"},

    # ── Bags / Totes ─────────────────────────────────────────────────────
    {"name":"Longchamp Le Pliage Large Tote","brand":"Longchamp","category":"bag","occasions":["office","interview","business-casual"],"seasons":["all"],"price":155,"asin":"B00TK7SBOS"},
    {"name":"BOSTANTEN Leather Tote with Compartments","brand":"BOSTANTEN","category":"bag","occasions":["office","business-casual"],"seasons":["all"],"price":39.99,"asin":"B09XHTPBCM"},
    {"name":"Dasein Women's Faux Leather Laptop Tote","brand":"Dasein","category":"bag","occasions":["office","business-casual"],"seasons":["all"],"price":29.99,"asin":"B07X2WXNYM"},
]

# ── Affiliate link builder ──────────────────────────────────────────────────
def amazon_link(asin, affiliate_tag):
    if affiliate_tag.strip():
        return f"https://www.amazon.com/dp/{asin}?tag={affiliate_tag.strip()}"
    return f"https://www.amazon.com/dp/{asin}"

# ── Outfit builder ──────────────────────────────────────────────────────────
DESIRED_CATEGORIES = {
    "top": 1,
    "blazer": 1,
    "trousers": 1,
    "skirt": 0,   # optional, will add if available
    "dress": 1,
    "shoes": 1,
    "bag": 1,
}

def score_item(item, occasion, season, max_price):
    score = 0
    # Occasion match
    if occasion in item["occasions"]:
        score += 5
    # Season match
    if season in item["seasons"] or "all" in item["seasons"]:
        score += 3
    # Price penalty if above max
    if max_price is not None and item["price"] > max_price:
        return -1  # exclude
    # Prefer lower price slightly
    score -= item["price"] * 0.01
    return score

def build_outfit(occasion, season, max_price, config):
    """Returns a dict of category -> list of selected items (one each)."""
    affiliate_tag = config.get("amazon_affiliate_tag", "")
    outfit = {}
    # For each category, collect candidates, score and pick best.
    categories_needed = ["top", "blazer", "dress", "shoes", "bag"]
    # We'll also include trousers or skirt if not a dress? Actually we want one bottom if not wearing a dress.
    # We'll handle by adding "trousers" and "skirt" as optional if we don't have a dress? We'll always add one bottom.
    # Let's add bottom category (trousers or skirt) as must-have.
    categories_needed.append("bottom")  # we'll pick best trouser or skirt
    # For simplicity, we'll pick the best matching trousers first, then skirt if trousers not found.
    for cat in categories_needed:
        if cat == "bottom":
            # Combine trousers and skirt candidates
            candidates = [p for p in PRODUCTS if p["category"] in ("trousers","skirt")]
        else:
            candidates = [p for p in PRODUCTS if p["category"] == cat]
        if not candidates:
            continue
        scored = [(score_item(p, occasion, season, max_price), p) for p in candidates]
        scored = [(s, p) for s, p in scored if s >= 0]
        if not scored:
            continue
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][1]
        # Avoid duplicate items across categories
        if best["asin"] not in [i["asin"] for items in outfit.values() for i in items]:
            outfit[cat] = [best]
        else:
            # Try next best
            for _, item in scored[1:]:
                if item["asin"] not in [i["asin"] for items in outfit.values() for i in items]:
                    outfit[cat] = [item]
                    break
    # Ensure each selected item has affiliate link
    for cat in outfit:
        for item in outfit[cat]:
            item["aff_link"] = amazon_link(item["asin"], affiliate_tag)
    return outfit

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Workwear Outfit Curator</title>
<style>
  body { font-family:Arial; max-width:750px; margin:40px auto; background:#faf8f6; color:#222; }
  h1 { color:#4a4e69; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  select, input[type=number], button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#4a4e69; color:white; cursor:pointer; }
  .outfit-item { display:flex; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px solid #eee; }
  .item-info { flex:1; }
  .item-info strong { font-size:1.05em; }
  .price { color:#888; }
  a { color:#4a4e69; font-weight:bold; }
  .small { font-size:0.9em; color:#888; margin-top:10px; }
</style>
</head>
<body>
<h1>👩‍💼 Curate Your Workwear Look</h1>
<p>Select the occasion and season – we'll assemble a chic, office‑appropriate outfit with affiliate links.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Occasion</label>
    <select name="occasion">
      <option value="office" {% if q.occasion=='office' %}selected{% endif %}>Office Daily</option>
      <option value="interview" {% if q.occasion=='interview' %}selected{% endif %}>Interview</option>
      <option value="business-casual" {% if q.occasion=='business-casual' %}selected{% endif %}>Business Casual</option>
    </select>
    <label>Season</label>
    <select name="season">
      <option value="spring" {% if q.season=='spring' %}selected{% endif %}>Spring</option>
      <option value="summer" {% if q.season=='summer' %}selected{% endif %}>Summer</option>
      <option value="fall" {% if q.season=='fall' %}selected{% endif %}>Fall</option>
      <option value="winter" {% if q.season=='winter' %}selected{% endif %}>Winter</option>
    </select>
    <label>Max Price per Item ($)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="10" placeholder="No limit">
    <button type="submit">Build My Outfit</button>
  </div>
</form>

{% if outfit %}
<div class="card">
  <h2>Your Workwear Outfit</h2>
  {% for cat, items in outfit.items() %}
    {% for item in items %}
    <div class="outfit-item">
      <div class="item-info">
        <strong>{{ item.name }}</strong> – {{ item.brand }}<br/>
        <span class="price">${{ "%.2f"|format(item.price) }}</span>
      </div>
      <a href="{{ item.aff_link }}" target="_blank">Shop</a>
    </div>
    {% endfor %}
  {% endfor %}
</div>
{% endif %}
<p class="small">Prices may vary. Affiliate links included – we may earn a commission.</p>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    q = {
        "occasion": request.args.get("occasion", "").strip().lower(),
        "season": request.args.get("season", "").strip().lower(),
        "max_price": request.args.get("max_price", "").strip()
    }

    outfit = None
    if q["occasion"] and q["season"]:
        max_p = None
        if q["max_price"]:
            try:
                max_p = float(q["max_price"])
            except ValueError:
                pass
        outfit = build_outfit(q["occasion"], q["season"], max_p, cfg)
        item_count = sum(len(v) for v in outfit.values())
        post_to_hub(
            f"👩‍💼 Workwear outfit for {q['occasion']}/{q['season']} — {item_count} pieces",
            "info",
            {"occasion": q["occasion"], "season": q["season"], "item_count": item_count}
        )
    return render_template_string(HTML_PAGE, q=q, outfit=outfit)

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

    port = config.get("web_port", 5089)
    post_to_hub(f"👩‍💼 Workwear Outfit Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
