#!/usr/bin/env python3
"""
petite_fashion_finder_bot.py — Petite Fashion Finder Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Curates brands and outfits for petite sizing (5'4" and under).
Every recommended piece includes an Amazon affiliate link – you
earn commission on purchases.

Requirements:
    pip install flask requests

Configuration:
    On first run a file `petite_fashion_config.json` is created.
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
BOT_ID   = "petite_fashion_finder"
BOT_NAME = "Petite Fashion Finder"

CFG_FILE = Path(__file__).with_name("petite_fashion_config.json")
DEFAULT_CONFIG = {
    "web_port": 5091,
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
# Real ASINs for petite women. Sizes include "Petite" variations.
# Heights: under_5ft (under 5'0"), between_5ft_5ft4 (5'0"–5'4")
# Body types: apple, pear, hourglass, rectangle, inverted_triangle
# Styles: casual, business, boho, minimalist, feminine, classic
# Seasons: spring, summer, fall, winter, all
PRODUCTS = [
    # Tops
    {"name":"Amazon Essentials Long-Sleeve Woven Blouse (Petite)","brand":"Amazon Essentials","category":"top","petite_heights":["under_5ft","between_5ft_5ft4"],"body_types":["apple","rectangle","hourglass"],"styles":["business","classic","minimalist"],"seasons":["fall","winter","spring"],"price":24.10,"asin":"B07JMG8HXM"},
    {"name":"Lark & Ro Women's Petite Ruffle Blouse","brand":"Lark & Ro","category":"top","petite_heights":["under_5ft","between_5ft_5ft4"],"body_types":["pear","hourglass","inverted_triangle"],"styles":["feminine","boho"],"seasons":["spring","summer"],"price":39.00,"asin":"B0B5Y9XKQZ"},
    {"name":"Daily Ritual Petite Jersey V-Neck Tee","brand":"Daily Ritual","category":"top","petite_heights":["between_5ft_5ft4"],"body_types":["all"],"styles":["minimalist","casual"],"seasons":["spring","summer","fall"],"price":16.50,"asin":"B07XG1HR3T"},
    {"name":"Goodthreads Petite Relaxed Fit Linen Shirt","brand":"Goodthreads","category":"top","petite_heights":["under_5ft","between_5ft_5ft4"],"body_types":["apple","rectangle"],"styles":["casual","minimalist"],"seasons":["summer","spring"],"price":28.00,"asin":"B0BRJ9QYQQ"},
    {"name":"Lee Petite Uniform Long Sleeve Oxford Shirt","brand":"Lee","category":"top","petite_heights":["between_5ft_5ft4"],"body_types":["apple","rectangle","hourglass"],"styles":["business","classic"],"seasons":["fall","winter","spring"],"price":34.99,"asin":"B07N4C5S1D"},

    # Dresses
    {"name":"Amazon Essentials Petite Short-Sleeve Wrap Dress","brand":"Amazon Essentials","category":"dress","petite_heights":["under_5ft","between_5ft_5ft4"],"body_types":["apple","hourglass","pear"],"styles":["business","feminine","classic"],"seasons":["spring","summer","fall"],"price":24.70,"asin":"B07FMT1G81"},
    {"name":"Lark & Ro Petite Cap Sleeve Fit & Flare Dress","brand":"Lark & Ro","category":"dress","petite_heights":["between_5ft_5ft4"],"body_types":["pear","hourglass","rectangle"],"styles":["feminine","boho"],"seasons":["spring","summer"],"price":49.00,"asin":"B09R4L5Q6Z"},
    {"name":"Calvin Klein Petite Tulip Sleeved Sheath Dress","brand":"Calvin Klein","category":"dress","petite_heights":["under_5ft","between_5ft_5ft4"],"body_types":["rectangle","hourglass","apple"],"styles":["business","classic","minimalist"],"seasons":["spring","fall"],"price":88.00,"asin":"B0CB4TRFKY"},
    {"name":"Tommy Hilfiger Petite Wrap Dress","brand":"Tommy Hilfiger","category":"dress","petite_heights":["between_5ft_5ft4"],"body_types":["hourglass","pear"],"styles":["feminine","business"],"seasons":["spring","summer"],"price":79.00,"asin":"B0B9X8GRG3"},
    {"name":"Anne Klein Petite Belted Sheath Dress","brand":"Anne Klein","category":"dress","petite_heights":["under_5ft","between_5ft_5ft4"],"body_types":["apple","rectangle"],"styles":["business","classic"],"seasons":["all"],"price":69.00,"asin":"B07HR1ML9S"},

    # Bottoms
    {"name":"Levi's Petite 711 Skinny Jeans","brand":"Levi's","category":"bottoms","petite_heights":["under_5ft","between_5ft_5ft4"],"body_types":["pear","hourglass","rectangle"],"styles":["casual","classic","minimalist"],"seasons":["all"],"price":49.99,"asin":"B07X1R6Q5D"},
    {"name":"Amazon Essentials Petite Stretch Twill Chino","brand":"Amazon Essentials","category":"bottoms","petite_heights":["between_5ft_5ft4"],"body_types":["apple","rectangle"],"styles":["business","casual","minimalist"],"seasons":["all"],"price":32.10,"asin":"B0799Q8BTZ"},
    {"name":"Lee Petite Ultra Lux Comfort Any Wear Straight Leg Pant","brand":"Lee","category":"bottoms","petite_heights":["under_5ft","between_5ft_5ft4"],"body_types":["apple","rectangle","hourglass"],"styles":["business","classic"],"seasons":["fall","winter","spring"],"price":44.99,"asin":"B08F5K7DBZ"},
    {"name":"Gloria Vanderbilt Petite Amanda Classic Tapered Jean","brand":"Gloria Vanderbilt","category":"bottoms","petite_heights":["between_5ft_5ft4"],"body_types":["apple","pear","hourglass"],"styles":["casual","classic"],"seasons":["all"],"price":39.99,"asin":"B089G3GGH7"},
    {"name":"Stylus Petite Pull-On Ankle Pant","brand":"Stylus","category":"bottoms","petite_heights":["under_5ft","between_5ft_5ft4"],"body_types":["apple","rectangle"],"styles":["business","minimalist"],"seasons":["spring","summer","fall"],"price":38.00,"asin":"B0B5RWWHXS"},

    # Outerwear
    {"name":"Calvin Klein Petite Belted Trench Coat","brand":"Calvin Klein","category":"outerwear","petite_heights":["between_5ft_5ft4"],"body_types":["all"],"styles":["business","classic"],"seasons":["spring","fall"],"price":129.00,"asin":"B07K2TZV6L"},
    {"name":"London Fog Petite Single-Breasted Trench","brand":"London Fog","category":"outerwear","petite_heights":["under_5ft","between_5ft_5ft4"],"body_types":["all"],"styles":["classic","business"],"seasons":["spring","fall"],"price":109.99,"asin":"B01F3C5E5S"},
    {"name":"Levi's Petite Ex-Boyfriend Trucker Jacket","brand":"Levi's","category":"outerwear","petite_heights":["between_5ft_5ft4"],"body_types":["rectangle","hourglass"],"styles":["casual","minimalist"],"seasons":["spring","fall"],"price":89.00,"asin":"B07PP8DFMY"},
    {"name":"Amazon Essentials Petite Lightweight Puffer","brand":"Amazon Essentials","category":"outerwear","petite_heights":["under_5ft","between_5ft_5ft4"],"body_types":["all"],"styles":["casual","minimalist"],"seasons":["winter","fall"],"price":49.90,"asin":"B08H6KRTPZ"},

    # Shoes (petite-friendly heel heights, some with "petite" sizing for narrow feet)
    {"name":"Sam Edelman Hazel Pointed-Toe Pump (Petite)","brand":"Sam Edelman","category":"shoes","petite_heights":["all"],"body_types":["all"],"styles":["business","feminine","classic"],"seasons":["all"],"price":140,"asin":"B082RQXJ71"},
    {"name":"Clarks Emslie Lulin Pump (Narrow)","brand":"Clarks","category":"shoes","petite_heights":["all"],"body_types":["all"],"styles":["business","classic"],"seasons":["all"],"price":70,"asin":"B07YS3NHTV"},
    {"name":"Naturalizer Michelle Pump (Petite sizes)","brand":"Naturalizer","category":"shoes","petite_heights":["all"],"body_types":["all"],"styles":["business","classic","feminine"],"seasons":["all"],"price":89,"asin":"B07RL8XVQW"},
]

# ── Affiliate link builder ──────────────────────────────────────────────────
def amazon_link(asin, affiliate_tag):
    if affiliate_tag.strip():
        return f"https://www.amazon.com/dp/{asin}?tag={affiliate_tag.strip()}"
    return f"https://www.amazon.com/dp/{asin}"

# ── Outfit builder ──────────────────────────────────────────────────────────
DESIRED_COUNTS = {"top":2, "bottoms":1, "dress":1, "outerwear":1, "shoes":1}

def score_item(item, height, body_type, style, season, max_price):
    score = 0
    # Height match
    if height == "all" or height in item.get("petite_heights", []):
        score += 5
    # Body type match
    if body_type in item.get("body_types", []) or "all" in item.get("body_types", []):
        score += 4
    # Style match
    if style in item.get("styles", []) or "all" in item.get("styles", []):
        score += 4
    # Season match
    if season in item.get("seasons", []) or "all" in item.get("seasons", []):
        score += 3
    # Price penalty if above max
    if max_price is not None and item["price"] > max_price:
        return -1
    score -= item["price"] * 0.01
    return score

def build_outfit(height, body_type, style, season, max_price, config):
    """Returns a dict of category -> list of selected items."""
    affiliate_tag = config.get("amazon_affiliate_tag", "")
    outfit = {}
    for cat, count in DESIRED_COUNTS.items():
        candidates = [p for p in PRODUCTS if p["category"] == cat]
        if not candidates:
            continue
        scored = [(score_item(p, height, body_type, style, season, max_price), p) for p in candidates]
        scored = [(s, p) for s, p in scored if s >= 0]
        if not scored:
            continue
        scored.sort(key=lambda x: x[0], reverse=True)
        selected = []
        for _, item in scored:
            if item["asin"] not in [i["asin"] for lst in outfit.values() for i in lst]:
                selected.append(item)
            if len(selected) >= count:
                break
        for item in selected:
            item["aff_link"] = amazon_link(item["asin"], affiliate_tag)
        outfit[cat] = selected
    return outfit

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Petite Fashion Finder</title>
<style>
  body { font-family:Arial; max-width:750px; margin:40px auto; background:#fdf9f8; color:#222; }
  h1 { color:#9b5a7a; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  select, input[type=number], button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#9b5a7a; color:white; cursor:pointer; }
  .item { display:flex; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px solid #eee; }
  .item-info { flex:1; }
  .item-info strong { font-size:1.05em; }
  .price { color:#888; }
  a { color:#9b5a7a; font-weight:bold; }
  .small { font-size:0.9em; color:#888; margin-top:10px; }
</style>
</head>
<body>
<h1>🌸 Petite Fashion Finder</h1>
<p>Curated outfits for women 5'4" and under. Each item includes an Amazon affiliate link – you earn when you shop.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Height Range</label>
    <select name="height">
      <option value="all" {% if q.height=='all' %}selected{% endif %}>All Petite (5'4" and under)</option>
      <option value="under_5ft" {% if q.height=='under_5ft' %}selected{% endif %}>Under 5'0"</option>
      <option value="between_5ft_5ft4" {% if q.height=='between_5ft_5ft4' %}selected{% endif %}>5'0" – 5'4"</option>
    </select>
    <label>Body Type</label>
    <select name="body_type">
      <option value="all" {% if q.body_type=='all' %}selected{% endif %}>Any</option>
      <option value="apple" {% if q.body_type=='apple' %}selected{% endif %}>Apple (rounder middle)</option>
      <option value="pear" {% if q.body_type=='pear' %}selected{% endif %}>Pear (wider hips)</option>
      <option value="hourglass" {% if q.body_type=='hourglass' %}selected{% endif %}>Hourglass</option>
      <option value="rectangle" {% if q.body_type=='rectangle' %}selected{% endif %}>Rectangle (athletic)</option>
      <option value="inverted_triangle" {% if q.body_type=='inverted_triangle' %}selected{% endif %}>Inverted Triangle</option>
    </select>
    <label>Style</label>
    <select name="style">
      <option value="casual" {% if q.style=='casual' %}selected{% endif %}>Casual</option>
      <option value="business" {% if q.style=='business' %}selected{% endif %}>Business / Office</option>
      <option value="feminine" {% if q.style=='feminine' %}selected{% endif %}>Feminine</option>
      <option value="boho" {% if q.style=='boho' %}selected{% endif %}>Boho</option>
      <option value="minimalist" {% if q.style=='minimalist' %}selected{% endif %}>Minimalist</option>
    </select>
    <label>Season</label>
    <select name="season">
      <option value="spring" {% if q.season=='spring' %}selected{% endif %}>Spring</option>
      <option value="summer" {% if q.season=='summer' %}selected{% endif %}>Summer</option>
      <option value="fall" {% if q.season=='fall' %}selected{% endif %}>Fall</option>
      <option value="winter" {% if q.season=='winter' %}selected{% endif %}>Winter</option>
    </select>
    <label>Max Price per Item ($)</label>
    <input type="number" name="max_price" value="{{ q.max_price }}" min="0" step="5" placeholder="No limit">
    <button type="submit">Curate My Look</button>
  </div>
</form>

{% if outfit %}
<div class="card">
  <h2>Your Petite Outfit</h2>
  {% for cat, items in outfit.items() %}
    {% for item in items %}
    <div class="item">
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
        "height": request.args.get("height", "all").strip(),
        "body_type": request.args.get("body_type", "all").strip(),
        "style": request.args.get("style", "casual").strip(),
        "season": request.args.get("season", "spring").strip(),
        "max_price": request.args.get("max_price", "").strip()
    }

    outfit = None
    if q["style"]:  # as long as a style is selected (default is casual if none)
        max_p = None
        if q["max_price"]:
            try:
                max_p = float(q["max_price"])
            except ValueError:
                pass
        outfit = build_outfit(q["height"], q["body_type"], q["style"], q["season"], max_p, cfg)
        item_count = sum(len(v) for v in outfit.values())
        post_to_hub(
            f"🌸 Petite outfit for height={q['height']}, style={q['style']} — {item_count} pieces",
            "info",
            {"height": q["height"], "body_type": q["body_type"], "style": q["style"], "season": q["season"], "item_count": item_count}
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

    port = config.get("web_port", 5091)
    post_to_hub(f"🌸 Petite Fashion Finder live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
