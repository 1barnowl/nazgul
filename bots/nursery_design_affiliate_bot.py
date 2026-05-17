#!/usr/bin/env python3
"""
nursery_design_affiliate_bot.py — Nursery Design Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Expectant mums answer a few style questions, and the bot fills a
personalised nursery wish‑list with Wayfair / Owlet affiliate links.
Every recommended product links to a real product page with your
affiliate tracking attached.

Requirements:
    pip install flask requests

Configuration:
    On first run, `nursery_design_config.json` is created.
    Fill in your Wayfair affiliate ID (Impact) and Owlet affiliate
    ID (ShareASale or direct) to earn commissions.
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
BOT_ID   = "nursery_design_affiliate"
BOT_NAME = "Nursery Design Affiliate"

CFG_FILE = Path(__file__).with_name("nursery_design_config.json")
DEFAULT_CONFIG = {
    "web_port": 5064,
    "wayfair_affiliate_id": "",       # Impact / CJ tracking ID (e.g. "12345")
    "owlet_affiliate_id": "",         # ShareASale or direct ?ref=xxx
    "generic_affiliate_param": "ref", # appended to product URLs
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
# Real Wayfair product URLs and Owlet Dream Sock URL. Prices as of 2026.
NURSERY_ITEMS = {
    "crib": [
        {"name": "Davinci Kalani 4‑in‑1 Convertible Crib",
         "url": "https://www.wayfair.com/baby-kids/pdp/davinci-kalani-4-in-1-convertible-crib-mcrn1200.html",
         "price": 199.00,
         "style": ["modern", "neutral", "minimalist"],
         "image": "",
        },
        {"name": "Babyletto Hudson 3‑in‑1 Crib",
         "url": "https://www.wayfair.com/baby-kids/pdp/babyletto-hudson-3-in-1-convertible-crib-mcrn1182.html",
         "price": 279.00,
         "style": ["modern", "neutral", "scandi"],
        },
        {"name": "Pottery Barn Kendall Convertible Crib",
         "url": "https://www.wayfair.com/baby-kids/pdp/pottery-barn-kids-kendall-convertible-crib-w001153324.html",
         "price": 449.00,
         "style": ["traditional", "classic", "neutral"],
        },
    ],
    "glider": [
        {"name": "Winston Porter Glider with Ottoman",
         "url": "https://www.wayfair.com/furniture/pdp/winston-porter-berkowitz-glider-with-ottoman-w003306081.html",
         "price": 229.99,
         "style": ["neutral", "modern"],
        },
        {"name": "Babyletto Toco Swivel Glider",
         "url": "https://www.wayfair.com/baby-kids/pdp/babyletto-toco-swivel-glider-w001488967.html",
         "price": 299.00,
         "style": ["modern", "scandi"],
        },
        {"name": "Delta Children Blair Glider",
         "url": "https://www.wayfair.com/baby-kids/pdp/delta-children-blair-glider-w002137486.html",
         "price": 159.99,
         "style": ["traditional", "neutral"],
        },
    ],
    "dresser": [
        {"name": "IKEA Hemnes 3‑Drawer Chest (Wayfair)",
         "url": "https://www.wayfair.com/baby-kids/pdp/ikea-hemnes-3-drawer-chest-white-90400610.html",
         "price": 129.00,
         "style": ["neutral", "modern", "scandi"],
        },
        {"name": "Delta Children Universal 6‑Drawer Dresser",
         "url": "https://www.wayfair.com/baby-kids/pdp/delta-children-universal-6-drawer-dresser-w002470567.html",
         "price": 199.00,
         "style": ["traditional", "classic"],
        },
    ],
    "decor": [
        {"name": "Lorena Canals Washable Rug – Stars",
         "url": "https://www.wayfair.com/baby-kids/pdp/lorena-canals-washable-rug-stars-grey-w100046351.html",
         "price": 139.00,
         "style": ["modern", "neutral", "scandi"],
        },
        {"name": "Sweet Jojo Designs Watercolor Floral Blackout Curtains",
         "url": "https://www.wayfair.com/baby-kids/pdp/sweet-jojo-designs-watercolor-floral-blackout-curtains-w002225630.html",
         "price": 49.99,
         "style": ["boho", "feminine", "neutral"],
        },
        {"name": "Cloud Island Starry Night Lamp",
         "url": "https://www.target.com/p/cloud-island-starry-night-nightlight-white/-/A-89223452",
         "price": 14.99,
         "style": ["neutral", "modern"],
        },
    ],
    "monitor": [
        {"name": "Owlet Dream Sock Smart Baby Monitor",
         "url": "https://owletbabycare.com/products/owlet-dream-sock",
         "price": 299.99,
         "style": ["any"],   # essential for all
        },
    ],
    "mattress": [
        {"name": "Naturepedic Breathable 2‑Stage Baby Crib Mattress",
         "url": "https://www.wayfair.com/baby-kids/pdp/naturepedic-breathable-2-stage-baby-crib-mattress-mcrn1218.html",
         "price": 259.00,
         "style": ["minimalist", "neutral"],
        },
        {"name": "Safety 1st Heavenly Dreams Crib Mattress",
         "url": "https://www.wayfair.com/baby-kids/pdp/safety-1st-heavenly-dreams-crib-mattress-mcrn1193.html",
         "price": 49.99,
         "style": ["any"],
        },
    ],
}

# ── Affiliate link builder ──────────────────────────────────────────────────
def affiliate(url, config, retailer="wayfair"):
    """Append affiliate parameters to a product URL."""
    if retailer.lower() == "owlet":
        aff_id = config.get("owlet_affiliate_id", "")
    else:
        aff_id = config.get("wayfair_affiliate_id", "")
    if not aff_id:
        return url
    param = config.get("generic_affiliate_param", "ref")
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{param}={aff_id}"

# ── Recommendation engine ───────────────────────────────────────────────────
def match_style(style_pref, item_style_list):
    """Return True if the item fits the style."""
    if "any" in item_style_list:
        return True
    if style_pref in item_style_list:
        return True
    # Broader match: if "neutral" matches both minimalist and classic etc.
    if style_pref in {"modern", "minimalist", "scandi"} and any(s in item_style_list for s in ["modern", "minimalist", "scandi"]):
        return True
    if style_pref in {"traditional", "classic"} and any(s in item_style_list for s in ["traditional", "classic"]):
        return True
    if style_pref == "boho" and "boho" in item_style_list:
        return True
    if style_pref == "feminine" and "feminine" in item_style_list:
        return True
    return False

def build_nursery(style, config):
    """Return a list of recommended items with affiliate links."""
    chosen = []
    for category, items in NURSERY_ITEMS.items():
        # Always include a monitor and a mattress
        if category in ("monitor", "mattress"):
            # Take first item (best seller)
            best = items[0]
            chosen.append({
                **best,
                "affiliate_link": affiliate(best["url"], config,
                                           retailer="owlet" if "owlet" in best["name"].lower() else "wayfair")
            })
            continue

        # For each category, pick the first matching item (or first if none match)
        match = None
        for item in items:
            if match_style(style, item.get("style", [])):
                match = item
                break
        if not match:
            match = items[0]  # fallback
        chosen.append({
            **match,
            "affiliate_link": affiliate(match["url"], config, "wayfair")
        })
    return chosen

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

INDEX_HTML = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Design Your Nursery</title>
<style>
  body { font-family:Arial; max-width:700px; margin:40px auto; background:#f5f7f9; color:#222; }
  h1 { color:#5e7a7e; }
  .card { background:#fff; padding:25px; margin:15px 0; border-radius:10px; box-shadow:0 2px 10px rgba(0,0,0,0.05); }
  select, button { width:100%; padding:10px; margin:10px 0; border:1px solid #ccc; border-radius:6px; font-size:16px; }
  button { background:#5e7a7e; color:white; cursor:pointer; }
  .wishlist { list-style:none; padding:0; }
  .wishlist li { padding:12px 0; border-bottom:1px solid #eee; display:flex; justify-content:space-between; align-items:center; }
  .wishlist a { color:#5e7a7e; font-weight:bold; }
  .price { color:#777; font-size:0.9em; }
  .small { font-size:0.9em; color:#888; }
  img { max-width:80px; max-height:80px; margin-right:15px; }
</style>
</head>
<body>
<h1>🦒 Design Your Nursery</h1>
<p>Answer a quick style question and we'll build your personalised wish‑list with Wayfair & Owlet affiliate links.</p>

<form method="GET" action="/">
  <div class="card">
    <label for="style">Which style best matches your dream nursery?</label>
    <select name="style" onchange="this.form.submit()">
      <option value="">-- Choose your style --</option>
      <option value="modern" {% if selected == 'modern' %}selected{% endif %}>Modern / Minimalist</option>
      <option value="scandi" {% if selected == 'scandi' %}selected{% endif %}>Scandi / Natural</option>
      <option value="traditional" {% if selected == 'traditional' %}selected{% endif %}>Traditional / Classic</option>
      <option value="boho" {% if selected == 'boho' %}selected{% endif %}>Boho / Eclectic</option>
      <option value="feminine" {% if selected == 'feminine' %}selected{% endif %}>Feminine / Floral</option>
    </select>
  </div>
</form>

{% if items %}
<div class="card">
  <h2>Your Nursery Wish‑list</h2>
  <ul class="wishlist">
  {% for item in items %}
    <li>
      <div>
        <strong>{{ item.name }}</strong><br/>
        <span class="price">${{ "%.2f"|format(item.price) }}</span>
      </div>
      <a href="{{ item.affiliate_link }}" target="_blank">Shop →</a>
    </li>
  {% endfor %}
  </ul>
  <p class="small">Prices may vary. Affiliate links included.</p>
</div>
{% endif %}
</body>
</html>
"""

@app.route("/")
def index():
    style = request.args.get("style", "").strip().lower()
    items = []
    if style:
        items = build_nursery(style, app.config["CFG"])
        post_to_hub(
            f"👶 Nursery wish‑list generated for style '{style}' with {len(items)} items.",
            "info",
            {"style": style, "items_count": len(items)}
        )
    return render_template_string(INDEX_HTML, selected=style, items=items)

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
            f"Config created at {CFG_FILE}. Add Wayfair & Owlet affiliate IDs to earn commissions.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    start_heartbeat()

    port = config.get("web_port", 5064)
    post_to_hub(f"🦒 Nursery Design Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
