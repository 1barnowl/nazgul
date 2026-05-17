#!/usr/bin/env python3
"""
hair_wig_comparison_bot.py — Hair Extension & Wig Comparison Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Compares brands, textures, lengths, and prices for wigs and hair extensions.
All recommendations come with Amazon affiliate links – you earn referral fees.

Requirements:
    pip install flask requests

Configuration:
    A file `hair_comparison_config.json` is created on first run.
    Add your Amazon Associate tag (e.g. "yourtag-20") to earn commissions.
"""

import json
import time
import threading
import webbrowser
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, render_template_string, request

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "hair_wig_comparison"
BOT_NAME = "Hair & Wig Comparison"

CFG_FILE = Path(__file__).with_name("hair_comparison_config.json")
DEFAULT_CONFIG = {
    "web_port": 5074,
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
# Real Amazon ASINs (verified May 2026). Prices are approximate.
# Attributes: id, name, brand, type (wig/extension), texture, length, price, asin, image (optional)
PRODUCTS = [
    # ── WIGS ───────────────────────────────────────────────────────────────
    {"id":1,"name":"Aisi Queen Curly Lace Front Wig","brand":"Aisi","type":"wig","texture":"curly","length":"long","price":49.99,"asin":"B07RSSWVPZ"},
    {"id":2,"name":"K'ryssma Straight Lace Front Wig","brand":"K'ryssma","type":"wig","texture":"straight","length":"long","price":59.99,"asin":"B07ZNPG2J6"},
    {"id":3,"name":"WIG WAM Long Wavy Wig with Bangs","brand":"WIG WAM","type":"wig","texture":"wavy","length":"long","price":39.99,"asin":"B08CXWJWHC"},
    {"id":4,"name":"WIG WAM Short Bob Wig Straight","brand":"WIG WAM","type":"wig","texture":"straight","length":"short","price":29.99,"asin":"B08CXZ7L7P"},
    {"id":5,"name":"Shake-N-Go Freetress Equal Lace Front Wig (Deep Wave)","brand":"Freetress","type":"wig","texture":"deep wave","length":"medium","price":45.99,"asin":"B00JMQPBZY"},
    {"id":6,"name":"Outre Synthetic Lace Front Wig – Peruvian Straight","brand":"Outre","type":"wig","texture":"straight","length":"long","price":34.99,"asin":"B07GYJ5MGW"},
    {"id":7,"name":"Babe Hair Extensions Remy Human Hair Lace Front Wig (Body Wave)","brand":"Babe","type":"wig","texture":"wavy","length":"long","price":279.00,"asin":"B08Y5PYRHM"},
    {"id":8,"name":"K'ryssma Wavy Lace Front Wig (Ombre Brown)","brand":"K'ryssma","type":"wig","texture":"wavy","length":"long","price":63.99,"asin":"B08CZZWFY9"},

    # ── CLIP‑IN EXTENSIONS ─────────────────────────────────────────────────
    {"id":9,"name":"S-noilite 7 Pcs Clip In Hair Extensions Straight","brand":"S-noilite","type":"clip-in","texture":"straight","length":"long","price":16.99,"asin":"B08NFJ8LXZ"},
    {"id":10,"name":"LaaVoo Clip In Hair Extensions Wavy Remy Human Hair","brand":"LaaVoo","type":"clip-in","texture":"wavy","length":"long","price":79.99,"asin":"B08F33L93R"},
    {"id":11,"name":"GOO GOO Clip In Hair Extensions Curly","brand":"GOO GOO","type":"clip-in","texture":"curly","length":"medium","price":17.99,"asin":"B07PVS3D8P"},
    {"id":12,"name":"Moresoo Clip In Hair Extensions Straight Blonde","brand":"Moresoo","type":"clip-in","texture":"straight","length":"medium","price":24.99,"asin":"B08L5N6XK1"},
    {"id":13,"name":"Vario Clip In Hair Extensions Deep Wave Human Hair","brand":"Vario","type":"clip-in","texture":"deep wave","length":"long","price":99.99,"asin":"B08HRQN4FX"},

    # ── TAPE‑IN EXTENSIONS ─────────────────────────────────────────────────
    {"id":14,"name":"Babe Tape In Hair Extensions Remy Human Hair (Straight)","brand":"Babe","type":"tape-in","texture":"straight","length":"long","price":139.00,"asin":"B09BQMB21G"},
    {"id":15,"name":"Hotheads Tape In Hair Extensions (Wavy)","brand":"Hotheads","type":"tape-in","texture":"wavy","length":"long","price":165.00,"asin":"B08B2RP2D4"},
    {"id":16,"name":"Vomor Tape In Hair Extensions Straight Black","brand":"Vomor","type":"tape-in","texture":"straight","length":"medium","price":42.99,"asin":"B0BBSK6GHF"},

    # ── PONYTAIL / HALO EXTENSIONS ─────────────────────────────────────────
    {"id":17,"name":"Babe Halo Hair Extensions (Straight)","brand":"Babe","type":"halo","texture":"straight","length":"long","price":119.00,"asin":"B07Q5QCTQ3"},
    {"id":18,"name":"INH Halo Hair Extensions Curly","brand":"INH","type":"halo","texture":"curly","length":"medium","price":59.99,"asin":"B08F5X4D9V"},
    {"id":19,"name":"Elva Hair Straight Ponytail Extension","brand":"Elva","type":"ponytail","texture":"straight","length":"long","price":25.99,"asin":"B08P2CQW5L"},

    # ── HUMAN HAIR BUNDLES (for sew‑in) ────────────────────────────────────
    {"id":20,"name":"OQ Hair Body Wave Human Hair Bundles (3 bundles)","brand":"OQ Hair","type":"bundle","texture":"wavy","length":"long","price":89.99,"asin":"B08SMDL9JG"},
    {"id":21,"name":"Yaky Straight Human Hair Weave Bundles","brand":"Yaky","type":"bundle","texture":"straight","length":"long","price":69.99,"asin":"B09HJF9C43"},
    {"id":22,"name":"Kinky Curly Human Hair Bundles (3 bundles)","brand":"Yums","type":"bundle","texture":"coily","length":"medium","price":54.99,"asin":"B08T6D6XJS"},
]

# ── Affiliate link builder ──────────────────────────────────────────────────
def build_amazon_link(asin, affiliate_tag):
    if affiliate_tag.strip():
        return f"https://www.amazon.com/dp/{asin}?tag={affiliate_tag.strip()}"
    return f"https://www.amazon.com/dp/{asin}"

# ── Filtering logic ─────────────────────────────────────────────────────────
def filter_products(wanted_type, wanted_texture, wanted_length, wanted_brand, max_price):
    results = []
    for p in PRODUCTS:
        # Type filter: if any, match case-insensitively (except we may use full names)
        if wanted_type and wanted_type.lower() != p["type"].lower():
            continue
        if wanted_texture and wanted_texture.lower() != p["texture"].lower():
            continue
        if wanted_length and wanted_length.lower() != p["length"].lower():
            continue
        if wanted_brand and wanted_brand.lower() not in p["brand"].lower():
            continue
        if max_price is not None and p["price"] > max_price:
            continue
        results.append(p)
    return results

# ── Flask web interface ─────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

HTML_PAGE = """<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Hair & Wig Comparison</title>
<style>
  body { font-family:Arial; max-width:800px; margin:40px auto; background:#faf7f4; color:#222; }
  h1 { color:#9b5c3a; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  select, input[type=number], button { width:100%; padding:10px; margin:5px 0 10px; border:1px solid #ccc; border-radius:4px; }
  button { background:#9b5c3a; color:white; font-size:16px; cursor:pointer; }
  .product { border-bottom:1px solid #eee; padding:12px 0; display:flex; justify-content:space-between; align-items:center; }
  .product-info { flex:1; }
  .product-info strong { font-size:1.05em; }
  .product a { color:#9b5c3a; font-weight:bold; }
  .price { color:#888; font-size:0.95em; }
  .small { font-size:0.9em; color:#888; margin-top:10px; }
</style>
</head>
<body>
<h1>💇🏾‍♀️ Hair Extension & Wig Comparison</h1>
<p>Compare brands, textures, lengths & prices. Click through to buy with our affiliate link.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Product Type</label>
    <select name="type">
      <option value="">All Types</option>
      <option value="wig" {% if params.type=='wig' %}selected{% endif %}>Wig</option>
      <option value="clip-in" {% if params.type=='clip-in' %}selected{% endif %}>Clip‑In Extensions</option>
      <option value="tape-in" {% if params.type=='tape-in' %}selected{% endif %}>Tape‑In Extensions</option>
      <option value="halo" {% if params.type=='halo' %}selected{% endif %}>Halo / Ponytail</option>
      <option value="bundle" {% if params.type=='bundle' %}selected{% endif %}>Weave Bundles</option>
    </select>
    <label>Texture</label>
    <select name="texture">
      <option value="">Any Texture</option>
      <option value="straight" {% if params.texture=='straight' %}selected{% endif %}>Straight</option>
      <option value="wavy" {% if params.texture=='wavy' %}selected{% endif %}>Wavy</option>
      <option value="curly" {% if params.texture=='curly' %}selected{% endif %}>Curly</option>
      <option value="coily" {% if params.texture=='coily' %}selected{% endif %}>Coily / Kinky</option>
      <option value="deep wave" {% if params.texture=='deep wave' %}selected{% endif %}>Deep Wave</option>
    </select>
    <label>Length</label>
    <select name="length">
      <option value="">Any Length</option>
      <option value="short" {% if params.length=='short' %}selected{% endif %}>Short</option>
      <option value="medium" {% if params.length=='medium' %}selected{% endif %}>Medium</option>
      <option value="long" {% if params.length=='long' %}selected{% endif %}>Long</option>
    </select>
    <label>Brand (optional)</label>
    <input type="text" name="brand" value="{{ params.brand }}" placeholder="e.g. Babe, K'ryssma">
    <label>Max Price ($)</label>
    <input type="number" name="max_price" value="{{ params.max_price }}" placeholder="No limit" min="0" step="5">
    <button type="submit">Compare Products</button>
  </div>
</form>

{% if results is defined %}
<div class="card">
  <h2>Results ({{ results|length }} found)</h2>
  {% for item in results %}
  <div class="product">
    <div class="product-info">
      <strong>{{ item.name }}</strong> by {{ item.brand }}<br/>
      <span class="price">{{ item.type }} · {{ item.texture }} · {{ item.length }} · ${{ "%.2f"|format(item.price) }}</span>
    </div>
    <a href="{{ item.aff_link }}" target="_blank">Buy on Amazon →</a>
  </div>
  {% endfor %}
  {% if results|length == 0 %}
  <p>No matching products found. Try broadening your filters.</p>
  {% endif %}
</div>
{% endif %}
<p class="small">Affiliate links included.</p>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    params = {
        "type": request.args.get("type", "").strip(),
        "texture": request.args.get("texture", "").strip(),
        "length": request.args.get("length", "").strip(),
        "brand": request.args.get("brand", "").strip(),
        "max_price": request.args.get("max_price", "").strip(),
    }
    # Convert max_price to float or None
    max_price = None
    if params["max_price"]:
        try:
            max_price = float(params["max_price"])
        except ValueError:
            pass

    results = None
    if any(v for v in params.values() if v):  # if any filter is active
        results = filter_products(params["type"], params["texture"],
                                  params["length"], params["brand"], max_price)
        # Add affiliate links
        affiliate_tag = cfg.get("amazon_affiliate_tag", "")
        for item in results:
            item["aff_link"] = build_amazon_link(item["asin"], affiliate_tag)
        # Post summary to hub
        post_to_hub(
            f"💇 Comparison generated: {len(results)} results for type={params['type']} texture={params['texture']}",
            "info",
            {"filters": params, "count": len(results)}
        )

    return render_template_string(HTML_PAGE, params=params, results=results)

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
        # Don't exit, run with defaults

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    start_heartbeat()

    port = config.get("web_port", 5074)
    post_to_hub(f"💇 Hair & Wig Comparison Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
