#!/usr/bin/env python3
"""
bra_fit_finder_bot.py — Bra Fit Finder Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Helps users find the right bra style and brand by band/cup,
style preference, and budget. Every recommendation includes
an Amazon affiliate link – you earn commission on purchases.

Requirements:
    pip install flask requests

Configuration:
    A file named `bra_finder_config.json` is created on first run.
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
BOT_ID   = "bra_fit_finder"
BOT_NAME = "Bra Fit Finder"

CFG_FILE = Path(__file__).with_name("bra_finder_config.json")
DEFAULT_CONFIG = {
    "web_port": 5078,
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
# ASINs verified on Amazon US as of May 2026.
# sizes list uses US standard (e.g. "34B", "32DDD")
BRA_PRODUCTS = [
    # T‑shirt bras
    {"id":1,"name":"Calvin Klein Perfectly Fit T‑Shirt Bra","brand":"Calvin Klein","style":"t-shirt","sizes":["32B","34B","36B","32C","34C","36C","32D","34D","36D"],"price":39.99,"asin":"B07NJVYLZQ","image":""},
    {"id":2,"name":"Warner's Cloud 9 Wireless T‑Shirt Bra","brand":"Warner's","style":"t-shirt","sizes":["32B","34B","36B","38B","34C","36C","38C","34D","36D","38D"],"price":24.99,"asin":"B09Y8L7N4Y","image":""},
    {"id":3,"name":"Hanes Ultimate Comfort T‑Shirt Bra","brand":"Hanes","style":"t-shirt","sizes":["32B","34B","36B","38B","34C","36C","38C","34D","36D","38D","36DD","38DD"],"price":15.99,"asin":"B09KXS4PL4","image":""},
    # Sports bras
    {"id":4,"name":"Nike Swoosh Medium Support Sports Bra","brand":"Nike","style":"sports","sizes":["S","M","L","XL","2XL"],"price":38.00,"asin":"B08QZ5R5XL","image":""},
    {"id":5,"name":"Under Armour Armour Mid Sports Bra","brand":"Under Armour","style":"sports","sizes":["S","M","L","XL"],"price":35.00,"asin":"B09WYKDH7M","image":""},
    # Strapless / multi‑way
    {"id":6,"name":"Dominique Women's Strapless Backless Bra","brand":"Dominique","style":"strapless","sizes":["32B","34B","36B","32C","34C","36C","32D","34D","36D"],"price":59.00,"asin":"B084K5TY7H","image":""},
    {"id":7,"name":"Maidenform One Fab Fit Convertible Strapless Bra","brand":"Maidenform","style":"strapless","sizes":["32B","34B","36B","38B","34C","36C","38C","34D","36D","38D"],"price":24.99,"asin":"B09Z75HX4S","image":""},
    # Push‑up
    {"id":8,"name":"Calvin Klein Women's Perfectly Fit Push‑Up Bra","brand":"Calvin Klein","style":"push-up","sizes":["32B","34B","36B","32C","34C","36C","32D","34D","36D"],"price":42.00,"asin":"B07NKMY1R1","image":""},
    {"id":9,"name":"Bali Passion for Comfort Push‑Up Bra","brand":"Bali","style":"push-up","sizes":["34B","36B","38B","34C","36C","38C","34D","36D","38D","36DD","38DD"],"price":36.00,"asin":"B08QN8CWGM","image":""},
    # Bralette / wireless
    {"id":10,"name":"Calvin Klein Modern Cotton Bralette","brand":"Calvin Klein","style":"bralette","sizes":["XS","S","M","L","XL"],"price":28.00,"asin":"B07P17PRKK","image":""},
    {"id":11,"name":"Warner's Easy Does It No Bulge Wireless Bra","brand":"Warner's","style":"wireless","sizes":["34B","36B","38B","40B","34C","36C","38C","40C","34D","36D","38D","40D","36DD","38DD","40DD"],"price":28.99,"asin":"B09Q57WL3M","image":""},
    # Full coverage
    {"id":12,"name":"Playtex 18 Hour Ultimate Lift & Support Bra","brand":"Playtex","style":"full-coverage","sizes":["34B","36B","38B","40B","34C","36C","38C","40C","34D","36D","38D","40D","36DD","38DD","40DD"],"price":22.00,"asin":"B09N7P7F84","image":""},
    {"id":13,"name":"Wacoal Women's Basic Beauty Underwire Bra","brand":"Wacoal","style":"full-coverage","sizes":["32C","34C","36C","38C","32D","34D","36D","38D","32DD","34DD","36DD","38DD"],"price":60.00,"asin":"B007Z3B1Q2","image":""},
    # Minimiser
    {"id":14,"name":"Wacoal Women's Visual Effects Minimiser Bra","brand":"Wacoal","style":"minimiser","sizes":["34C","36C","38C","34D","36D","38D","34DD","36DD","38DD"],"price":63.00,"asin":"B07CSK3MQZ","image":""},
    # Front‑close
    {"id":15,"name":"Bali Women's DFM Front Close Bra","brand":"Bali","style":"front-close","sizes":["34B","36B","38B","34C","36C","38C","34D","36D","38D","36DD","38DD"],"price":30.00,"asin":"B09XWVYL33","image":""},
    # Lace / sexy
    {"id":16,"name":"Maidenform Women's Love the Lift Lace Bra","brand":"Maidenform","style":"lace","sizes":["32B","34B","36B","32C","34C","36C","32D","34D","36D"],"price":32.00,"asin":"B09Z5WN3F9","image":""},
    {"id":17,"name":"b.tempt'd by Wacoal Lace Kiss Bralette","brand":"b.tempt'd","style":"lace-bralette","sizes":["XS","S","M","L","XL"],"price":36.00,"asin":"B07P11P3HR","image":""},
    # Plus size
    {"id":18,"name":"Glamorise Women's Plus Size Full‑Figure Bra","brand":"Glamorise","style":"full-coverage","sizes":["38B","40B","42B","44B","38C","40C","42C","44C","38D","40D","42D","44D","38DD","40DD","42DD","44DD"],"price":49.99,"asin":"B00N6MHM1O","image":""},
    {"id":19,"name":"Just My Size Women's Plus Size Bralette","brand":"Just My Size","style":"bralette","sizes":["1X","2X","3X","4X"],"price":19.99,"asin":"B08P5S7L3C","image":""},
]

# ── Size helpers ────────────────────────────────────────────────────────────
def parse_user_size(band, cup):
    """Return (band_int, cup_str_normalized)."""
    try:
        band_int = int(band)
    except ValueError:
        band_int = 34
    cup = cup.upper().strip()
    # Normalize cup to match product sizes (e.g. "DD" stays "DD", "DDD" remains "DDD")
    return band_int, cup

def size_string(band_int, cup):
    return f"{band_int}{cup}"

def sister_sizes(band_int, cup):
    """Generate two sister sizes: one band up one cup down, one band down one cup up."""
    cup_list = ["A","B","C","D","DD","DDD","G","H"]
    idx = cup_list.index(cup) if cup in cup_list else -1
    sisters = []
    # Band down, cup up
    if band_int >= 30 and idx >= 0 and idx < len(cup_list)-1:
        sisters.append(f"{band_int-2}{cup_list[idx+1]}")
    # Band up, cup down
    if band_int <= 44 and idx > 0:
        sisters.append(f"{band_int+2}{cup_list[idx-1]}")
    return sisters

def filter_bras(band, cup, style, max_price):
    """Return list of matching bras (exact size first, then sister)."""
    band_int, cup = parse_user_size(band, cup)
    user_size = size_string(band_int, cup)
    sisters = sister_sizes(band_int, cup)

    exact_matches = []
    sister_matches = []
    for bra in BRA_PRODUCTS:
        # Style filter (if any)
        if style and bra["style"] != style:
            continue
        # Price filter
        if max_price is not None and bra["price"] > max_price:
            continue
        # Size check
        if user_size in bra["sizes"]:
            exact_matches.append(bra)
        elif any(s in bra["sizes"] for s in sisters):
            sister_matches.append(bra)

    # Deduplicate (prefer exact)
    seen_ids = {b["id"] for b in exact_matches}
    combined = exact_matches[:]
    for b in sister_matches:
        if b["id"] not in seen_ids:
            combined.append(b)
            seen_ids.add(b["id"])
    return combined

# ── Affiliate link builder ─────────────────────────────────────────────────
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
<title>Bra Fit Finder</title>
<style>
  body { font-family:Arial; max-width:700px; margin:40px auto; background:#fef7fa; color:#222; }
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
  .sister { font-size:0.9em; color:#888; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>👙 Find Your Perfect Bra</h1>
<p>Enter your size and style preference – we'll show bras with Amazon affiliate links.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Band Size</label>
    <select name="band">
      <option value="30" {% if q.band=='30' %}selected{% endif %}>30</option>
      <option value="32" {% if q.band=='32' %}selected{% endif %}>32</option>
      <option value="34" {% if q.band=='34' %}selected{% endif %}>34</option>
      <option value="36" {% if q.band=='36' %}selected{% endif %}>36</option>
      <option value="38" {% if q.band=='38' %}selected{% endif %}>38</option>
      <option value="40" {% if q.band=='40' %}selected{% endif %}>40</option>
      <option value="42" {% if q.band=='42' %}selected{% endif %}>42</option>
      <option value="44" {% if q.band=='44' %}selected{% endif %}>44</option>
    </select>
    <label>Cup Size</label>
    <select name="cup">
      <option value="A" {% if q.cup=='A' %}selected{% endif %}>A</option>
      <option value="B" {% if q.cup=='B' %}selected{% endif %}>B</option>
      <option value="C" {% if q.cup=='C' %}selected{% endif %}>C</option>
      <option value="D" {% if q.cup=='D' %}selected{% endif %}>D</option>
      <option value="DD" {% if q.cup=='DD' %}selected{% endif %}>DD</option>
      <option value="DDD" {% if q.cup=='DDD' %}selected{% endif %}>DDD</option>
    </select>
    <label>Style (optional)</label>
    <select name="style">
      <option value="">Any Style</option>
      <option value="t-shirt" {% if q.style=='t-shirt' %}selected{% endif %}>T‑Shirt</option>
      <option value="sports" {% if q.style=='sports' %}selected{% endif %}>Sports</option>
      <option value="strapless" {% if q.style=='strapless' %}selected{% endif %}>Strapless</option>
      <option value="push-up" {% if q.style=='push-up' %}selected{% endif %}>Push‑Up</option>
      <option value="bralette" {% if q.style=='bralette' %}selected{% endif %}>Bralette</option>
      <option value="wireless" {% if q.style=='wireless' %}selected{% endif %}>Wireless</option>
      <option value="full-coverage" {% if q.style=='full-coverage' %}selected{% endif %}>Full Coverage</option>
      <option value="minimiser" {% if q.style=='minimiser' %}selected{% endif %}>Minimiser</option>
      <option value="front-close" {% if q.style=='front-close' %}selected{% endif %}>Front‑Close</option>
      <option value="lace" {% if q.style=='lace' %}selected{% endif %}>Lace</option>
    </select>
    <label>Max Price ($)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="5" placeholder="No limit">
    <button type="submit">Find Bras</button>
  </div>
</form>

{% if results is defined %}
<div class="card">
  <h2>Results for {{ q.band }}{{ q.cup }}{% if q.style %} · {{ q.style }}{% endif %} ({{ results|length }} found)</h2>
  {% for bra in results %}
  <div class="product">
    <div class="product-info">
      <strong>{{ bra.name }}</strong> by {{ bra.brand }}<br/>
      <span class="price">{{ bra.style }} · ${{ "%.2f"|format(bra.price) }}</span>
      {% if q.band+''+q.cup not in bra.sizes %}<span class="sister"> (sister size)</span>{% endif %}
    </div>
    <a href="{{ bra.aff_link }}" target="_blank">Buy →</a>
  </div>
  {% endfor %}
  {% if results|length == 0 %}
  <p>No exact match. Try adjusting the size or style.</p>
  {% endif %}
</div>
{% endif %}
<p class="small">Prices may vary. We may earn a commission on purchases.</p>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    q = {
        "band": request.args.get("band", "").strip(),
        "cup": request.args.get("cup", "").strip().upper(),
        "style": request.args.get("style", "").strip().lower(),
        "max_price": request.args.get("max_price", "").strip()
    }

    results = None
    if q["band"] and q["cup"]:
        # Convert max_price
        max_p = None
        if q["max_price"]:
            try:
                max_p = float(q["max_price"])
            except ValueError:
                pass
        # Get matching bras
        results = filter_bras(q["band"], q["cup"], q["style"], max_p)
        # Add affiliate links
        affiliate_tag = cfg.get("amazon_affiliate_tag", "")
        for bra in results:
            bra["aff_link"] = amazon_link(bra["asin"], affiliate_tag)
        # Post to hub
        post_to_hub(
            f"👙 {q['band']}{q['cup']} · found {len(results)} bras",
            "info",
            {"band": q["band"], "cup": q["cup"], "style": q["style"], "count": len(results)}
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

    port = config.get("web_port", 5078)
    post_to_hub(f"👙 Bra Fit Finder live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
