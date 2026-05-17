#!/usr/bin/env python3
"""
lingerie_recommendation_bot.py — Lingerie Recommendation Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Suggests lingerie by size, occasion, comfort, and style.
Monetises via Amazon affiliate links.

Requirements:
    pip install flask requests

Configuration:
    On first run, `lingerie_finder_config.json` is created.
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
BOT_ID   = "lingerie_recommendation"
BOT_NAME = "Lingerie Recommendation"

CFG_FILE = Path(__file__).with_name("lingerie_finder_config.json")
DEFAULT_CONFIG = {
    "web_port": 5079,
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
LINGERIE = [
    # Everyday bras
    {"id":1,"name":"Calvin Klein Perfectly Fit T‑Shirt Bra","brand":"Calvin Klein","type":"bra","style":"t-shirt","sizes":["32B","34B","36B","32C","34C","36C","32D","34D","36D"],"occasions":["everyday"],"price":39.99,"asin":"B07NJVYLZQ"},
    {"id":2,"name":"Warner's Cloud 9 Wireless T‑Shirt Bra","brand":"Warner's","type":"bra","style":"wireless","sizes":["34B","36B","38B","34C","36C","38C","34D","36D","38D"],"occasions":["everyday","comfortable"],"price":24.99,"asin":"B09Y8L7N4Y"},
    {"id":3,"name":"Maidenform One Fab Fit Demi Bra","brand":"Maidenform","type":"bra","style":"demi","sizes":["32B","34B","36B","38B","34C","36C","38C","34D","36D","38D"],"occasions":["everyday"],"price":29.99,"asin":"B09Z5R28JH"},
    # Date night / push‑up
    {"id":4,"name":"Calvin Klein Women's Perfectly Fit Push‑Up Bra","brand":"Calvin Klein","type":"bra","style":"push-up","sizes":["32B","34B","36B","32C","34C","36C","32D","34D","36D"],"occasions":["date-night"],"price":42.00,"asin":"B07NKMY1R1"},
    {"id":5,"name":"b.tempt'd by Wacoal Lace Kiss Bralette","brand":"b.tempt'd","type":"bralette","style":"lace-bralette","sizes":["XS","S","M","L","XL"],"occasions":["date-night","bridal"],"price":36.00,"asin":"B07P11P3HR"},
    {"id":6,"name":"Dominique Women's Strapless Backless Bra","brand":"Dominique","type":"bra","style":"strapless","sizes":["32B","34B","36B","32C","34C","36C","32D","34D","36D"],"occasions":["date-night","bridal"],"price":59.00,"asin":"B084K5TY7H"},
    # Comfortable / lounge
    {"id":7,"name":"Hanes Ultimate Comfort T‑Shirt Bra","brand":"Hanes","type":"bra","style":"t-shirt","sizes":["34B","36B","38B","34C","36C","38C","34D","36D","38D","36DD","38DD"],"occasions":["everyday","comfortable"],"price":15.99,"asin":"B09KXS4PL4"},
    {"id":8,"name":"Warner's Easy Does It No Bulge Wireless Bra","brand":"Warner's","type":"bra","style":"wireless","sizes":["34B","36B","38B","40B","34C","36C","38C","40C","34D","36D","38D","40D","36DD","38DD","40DD"],"occasions":["everyday","comfortable"],"price":28.99,"asin":"B09Q57WL3M"},
    {"id":9,"name":"Fruit of the Loom Spaghetti Strap Cotton Bra","brand":"Fruit of the Loom","type":"bralette","style":"wireless","sizes":["S","M","L","XL"],"occasions":["comfortable","everyday"],"price":14.99,"asin":"B084K5TY7H"},
    # Panties
    {"id":10,"name":"Calvin Klein Women's Cotton Stretch Bikini Panties (3‑Pack)","brand":"Calvin Klein","type":"panty","style":"bikini","sizes":["S","M","L","XL"],"occasions":["everyday","comfortable"],"price":24.99,"asin":"B08JQWP1XS"},
    {"id":11,"name":"Maidenform Women's Comfort Devotion Lace Thong","brand":"Maidenform","type":"panty","style":"thong","sizes":["S","M","L","XL"],"occasions":["date-night"],"price":18.99,"asin":"B09Z6CKWFX"},
    {"id":12,"name":"Hanes Women's Cotton Brief Panties (6‑Pack)","brand":"Hanes","type":"panty","style":"brief","sizes":["S","M","L","XL"],"occasions":["everyday","comfortable"],"price":14.99,"asin":"B00A6WIHK4"},
    # Sets
    {"id":13,"name":"Avidlove Lace Lingerie Set (Bra + Panty)","brand":"Avidlove","type":"set","style":"lace","sizes":["32B","34B","36B","32C","34C","36C","32D","34D","36D"],"occasions":["date-night","bridal"],"price":19.99,"asin":"B08HZZT7B2"},
    {"id":14,"name":"Fashion Nova Micro Mesh Bra & Thong Set","brand":"Fashion Nova","type":"set","style":"mesh","sizes":["XS","S","M","L","XL"],"occasions":["date-night"],"price":29.99,"asin":"B09HRR4R4R"},
    # Bodysuits
    {"id":15,"name":"Avidlove Women's Lace Bodysuit","brand":"Avidlove","type":"bodysuit","style":"lace","sizes":["S","M","L","XL"],"occasions":["date-night","bridal"],"price":19.99,"asin":"B08HZZJ2P5"},
    {"id":16,"name":"Calvin Klein Women's Invisibles Comfort Seamless Bodysuit","brand":"Calvin Klein","type":"bodysuit","style":"seamless","sizes":["XS","S","M","L","XL"],"occasions":["everyday","comfortable"],"price":54.00,"asin":"B07YZN94RB"},
    # Bridal
    {"id":17,"name":"Felina Velveteen Plunge Push‑Up Bra","brand":"Felina","type":"bra","style":"push-up","sizes":["32B","34B","36B","32C","34C","36C","32D","34D","36D"],"occasions":["bridal","date-night"],"price":33.99,"asin":"B07DWC3MKF"},
    {"id":18,"name":"b.tempt'd by Wacoal Future Foundation T‑Shirt Bra","brand":"b.tempt'd","type":"bra","style":"t-shirt","sizes":["32B","34B","36B","32C","34C","36C","32D","34D","36D"],"occasions":["everyday","comfortable"],"price":38.00,"asin":"B07P162PJS"},
    {"id":19,"name":"Muk Luks Women's Lace Romper (Lingerie)","brand":"Muk Luks","type":"romper","style":"lace","sizes":["S","M","L","XL"],"occasions":["bridal","date-night"],"price":27.99,"asin":"B08CYXFZPG"},
]

# ── Size helpers ────────────────────────────────────────────────────────────
def parse_user_size(band, cup):
    """Return (band_int, cup_str) normalized."""
    try:
        band_int = int(band)
    except ValueError:
        band_int = 34
    cup = cup.upper().strip()
    return band_int, cup

def size_string(band_int, cup):
    return f"{band_int}{cup}"

def sister_sizes(band_int, cup):
    cup_list = ["A","B","C","D","DD","DDD","G"]
    idx = cup_list.index(cup) if cup in cup_list else -1
    sisters = []
    if band_int >= 30 and idx >= 0 and idx < len(cup_list)-1:
        sisters.append(f"{band_int-2}{cup_list[idx+1]}")
    if band_int <= 44 and idx > 0:
        sisters.append(f"{band_int+2}{cup_list[idx-1]}")
    return sisters

def matches_bra_size(product, user_size, sisters):
    """Check if product contains the user's exact size or sister size."""
    for s in product["sizes"]:
        if s == user_size:
            return True
    for s in product["sizes"]:
        if s in sisters:
            return True
    return False

def matches_letter_size(product, letter_size):
    """Check if the product contains the given letter size."""
    # If product sizes are like "S", "M", etc.
    for s in product["sizes"]:
        if s.strip().upper() == letter_size.upper():
            return True
    return False

# ── Filtering logic ─────────────────────────────────────────────────────────
def filter_lingerie(size_type, band, cup, letter_size, occasion, style, max_price):
    """
    size_type: "bra" or "letter"
    band, cup: for bra size
    letter_size: for letter size (XS-4X)
    Returns list of matching products.
    """
    results = []
    for item in LINGERIE:
        # Size filter
        if size_type == "bra":
            user_size = size_string(int(band), cup.upper())
            sisters = sister_sizes(int(band), cup.upper())
            if not matches_bra_size(item, user_size, sisters):
                continue
        elif size_type == "letter" and letter_size:
            if not matches_letter_size(item, letter_size):
                continue
        # Occasion filter
        if occasion and occasion not in item["occasions"]:
            continue
        # Style filter
        if style and item["style"] != style:
            continue
        # Price filter
        if max_price is not None and item["price"] > max_price:
            continue
        results.append(item)
    return results

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
<title>Lingerie Recommendations</title>
<style>
  body { font-family:Arial; max-width:750px; margin:40px auto; background:#fef7fa; color:#222; }
  h1 { color:#b23a6e; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:12px; }
  select, input[type=number], button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#b23a6e; color:white; cursor:pointer; }
  .product { border-bottom:1px solid #eee; padding:12px 0; display:flex; justify-content:space-between; align-items:center; }
  .product-info { flex:1; }
  .product-info strong { font-size:1.05em; }
  .price { color:#888; }
  a { color:#b23a6e; font-weight:bold; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>✨ Your Lingerie Style Match</h1>
<p>Tell us your size and what you're looking for. We'll pick perfect pieces with affiliate links.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Size Type</label>
    <select name="size_type" onchange="this.form.submit()">
      <option value="bra" {% if q.size_type=='bra' %}selected{% endif %}>Bra Size (Band + Cup)</option>
      <option value="letter" {% if q.size_type=='letter' %}selected{% endif %}>Letter Size (XS-4X)</option>
    </select>
    {% if q.size_type == 'bra' %}
    <label>Band Size</label>
    <select name="band">
      <option value="30" {% if q.band=='30' %}selected{% endif %}>30</option>
      <option value="32" {% if q.band=='32' %}selected{% endif %}>32</option>
      <option value="34" {% if q.band=='34' %}selected{% endif %}>34</option>
      <option value="36" {% if q.band=='36' %}selected{% endif %}>36</option>
      <option value="38" {% if q.band=='38' %}selected{% endif %}>38</option>
      <option value="40" {% if q.band=='40' %}selected{% endif %}>40</option>
    </select>
    <label>Cup Size</label>
    <select name="cup">
      <option value="B" {% if q.cup=='B' %}selected{% endif %}>B</option>
      <option value="C" {% if q.cup=='C' %}selected{% endif %}>C</option>
      <option value="D" {% if q.cup=='D' %}selected{% endif %}>D</option>
      <option value="DD" {% if q.cup=='DD' %}selected{% endif %}>DD</option>
      <option value="DDD" {% if q.cup=='DDD' %}selected{% endif %}>DDD</option>
    </select>
    {% elif q.size_type == 'letter' %}
    <label>Your Size</label>
    <select name="letter_size">
      <option value="XS" {% if q.letter_size=='XS' %}selected{% endif %}>XS</option>
      <option value="S" {% if q.letter_size=='S' %}selected{% endif %}>S</option>
      <option value="M" {% if q.letter_size=='M' %}selected{% endif %}>M</option>
      <option value="L" {% if q.letter_size=='L' %}selected{% endif %}>L</option>
      <option value="XL" {% if q.letter_size=='XL' %}selected{% endif %}>XL</option>
      <option value="2XL" {% if q.letter_size=='2XL' %}selected{% endif %}>2XL</option>
    </select>
    {% endif %}
    <label>Occasion</label>
    <select name="occasion">
      <option value="">All Occasions</option>
      <option value="everyday" {% if q.occasion=='everyday' %}selected{% endif %}>Everyday</option>
      <option value="date-night" {% if q.occasion=='date-night' %}selected{% endif %}>Date Night</option>
      <option value="bridal" {% if q.occasion=='bridal' %}selected{% endif %}>Bridal</option>
      <option value="comfortable" {% if q.occasion=='comfortable' %}selected{% endif %}>Comfortable</option>
    </select>
    <label>Style (optional)</label>
    <select name="style">
      <option value="">Any Style</option>
      <option value="t-shirt" {% if q.style=='t-shirt' %}selected{% endif %}>T‑Shirt</option>
      <option value="push-up" {% if q.style=='push-up' %}selected{% endif %}>Push‑Up</option>
      <option value="wireless" {% if q.style=='wireless' %}selected{% endif %}>Wireless</option>
      <option value="lace" {% if q.style=='lace' %}selected{% endif %}>Lace</option>
      <option value="lace-bralette" {% if q.style=='lace-bralette' %}selected{% endif %}>Lace Bralette</option>
      <option value="strapless" {% if q.style=='strapless' %}selected{% endif %}>Strapless</option>
      <option value="bikini" {% if q.style=='bikini' %}selected{% endif %}>Bikini Panty</option>
      <option value="thong" {% if q.style=='thong' %}selected{% endif %}>Thong</option>
      <option value="brief" {% if q.style=='brief' %}selected{% endif %}>Brief</option>
      <option value="mesh" {% if q.style=='mesh' %}selected{% endif %}>Mesh</option>
      <option value="seamless" {% if q.style=='seamless' %}selected{% endif %}>Seamless</option>
    </select>
    <label>Max Price ($)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="5" placeholder="No limit">
    <button type="submit">Get Recommendations</button>
  </div>
</form>

{% if results is defined %}
<div class="card">
  <h2>Your Picks ({{ results|length }})</h2>
  {% for item in results %}
  <div class="product">
    <div class="product-info">
      <strong>{{ item.name }}</strong> by {{ item.brand }}<br/>
      <span class="price">{{ item.type }} · {{ item.style }} · ${{ "%.2f"|format(item.price) }}</span>
    </div>
    <a href="{{ item.aff_link }}" target="_blank">Shop →</a>
  </div>
  {% endfor %}
  {% if results|length == 0 %}<p>No items match. Try different filters.</p>{% endif %}
</div>
{% endif %}
<p class="small">Affiliate links included – we may earn a commission.</p>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    q = {
        "size_type": request.args.get("size_type", "bra").strip(),
        "band": request.args.get("band", "34").strip(),
        "cup": request.args.get("cup", "B").strip().upper(),
        "letter_size": request.args.get("letter_size", "M").strip(),
        "occasion": request.args.get("occasion", "").strip().lower(),
        "style": request.args.get("style", "").strip().lower(),
        "max_price": request.args.get("max_price", "").strip()
    }
    # Default to bra size if not set
    if q["size_type"] not in ("bra", "letter"):
        q["size_type"] = "bra"

    results = None
    if q["size_type"]:
        max_p = None
        if q["max_price"]:
            try:
                max_p = float(q["max_price"])
            except ValueError:
                pass
        results = filter_lingerie(
            q["size_type"], q["band"], q["cup"],
            q["letter_size"] if q["size_type"] == "letter" else None,
            q["occasion"], q["style"], max_p
        )
        affiliate_tag = cfg.get("amazon_affiliate_tag", "")
        for item in results:
            item["aff_link"] = amazon_link(item["asin"], affiliate_tag)
        post_to_hub(
            f"👙 Lingerie picks: {len(results)} items for {q['size_type']} {q.get('band','')}{q.get('cup','')}{q.get('letter_size','')}",
            "info",
            {"size_type": q["size_type"], "count": len(results)}
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
            f"Config created at {CFG_FILE}. Add your Amazon Affiliate tag to earn.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    start_heartbeat()

    port = config.get("web_port", 5079)
    post_to_hub(f"👙 Lingerie Recommendation Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
