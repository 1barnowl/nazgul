#!/usr/bin/env python3
"""
womens_fashion_resale_bot.py — Women’s Fashion Resale Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tracks resale listings for designer handbags, shoes, and clothing
via eBay. Posts new finds to the BotController hub with affiliate
links. Premium email alerts available.

Requirements:
    pip install flask requests

Configuration:
    On first run a file `fashion_resale_config.json` is created.
    Fill in your eBay App ID and eBay Partner Network (EPN) campaign ID
    to earn commissions.
"""

import json
import time
import threading
import webbrowser
from pathlib import Path
from datetime import datetime, timedelta

import requests
from flask import Flask, render_template_string, request, redirect, url_for

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "womens_fashion_resale"
BOT_NAME = "Women’s Fashion Resale"

CFG_FILE   = Path(__file__).with_name("fashion_resale_config.json")
STATE_FILE = Path(__file__).with_name("fashion_resale_state.json")
ALERTS_FILE = Path(__file__).with_name("fashion_resale_alerts.json")

DEFAULT_CONFIG = {
    "web_port": 5087,
    "ebay_app_id": "",                    # ★ REQUIRED
    "epn_campaign_id": "",               # eBay Partner Network campaign ID (optional)
    "premium_email_alerts": False,       # charge for these
    "smtp": {
        "enabled": False,
        "server": "smtp.gmail.com",
        "port": 587,
        "username": "",
        "password": "",
        "from_email": "",
        "premium_recipients": []
    },
    "designer_brands": [                 # brands to monitor (case-insensitive)
        "Chanel",
        "Louis Vuitton",
        "Gucci",
        "Hermès",
        "Prada",
        "Dior",
        "Fendi",
        "Valentino",
        "Burberry",
        "Saint Laurent"
    ],
    "categories": [                      # eBay category IDs for clothing, shoes, handbags
        "63852",   # Women's Handbags & Bags
        "63889",   # Women's Shoes
        "63861",   # Women's Clothing
    ],
    "condition": "pre-owned",            # new, pre-owned, or both? We'll use used only
    "max_price": 1000,
    "scan_interval_minutes": 60
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

# ── Data helpers ────────────────────────────────────────────────────────────
def load_json(filepath, default=None):
    if filepath.exists():
        with open(filepath, "r") as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(filepath, data):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

# ── eBay Finding API (free) ────────────────────────────────────────────────
EBAY_API = "https://svcs.ebay.com/services/search/FindingService/v1"

def search_ebay_brand(app_id, brand, categories, max_price, condition="pre-owned", page=1):
    """
    Returns list of items: { itemId, title, price, buyItNowPrice,
    listingType, url, galleryURL }
    """
    if not app_id:
        return []
    headers = {"X-EBAY-SOA-REQUEST-DATA-FORMAT": "JSON"}
    # category IDs as comma-separated
    cat_str = ",".join(categories)
    filters = [
        {"name": "MaxPrice", "value": str(max_price), "paramName": "Currency", "paramValue": "USD"},
        {"name": "Condition", "value": "Used" if condition == "pre-owned" else "New"},
    ]
    payload = {
        "OPERATION-NAME": "findItemsAdvanced",
        "SERVICE-VERSION": "1.13.0",
        "SECURITY-APPNAME": app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "keywords": brand,
        "categoryId": cat_str,
        "itemFilter": filters,
        "paginationInput.entriesPerPage": 50,
        "paginationInput.pageNumber": page,
        "sortOrder": "StartTimeNewest"
    }
    try:
        r = requests.get(EBAY_API, params=payload, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("findItemsAdvancedResponse", [{}])[0]\
                     .get("searchResult", {}).get("item", [])
        results = []
        for item in items:
            listing_type = item.get("listingInfo", [{}])[0].get("listingType", "")
            # Skip auctions? We'll keep everything.
            price_info = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
            price = float(price_info.get("__value__", 0))
            # Buy It Now price (if available)
            buyitnow = item.get("listingInfo", [{}])[0].get("buyItNowPrice", [{}])[0].get("__value__")
            buyitnow = float(buyitnow) if buyitnow else None

            results.append({
                "itemId": item["itemId"],
                "title": item.get("title", ""),
                "price": price,
                "buyItNowPrice": buyitnow,
                "listingType": listing_type,
                "url": item.get("viewItemURL", f"https://www.ebay.com/itm/{item['itemId']}"),
                "galleryURL": item.get("galleryURL", "")
            })
        return results
    except Exception as e:
        post_to_hub(f"eBay search error for {brand}: {e}", "error")
        return []

def build_affiliate_link(ebay_url, campaign_id):
    """Append EPN campaign ID if available."""
    if campaign_id.strip():
        sep = "&" if "?" in ebay_url else "?"
        return f"{ebay_url}{sep}campid={campaign_id.strip()}"
    return ebay_url

# ── Alert storage ───────────────────────────────────────────────────────────
def save_alert(alert):
    alerts = load_json(ALERTS_FILE, [])
    alerts.append(alert)
    save_json(ALERTS_FILE, alerts)

# ── Scanner thread ──────────────────────────────────────────────────────────
def scan(config, state):
    app_id = config.get("ebay_app_id", "").strip()
    if not app_id:
        post_to_hub("eBay App ID missing – cannot scan.", "error")
        return

    brands = config.get("designer_brands", [])
    categories = config.get("categories", [])
    max_price = config.get("max_price", 1000)
    condition = config.get("condition", "pre-owned")
    campaign_id = config.get("epn_campaign_id", "")

    seen_ids = state.get("seen_item_ids", [])
    new_alerts = []

    for brand in brands:
        items = search_ebay_brand(app_id, brand, categories, max_price, condition)
        for item in items:
            if item["itemId"] in seen_ids:
                continue
            seen_ids.append(item["itemId"])
            aff_link = build_affiliate_link(item["url"], campaign_id)
            item["affiliate_link"] = aff_link
            item["brand"] = brand
            item["found_at"] = datetime.utcnow().isoformat()
            new_alerts.append(item)
        time.sleep(1)  # rate limit

    if new_alerts:
        # Post to Hub and save
        for alert in new_alerts:
            summary = f"👜 New {alert['brand']} listing: {alert['title'][:80]} - ${alert['price']:.0f}"
            post_to_hub(summary, "info", alert)
            save_alert(alert)

        # Premium email alert (if enabled)
        if config.get("premium_email_alerts") and config.get("smtp", {}).get("enabled"):
            send_premium_alerts(new_alerts, config)

    state["seen_item_ids"] = seen_ids
    save_json(STATE_FILE, state)

def send_premium_alerts(items, config):
    smtp_cfg = config["smtp"]
    recipients = smtp_cfg.get("premium_recipients", [])
    if not recipients:
        return
    import smtplib
    from email.mime.text import MIMEText

    body = "New designer resale finds:\n\n"
    for item in items:
        body += f"{item['brand']}: {item['title']} - ${item['price']:.2f}\n{item['affiliate_link']}\n\n"

    msg = MIMEText(body)
    msg["Subject"] = f"👜 {len(items)} New Designer Resale Alerts"
    msg["From"] = smtp_cfg["from_email"]
    msg["To"] = ", ".join(recipients)
    try:
        with smtplib.SMTP(smtp_cfg["server"], smtp_cfg["port"], timeout=10) as server:
            server.starttls()
            server.login(smtp_cfg["username"], smtp_cfg["password"])
            server.send_message(msg)
    except Exception as e:
        post_to_hub(f"Premium email error: {e}", "error")

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

HTML_MAIN = """<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Women’s Fashion Resale</title>
<style>
  body { font-family:Arial; max-width:900px; margin:40px auto; background:#fcf9f7; color:#222; }
  h1 { color:#9b4e5e; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, button { width:100%; padding:10px; margin:5px 0 12px; border:1px solid #ccc; border-radius:4px; }
  button { background:#9b4e5e; color:white; cursor:pointer; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:10px; border-bottom:1px solid #eee; text-align:left; }
  a { color:#9b4e5e; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>👜 Women’s Fashion Resale Alerts</h1>
<p>Real-time designer finds from eBay, with affiliate links. Premium email alerts available.</p>

<div class="card">
  <h2>Recent Alerts (last 200)</h2>
  <table>
    <tr><th>Brand</th><th>Title</th><th>Price</th><th>Link</th></tr>
    {% for a in alerts[-200:]|reverse %}
    <tr>
      <td>{{ a.brand }}</td>
      <td>{{ a.title[:60] }}</td>
      <td>${{ "%.2f"|format(a.price) }}</td>
      <td><a href="{{ a.affiliate_link }}" target="_blank">View</a></td>
    </tr>
    {% endfor %}
  </table>
  {% if alerts|length == 0 %}<p>No alerts yet. Waiting for scan…</p>{% endif %}
</div>

<div class="card">
  <h2>Add a Brand to Track</h2>
  <form method="POST" action="/add_brand">
    <input type="text" name="brand" placeholder="e.g. Chanel" required>
    <button type="submit">Add Brand</button>
  </form>
  <p class="small">Current brands: {{ config.designer_brands|join(', ') }}</p>
</div>

<p class="small"><a href="/premium">Get premium email alerts</a></p>
</body>
</html>"""

PREMIUM_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Premium Alerts</title>
<style>
  body { font-family:Arial; max-width:600px; margin:40px auto; background:#fcf9f7; }
  h2 { color:#9b4e5e; }
  .card { background:#fff; padding:20px; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  input, button { width:100%; padding:10px; margin:5px 0; border:1px solid #ccc; border-radius:4px; }
  button { background:#9b4e5e; color:white; cursor:pointer; }
</style>
</head>
<body>
<h2>💎 Premium Email Alerts</h2>
<div class="card">
  <p>Get early email alerts for $5/month. (Payment link placeholder – add your Venmo/PayPal).</p>
  <form method="POST" action="/subscribe">
    <input type="email" name="email" placeholder="Your email" required>
    <button type="submit">Subscribe (pending payment)</button>
  </form>
  <p><a href="/">← Back</a></p>
</div>
</body>
</html>"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    alerts = load_json(ALERTS_FILE, [])
    return render_template_string(HTML_MAIN, alerts=alerts, config=cfg)

@app.route("/add_brand", methods=["POST"])
def add_brand():
    cfg = app.config["CFG"]
    brand = request.form.get("brand", "").strip()
    if brand:
        brands = cfg.get("designer_brands", [])
        if brand not in brands:
            brands.append(brand)
            cfg["designer_brands"] = brands
            save_json(CFG_FILE, cfg)
            post_to_hub(f"➕ Added brand: {brand}", "info")
    return redirect(url_for("index"))

@app.route("/premium")
def premium_page():
    return render_template_string(PREMIUM_HTML)

@app.route("/subscribe", methods=["POST"])
def subscribe():
    email = request.form.get("email", "").strip()
    if email:
        cfg = app.config["CFG"]
        recipients = cfg.setdefault("smtp", {}).setdefault("premium_recipients", [])
        if email not in recipients:
            recipients.append(email)
            cfg["smtp"]["enabled"] = True  # enable if not already
            save_json(CFG_FILE, cfg)
            post_to_hub(f"📧 Premium subscriber added: {email}", "info")
    return redirect(url_for("index"))

# ── Background scanner ──────────────────────────────────────────────────────
def scanner_loop(config, state):
    interval = config.get("scan_interval_minutes", 60) * 60
    while True:
        scan(config, state)
        time.sleep(interval)

# ── Initialization ──────────────────────────────────────────────────────────
def initialize_files(config):
    if not STATE_FILE.exists():
        save_json(STATE_FILE, {"seen_item_ids": []})
    if not ALERTS_FILE.exists():
        save_json(ALERTS_FILE, [])

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub("Config created. Add eBay App ID and EPN campaign ID.", "warning")
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    initialize_files(config)

    state = load_json(STATE_FILE, {"seen_item_ids": []})

    # Start scanner
    threading.Thread(target=scanner_loop, args=(config, state), daemon=True).start()

    # Heartbeat
    def heartbeat():
        while True:
            time.sleep(20)
            try:
                requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
                    "bot_name": BOT_NAME, "status": "online"
                }, timeout=3)
            except Exception:
                pass
    threading.Thread(target=heartbeat, daemon=True).start()

    port = config.get("web_port", 5087)
    post_to_hub(f"👜 Fashion Resale Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
