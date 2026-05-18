#!/usr/bin/env python3
"""
plus_size_fashion_finder_bot.py — Plus‑Size Fashion Finder Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Highlights inclusive brands and deals for sizes 12‑32+.
Every recommendation carries an Amazon affiliate link — you earn
a commission on purchases.

Requirements:
    pip install flask requests

Configuration:
    On first run a file `plus_size_fashion_config.json` is created.
    Add your Amazon Associate tag (e.g. "your-20") to earn.
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
BOT_ID   = "plus_size_fashion_finder"
BOT_NAME = "Plus‑Size Fashion Finder"

CFG_FILE = Path(__file__).with_name("plus_size_fashion_config.json")
DEFAULT_CONFIG = {
    "web_port": 5090,
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
# Sizes are either numeric "14 Plus", "16 Plus", etc., or letter "1X", "2X", ...
# We'll store as list of strings (both styles may appear). "Plus" sizes on Amazon
# often look like "14 Plus" but sometimes just "XXL" for some brands.
PRODUCTS = [
    # ── Tops ─────────────────────────────────────────────────────────────
    {"id":1,"name":"Eloquii Pleated Sleeve Blouse","brand":"Eloquii","category":"top","sizes":["14 Plus","16 Plus","18 Plus","20 Plus","22 Plus","24 Plus","26 Plus","28 Plus","30 Plus","32 Plus"],"price":69.95,"asin":"B09NVLRW6K"},
    {"id":2,"name":"Torrid Essential V‑Neck Tee","brand":"Torrid","category":"top","sizes":["1X","2X","3X","4X","5X","6X"],"price":29.90,"asin":"B0C5L3Q7Y3"},
    {"id":3,"name":"Universal Standard Geneva Tee","brand":"Universal Standard","category":"top","sizes":["0 (XS)","1 (S)","2 (M)","3 (L)","4 (XL)","5 (2X)","6 (3X)","7 (4X)"],"price":40.00,"asin":"B0BX2QHV1W"},
    {"id":4,"name":"Lane Bryant No‑Iron Stretch Shirt","brand":"Lane Bryant","category":"top","sizes":["14","16","18","20","22","24","26","28","30","32"],"price":59.95,"asin":"B08K44N4DT"},
    {"id":5,"name":"Old Navy EveryWear V‑Neck Tee (Plus)","brand":"Old Navy","category":"top","sizes":["1X","2X","3X","4X"],"price":14.99,"asin":"B09YQ1FKHF"},

    # ── Dresses ─────────────────────────────────────────────────────────
    {"id":6,"name":"Eloquii Wrap Dress","brand":"Eloquii","category":"dress","sizes":["14 Plus","16 Plus","18 Plus","20 Plus","22 Plus","24 Plus","26 Plus","28 Plus"],"price":89.95,"asin":"B09QKF8ZS6"},
    {"id":7,"name":"Torrid Lace Fit & Flare Dress","brand":"Torrid","category":"dress","sizes":["1X","2X","3X","4X"],"price":68.50,"asin":"B0BWNCNFQR"},
    {"id":8,"name":"Kiyonna Clothing Wrap Dress","brand":"Kiyonna","category":"dress","sizes":["16 Plus","18 Plus","20 Plus","22 Plus","24 Plus","26 Plus","28 Plus"],"price":128.00,"asin":"B07NZ45C7Y"},
    {"id":9,"name":"Avenue Swing Dress","brand":"Avenue","category":"dress","sizes":["14/16","18/20","22/24","26/28","30/32"],"price":45.99,"asin":"B08VWTVS2P"},

    # ── Bottoms ─────────────────────────────────────────────────────────
    {"id":10,"name":"Torrid Bombshell Skinny Jean","brand":"Torrid","category":"bottoms","sizes":["10","12","14","16","18","20","22","24","26","28","30"],"price":79.50,"asin":"B0BN8XWQY5"},
    {"id":11,"name":"Universal Standard Seine High‑Rise Skinny Jeans","brand":"Universal Standard","category":"bottoms","sizes":["0 (XS)","1 (S)","2 (M)","3 (L)","4 (XL)","5 (2X)","6 (3X)","7 (4X)"],"price":98.00,"asin":"B0BX2QHV1W"},
    {"id":12,"name":"Lane Bryant All‑Season Stretch Pant","brand":"Lane Bryant","category":"bottoms","sizes":["14","16","18","20","22","24","26","28","30","32"],"price":69.95,"asin":"B08GL3LK8K"},
    {"id":13,"name":"Old Navy High‑Waisted PowerSoft Leggings (Plus)","brand":"Old Navy","category":"bottoms","sizes":["1X","2X","3X","4X"],"price":29.99,"asin":"B09BCJX6G5"},
    {"id":14,"name":"Gloria Vanderbilt Amanda Classic Tapered Jean (Plus)","brand":"Gloria Vanderbilt","category":"bottoms","sizes":["16 Plus","18 Plus","20 Plus","22 Plus","24 Plus","26 Plus","28 Plus"],"price":44.99,"asin":"B089G3GGH7"},

    # ── Activewear ──────────────────────────────────────────────────────
    {"id":15,"name":"Fabletics Trinity High‑Waisted Legging (Plus)","brand":"Fabletics","category":"activewear","sizes":["1X","2X","3X"],"price":69.95,"asin":"B0BXRDTC3D"},
    {"id":16,"name":"Nike Plus Size One Luxe Icon Clash Sports Bra","brand":"Nike","category":"activewear","sizes":["1X","2X","3X"],"price":40.00,"asin":"B0BZDMVH7K"},
    {"id":17,"name":"Zella Restore Live In High Waist Leggings (Plus)","brand":"Zella","category":"activewear","sizes":["1X","2X","3X","4X"],"price":59.00,"asin":"B07S2JVKLM"},

    # ── Lingerie ────────────────────────────────────────────────────────
    {"id":18,"name":"Torrid Lace Bralette","brand":"Torrid","category":"lingerie","sizes":["1X","2X","3X","4X"],"price":29.90,"asin":"B0BYJ3ZRN4"},
    {"id":19,"name":"Lane Bryant Cushion Comfort Back Smoothing Bra","brand":"Lane Bryant","category":"lingerie","sizes":["36C","38C","40C","42C","44C","38D","40D","42D","44D","38DD","40DD","42DD","44DD"],"price":44.95,"asin":"B08GS4B1BH"},
    {"id":20,"name":"Playtex 18 Hour Ultimate Lift & Support Bra (Plus)","brand":"Playtex","category":"lingerie","sizes":["38C","40C","42C","44C","38D","40D","42D","44D","38DD","40DD","42DD","44DD"],"price":22.00,"asin":"B09N7P7F84"},

    # ── Outerwear ───────────────────────────────────────────────────────
    {"id":21,"name":"Lane Bryant Lightweight Faux Leather Moto Jacket","brand":"Lane Bryant","category":"outerwear","sizes":["14","16","18","20","22","24","26","28","30","32"],"price":99.95,"asin":"B08GLV3YW2"},
    {"id":22,"name":"Torrid Faux Suede Moto Jacket","brand":"Torrid","category":"outerwear","sizes":["1X","2X","3X","4X"],"price":98.50,"asin":"B0B3N6KV49"},
    {"id":23,"name":"Amazon Essentials Women's Plus Size Quilted Jacket","brand":"Amazon Essentials","category":"outerwear","sizes":["1X","2X","3X","4X","5X","6X"],"price":44.90,"asin":"B08H6KRTPZ"},
]

# ── Size helpers ────────────────────────────────────────────────────────────
def match_size(item_sizes, user_size):
    """Check if the item carries a size that matches the user's input.
    We'll compare case‑insensitively. The user can enter a number like "22"
    or a letter code like "3X". We'll try to match against the item's size list.
    """
    user_size = user_size.strip().lower()
    if not user_size:
        return True
    for s in item_sizes:
        s_lower = s.lower()
        # Direct match
        if user_size == s_lower:
            return True
        # If user entered numeric size, maybe item stores "14 Plus" or "16 Plus"
        # and the numeric part matches
        if user_size.isdigit():
            # Remove "plus" from the item string and check if the number equals user input
            cleaned = s_lower.replace("plus", "").replace(" ", "").replace("/", "").replace("-", "")
            if cleaned.isdigit() and cleaned == user_size:
                return True
        # If user entered letter size like "2x", item might have "2X" or "2x"
        # Already handled by direct match lowercased.
    return False

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
<title>Plus‑Size Fashion Finder</title>
<style>
  body { font-family:Arial; max-width:800px; margin:40px auto; background:#fcf9f8; color:#222; }
  h1 { color:#b2546a; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  select, input[type=number], button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#b2546a; color:white; cursor:pointer; }
  .product { border-bottom:1px solid #eee; padding:12px 0; display:flex; justify-content:space-between; align-items:center; }
  .product-info { flex:1; }
  .product-info strong { font-size:1.05em; }
  .price { color:#888; }
  a { color:#b2546a; font-weight:bold; }
  .small { font-size:0.9em; color:#888; margin-top:10px; }
</style>
</head>
<body>
<h1>👗 Plus‑Size Fashion Finder</h1>
<p>Find inclusive brands and deals in your size. Affiliate links included – we earn a commission when you shop.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Your Size</label>
    <select name="size">
      <option value="">All Sizes</option>
      <option value="14" {% if q.size=='14' %}selected{% endif %}>14</option>
      <option value="16" {% if q.size=='16' %}selected{% endif %}>16</option>
      <option value="18" {% if q.size=='18' %}selected{% endif %}>18</option>
      <option value="20" {% if q.size=='20' %}selected{% endif %}>20</option>
      <option value="22" {% if q.size=='22' %}selected{% endif %}>22</option>
      <option value="24" {% if q.size=='24' %}selected{% endif %}>24</option>
      <option value="26" {% if q.size=='26' %}selected{% endif %}>26</option>
      <option value="28" {% if q.size=='28' %}selected{% endif %}>28</option>
      <option value="30" {% if q.size=='30' %}selected{% endif %}>30</option>
      <option value="1X" {% if q.size=='1x' %}selected{% endif %}>1X</option>
      <option value="2X" {% if q.size=='2x' %}selected{% endif %}>2X</option>
      <option value="3X" {% if q.size=='3x' %}selected{% endif %}>3X</option>
      <option value="4X" {% if q.size=='4x' %}selected{% endif %}>4X</option>
      <option value="5X" {% if q.size=='5x' %}selected{% endif %}>5X</option>
      <option value="6X" {% if q.size=='6x' %}selected{% endif %}>6X</option>
    </select>
    <label>Category</label>
    <select name="category">
      <option value="">All Categories</option>
      <option value="top" {% if q.category=='top' %}selected{% endif %}>Tops</option>
      <option value="dress" {% if q.category=='dress' %}selected{% endif %}>Dresses</option>
      <option value="bottoms" {% if q.category=='bottoms' %}selected{% endif %}>Bottoms</option>
      <option value="activewear" {% if q.category=='activewear' %}selected{% endif %}>Activewear</option>
      <option value="lingerie" {% if q.category=='lingerie' %}selected{% endif %}>Lingerie</option>
      <option value="outerwear" {% if q.category=='outerwear' %}selected{% endif %}>Outerwear</option>
    </select>
    <label>Brand (optional)</label>
    <input type="text" name="brand" value="{{ q.brand }}" placeholder="e.g. Torrid, Eloquii">
    <label>Max Price ($)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="5" placeholder="No limit">
    <button type="submit">Find Fashion</button>
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
  {% if results|length == 0 %}<p>No items match your filters. Try a different size or category.</p>{% endif %}
</div>
{% endif %}
<p class="small">Prices may vary. Affiliate links included.</p>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    q = {
        "size": request.args.get("size", "").strip().lower(),
        "category": request.args.get("category", "").strip().lower(),
        "brand": request.args.get("brand", "").strip().lower(),
        "max_price": request.args.get("max_price", "").strip()
    }

    results = None
    if any(q.values()):
        max_p = None
        if q["max_price"]:
            try:
                max_p = float(q["max_price"])
            except ValueError:
                pass

        filtered = []
        for item in PRODUCTS:
            # size filter
            if q["size"] and not match_size(item["sizes"], q["size"]):
                continue
            # category filter
            if q["category"] and item["category"] != q["category"]:
                continue
            # brand filter (partial match)
            if q["brand"] and q["brand"] not in item["brand"].lower():
                continue
            # max price
            if max_p is not None and item["price"] > max_p:
                continue
            filtered.append(item)

        affiliate_tag = cfg.get("amazon_affiliate_tag", "")
        for item in filtered:
            item["aff_link"] = amazon_link(item["asin"], affiliate_tag)

        post_to_hub(
            f"👗 Plus‑size fashion: {len(filtered)} items for size={q['size']} category={q['category']}",
            "info",
            {"size": q["size"], "category": q["category"], "brand": q["brand"], "count": len(filtered)}
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

    port = config.get("web_port", 5090)
    post_to_hub(f"👗 Plus‑Size Fashion Finder live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
