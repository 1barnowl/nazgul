#!/usr/bin/env python3
"""
declutter_resale_bot.py — Decluttering Resale Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Lets the user upload a photo of items to get rid of.  Uses
Google Cloud Vision to identify objects, then searches eBay
sold listings to estimate resale value.  The bot can also
generate Poshmark / Mercari listing drafts and post them
to the BotController hub so you can list for a 10% cut.

Requirements:
    pip install flask requests google-cloud-vision

Google Vision setup:
    1. Create a project in Google Cloud Console.
    2. Enable the Vision API.
    3. Create a service account, download its JSON key file.
    4. Set environment variable:
         export GOOGLE_APPLICATION_CREDENTIALS="/path/to/key.json"

eBay setup:
    Get an eBay App ID from developer.ebay.com (free).
    Put it in the config file.

Configuration:
    On first run a file `declutter_resale_config.json` is created.
    Fill in your eBay App ID and optionally your Poshmark/Mercari
    usernames to generate ready‑to‑post descriptions.
"""

import json
import os
import time
import threading
import webbrowser
from pathlib import Path

import requests
from flask import Flask, render_template_string, request, redirect, url_for

# ── Google Vision ──────────────────────────────────────────────────────────
try:
    from google.cloud import vision
except ImportError:
    vision = None

# ── Hub connection ─────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "declutter_resale"
BOT_NAME = "Decluttering Resale"

CFG_FILE = Path(__file__).with_name("declutter_resale_config.json")
UPLOAD_DIR = Path(__file__).with_name("declutter_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "web_port": 5062,
    "ebay_app_id": "",                # ★ REQUIRED
    "poshmark_username": "",
    "mercari_username": "",
    "cut_percentage": 10,
    "max_items_per_photo": 10
}

# ── Hub helpers ────────────────────────────────────────────────────────────
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

# ── eBay sold price lookup (Finding API) ──────────────────────────────────
def get_sold_prices(query, app_id, max_results=5):
    """Return median sold price (USD) for a given query, or None."""
    if not app_id:
        return None
    url = "https://svcs.ebay.com/services/search/FindingService/v1"
    params = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.13.0",
        "SECURITY-APPNAME": app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "",
        "keywords": query,
        "itemFilter(0).name": "SoldItemsOnly",
        "itemFilter(0).value": "true",
        "itemFilter(1).name": "Currency",
        "itemFilter(1).value": "USD",
        "paginationInput.entriesPerPage": max_results,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        items = data.get("findCompletedItemsResponse", [{}])[0]\
                     .get("searchResult", {}).get("item", [])
        prices = []
        for item in items:
            amount = item.get("sellingStatus", [{}])[0]\
                         .get("currentPrice", [{}])[0].get("__value__")
            if amount:
                prices.append(float(amount))
        if prices:
            prices.sort()
            return round(prices[len(prices)//2], 2)  # median
        return None
    except Exception:
        return None

# ── Flask web app ──────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["cfg"] = {}

INDEX_HTML = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Declutter & Resell</title>
<style>
  body { font-family:Arial; max-width:600px; margin:40px auto; background:#fafafa; color:#222; }
  h1 { color:#5c3b27; }
  .card { background:#fff; padding:25px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  input[type=file] { padding:8px; }
  button { background:#5c3b27; color:white; padding:10px 20px; border:none; border-radius:6px; cursor:pointer; }
  .item { border-bottom:1px solid #eee; padding:8px 0; }
</style>
</head>
<body>
<h1>📸 Declutter & Resell</h1>
<p>Upload a photo of the items you want to get rid of. The bot will identify them,
   estimate their resale value from eBay sold listings, and create a Poshmark/Mercari
   listing draft for a 10% cut.</p>
<form method="POST" action="/upload" enctype="multipart/form-data">
  <input type="file" name="photo" accept="image/*" required><br/><br/>
  <button type="submit">Scan My Stuff</button>
</form>
<div id="results">{results}</div>
</body>
</html>
"""

RESULTS_HTML = """
<div class="card">
  <h2>🛍️ Items Found</h2>
  <p class="small">Resale values are median sold prices on eBay.</p>
  {% for item in items %}
  <div class="item">
    <strong>{{ item.label }}</strong><br/>
    Estimated resale: <strong>${{ item.estimated_price if item.estimated_price else 'N/A' }}</strong><br/>
    <a href="{{ item.listing_url }}">{{ "Poshmark" if 'poshmark' in item.listing_url else "Mercari" }} Draft →</a>
  </div>
  {% endfor %}
  {% if not items %}
  <p>No sellable items detected. Try a clearer photo.</p>
  {% endif %}
</div>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML, results="")

@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("photo")
    if not file:
        return "No file", 400
    # Save temporarily
    filepath = UPLOAD_DIR / f"upload_{int(time.time())}_{file.filename}"
    file.save(filepath)

    # Use Google Vision
    if vision is None:
        return "Google Cloud Vision not installed. See requirements.", 500
    client = vision.ImageAnnotatorClient()
    with open(filepath, "rb") as img_file:
        content = img_file.read()
    image = vision.Image(content=content)
    response = client.label_detection(image=image, max_results=app.config["cfg"].get("max_items_per_photo", 10))
    labels = response.label_annotations

    items = []
    app_id = app.config["cfg"].get("ebay_app_id", "").strip()
    posh_user = app.config["cfg"].get("poshmark_username", "").strip()
    merc_user = app.config["cfg"].get("mercari_username", "").strip()

    for label in labels:
        # Filter out very general labels that aren't items (e.g., "product", "design")
        if label.score < 0.6 or label.description.lower() in {"product", "brand", "design", "fashion", "clothing", "apparel", "shoe"}:
            continue
        query = label.description
        est_price = get_sold_prices(query, app_id) if app_id else None
        # Create draft listing URL (dummy – we just prepare a text)
        listing_text = f"Excellent condition {query}. Fast shipping!"
        # Generate Poshmark listing link (opens web listing creation with pre-filled description)
        posh_url = f"https://poshmark.com/create-listing?title={query.replace(' ', '%20')}&description={listing_text.replace(' ', '%20')}&price={int(est_price) if est_price else ''}"
        if merc_user:
            merc_url = f"https://www.mercari.com/sell/?name={query.replace(' ', '%20')}&description={listing_text.replace(' ', '%20')}&price={int(est_price) if est_price else ''}"
        else:
            merc_url = posh_url
        items.append({
            "label": label.description,
            "score": label.score,
            "estimated_price": est_price,
            "listing_url": posh_url if posh_user else merc_url,
            "raw": {
                "query": query,
                "ebay_sold_median": est_price,
                "poshmark_draft": posh_url,
            }
        })
        post_to_hub(
            f"Detected: {label.description} — est. resale ${est_price if est_price else 'unknown'}",
            "info",
            {"label": label.description, "estimated_price": est_price}
        )

    # Clean up uploaded file
    try:
        os.remove(filepath)
    except Exception:
        pass

    return render_template_string(INDEX_HTML, results=render_template_string(RESULTS_HTML, items=items))

# ── Hub heartbeat ──────────────────────────────────────────────────────────
def start_heartbeat(config):
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
            f"Config created at {CFG_FILE}. Add eBay App ID and restart.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["cfg"] = config
    start_heartbeat(config)

    port = config.get("web_port", 5062)
    post_to_hub(f"📸 Declutter Resale Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
