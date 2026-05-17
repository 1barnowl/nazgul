#!/usr/bin/env python3
"""
beauty_price_tracker_bot.py — Beauty Product Price Tracker Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors price drops on cosmetics, skincare, and hair tools.
Scrapes Amazon (ASIN) and other retailers for current prices,
posts alerts to the BotController hub with affiliate links.

Requirements:
    pip install flask requests beautifulsoup4 lxml

Configuration:
    A file `beauty_price_tracker_config.json` is created on first run.
    Add your Amazon Associate tag (optional) and other affiliate IDs.
"""

import json
import re
import time
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, redirect, url_for

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "beauty_price_tracker"
BOT_NAME = "Beauty Price Tracker"

CFG_FILE    = Path(__file__).with_name("beauty_price_tracker_config.json")
ITEMS_FILE  = Path(__file__).with_name("tracked_items.json")
HISTORY_FILE = Path(__file__).with_name("price_history.json")

DEFAULT_CONFIG = {
    "web_port": 5073,
    "check_interval_minutes": 60,
    "amazon_affiliate_tag": "",          # e.g. "yourtag-20"
    "generic_affiliate_param": "ref",    # for non‑Amazon stores
    "generic_affiliate_id": "",
    "smtp": {                            # optional – email premium alerts
        "enabled": False,
        "server": "smtp.gmail.com",
        "port": 587,
        "username": "",
        "password": "",
        "from_email": ""
    },
    "premium_alert_recipients": []       # list of emails to notify on price drop
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

# ── Price scraping ──────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def scrape_price(url, asin=None, store="amazon"):
    """
    Returns current price (float) or None if not found.
    For Amazon, uses ASIN lookup; for other stores, tries generic extraction.
    """
    if store.lower() == "amazon" and asin:
        url = f"https://www.amazon.com/dp/{asin}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # 1) Try JSON-LD first (many e‑commerce sites)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    data = data[0]
                if data.get("@type") in ("Product", "IndividualProduct"):
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price")
                    if price is not None:
                        return float(price)
            except Exception:
                continue

        # 2) Amazon-specific fallback
        if store.lower() == "amazon" or "amazon" in url:
            # Look for price element
            price_elem = soup.select_one('.a-price .a-offscreen')
            if price_elem:
                price_str = price_elem.get_text(strip=True)
                price_num = re.sub(r'[^\d.]', '', price_str)
                if price_num:
                    return float(price_num)
            # Alternative: "price_inside_buybox" span
            buybox_price = soup.find("span", id="price_inside_buybox")
            if buybox_price:
                price_str = buybox_price.get_text(strip=True)
                price_num = re.sub(r'[^\d.]', '', price_str)
                if price_num:
                    return float(price_num)

        # 3) Generic meta tag extraction (og:price:amount)
        meta_price = soup.find("meta", property="product:price:amount")
        if meta_price and meta_price.get("content"):
            try:
                return float(meta_price["content"])
            except:
                pass

        return None
    except Exception:
        return None

# ── Affiliate link builder ──────────────────────────────────────────────────
def build_affiliate_link(product, config):
    """Return an affiliate‑ready URL."""
    url = product.get("url", "")
    asin = product.get("asin", "")
    store = product.get("store", "amazon").lower()

    if store == "amazon" or "amazon" in url:
        tag = config.get("amazon_affiliate_tag", "").strip()
        if tag and asin:
            return f"https://www.amazon.com/dp/{asin}?tag={tag}"
        elif tag:
            # Append to URL
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}tag={tag}"
        return url

    # Generic affiliate
    param = config.get("generic_affiliate_param", "ref")
    aff_id = config.get("generic_affiliate_id", "").strip()
    if aff_id:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{param}={aff_id}"
    return url

# ── Price checking and alerting ─────────────────────────────────────────────
def check_prices(config):
    items = load_json(ITEMS_FILE, [])
    history = load_json(HISTORY_FILE, {})
    now_ts = time.time()
    now_str = datetime.utcnow().isoformat()

    for item in items:
        pid = item["id"]
        asin = item.get("asin")
        url = item.get("url", f"https://www.amazon.com/dp/{asin}" if asin else "")
        store = item.get("store", "amazon")
        current_price = scrape_price(url, asin, store)

        if current_price is None:
            # Could not fetch – skip
            continue

        # Update item’s current price
        item["current_price"] = round(current_price, 2)
        item["last_checked"] = now_str

        # Record history
        if pid not in history:
            history[pid] = []
        history[pid].append({
            "timestamp": now_ts,
            "price": current_price
        })

        # Detect drop from previous price (if any)
        prev_price = item.get("previous_price")
        if prev_price and current_price < prev_price:
            drop_percent = round((1 - current_price/prev_price) * 100, 1)
            aff_link = build_affiliate_link(item, config)
            summary = f"💰 Price drop on {item['name']}: ${prev_price:.2f} → ${current_price:.2f} ({drop_percent}% off)"
            payload = {
                "product": item["name"],
                "store": store,
                "old_price": prev_price,
                "new_price": current_price,
                "drop_percent": drop_percent,
                "affiliate_link": aff_link,
                "url": url
            }
            post_to_hub(summary, "warning", payload)

            # Premium email alert
            if config.get("smtp", {}).get("enabled"):
                send_email_alert(summary, payload, config)

        # Update previous price for next check
        item["previous_price"] = current_price

    save_json(ITEMS_FILE, items)
    save_json(HISTORY_FILE, history)

def send_email_alert(subject, details, config):
    """Send email to premium recipients."""
    smtp = config.get("smtp", {})
    if not smtp.get("server") or not smtp.get("username"):
        return
    recipients = config.get("premium_alert_recipients", [])
    if not recipients:
        return
    import smtplib
    from email.mime.text import MIMEText
    body = f"""Price drop detected:
    Product: {details['product']}
    Store: {details['store']}
    Old Price: ${details['old_price']:.2f}
    New Price: ${details['new_price']:.2f} ({details['drop_percent']}% off)
    Link: {details['affiliate_link']}
    """
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = smtp["from_email"]
    msg["To"] = ", ".join(recipients)
    try:
        with smtplib.SMTP(smtp["server"], smtp["port"], timeout=10) as server:
            server.starttls()
            server.login(smtp["username"], smtp["password"])
            server.send_message(msg)
    except Exception as e:
        post_to_hub(f"Failed to send premium email: {e}", "error")

# ── Flask web interface ─────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Beauty Price Tracker</title>
<style>
  body { font-family:Arial; max-width:900px; margin:40px auto; background:#fdf8f5; color:#222; }
  h1 { color:#b25c5c; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  input, select { width:100%; padding:8px; margin:4px 0 10px; border:1px solid #ccc; border-radius:4px; }
  button { background:#b25c5c; color:white; padding:10px 20px; border:none; border-radius:6px; cursor:pointer; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:10px; border-bottom:1px solid #eee; text-align:left; }
  th { background:#f5e5e5; }
  .drop { color:green; font-weight:bold; }
  .no-drop { color:#888; }
  a { color:#b25c5c; }
  .small { font-size:0.9em; color:#888; }
  .history-table td { font-size:0.9em; }
</style>
</head>
<body>
<h1>💄 Beauty Price Tracker</h1>
<p>Track price drops on cosmetics, skincare, hair tools. Affiliate links included.</p>

<div class="card">
  <h2>Add Product to Track</h2>
  <form method="POST" action="/add">
    <label>Product Name</label><input type="text" name="name" required>
    <label>Store</label>
    <select name="store">
      <option value="amazon">Amazon</option>
      <option value="sephora">Sephora</option>
      <option value="ulta">Ulta</option>
      <option value="other">Other</option>
    </select>
    <label>Amazon ASIN (if Amazon)</label><input type="text" name="asin" placeholder="e.g. B08H5K9KH4">
    <label>Product URL (alternative)</label><input type="text" name="url" placeholder="Full URL">
    <button type="submit">Add to Tracking</button>
  </form>
</div>

<div class="card">
  <h2>Tracked Products</h2>
  {% if items %}
  <table>
    <tr><th>Name</th><th>Store</th><th>Current Price</th><th>Previous</th><th>Last Checked</th><th>History</th><th>Remove</th></tr>
    {% for item in items %}
    <tr>
      <td>{{ item.name }}</td>
      <td>{{ item.store }}</td>
      <td>${{ item.current_price if item.current_price else 'N/A' }}</td>
      <td>${{ item.previous_price if item.previous_price else 'N/A' }}</td>
      <td>{{ item.last_checked[:16] if item.last_checked else 'never' }}</td>
      <td><a href="/history/{{ item.id }}">view</a></td>
      <td><a href="/remove/{{ item.id }}" style="color:red;">✕</a></td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p>No products tracked yet.</p>
  {% endif %}
</div>
<p class="small">Auto‑checks every {{ interval }} minutes. Price drops appear in the BotController hub as alerts.</p>
</body>
</html>
"""

HISTORY_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Price History – {{ product.name }}</title>
<style>
  body { font-family:Arial; max-width:600px; margin:40px auto; background:#fff; }
  h2 { color:#b25c5c; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:8px; border-bottom:1px solid #eee; }
  a { color:#b25c5c; }
</style>
</head>
<body>
<h2>Price History for {{ product.name }}</h2>
<p>Store: {{ product.store }} | Current: ${{ product.current_price if product.current_price else 'N/A' }}</p>
<table>
  <tr><th>Date</th><th>Price</th></tr>
  {% for entry in history %}
  <tr><td>{{ entry.date }}</td><td>${{ "%.2f"|format(entry.price) }}</td></tr>
  {% endfor %}
</table>
<a href="/">← Back</a>
</body>
</html>
"""

@app.route("/")
def index():
    items = load_json(ITEMS_FILE, [])
    interval = app.config["CFG"].get("check_interval_minutes", 60)
    return render_template_string(TEMPLATE, items=items, interval=interval)

@app.route("/add", methods=["POST"])
def add_product():
    items = load_json(ITEMS_FILE, [])
    new_id = max([i["id"] for i in items], default=0) + 1
    name = request.form.get("name", "").strip()
    store = request.form.get("store", "amazon").strip().lower()
    asin = request.form.get("asin", "").strip() or None
    url = request.form.get("url", "").strip() or None

    if not name:
        return "Name required", 400
    item = {
        "id": new_id,
        "name": name,
        "store": store,
        "asin": asin,
        "url": url,
        "current_price": None,
        "previous_price": None,
        "last_checked": None
    }
    items.append(item)
    save_json(ITEMS_FILE, items)
    post_to_hub(f"📦 New product tracked: {name} ({store})", "info")
    return redirect(url_for("index"))

@app.route("/remove/<int:item_id>")
def remove_product(item_id):
    items = load_json(ITEMS_FILE, [])
    items = [i for i in items if i["id"] != item_id]
    save_json(ITEMS_FILE, items)
    post_to_hub(f"🗑️ Removed product #{item_id}", "info")
    return redirect(url_for("index"))

@app.route("/history/<int:item_id>")
def price_history(item_id):
    items = load_json(ITEMS_FILE, [])
    product = next((i for i in items if i["id"] == item_id), None)
    if not product:
        return "Product not found", 404
    all_history = load_json(HISTORY_FILE, {})
    pid_history = all_history.get(str(item_id), [])
    # Format dates
    for entry in pid_history:
        entry["date"] = datetime.fromtimestamp(entry["timestamp"]).strftime("%Y-%m-%d %H:%M")
    return render_template_string(HISTORY_TEMPLATE, product=product, history=pid_history)

# ── Scanner background thread ──────────────────────────────────────────────
def scanner_loop(config):
    interval = config.get("check_interval_minutes", 60) * 60
    while True:
        check_prices(config)
        time.sleep(interval)

# ── Initialization ──────────────────────────────────────────────────────────
def initialize_files():
    if not ITEMS_FILE.exists():
        save_json(ITEMS_FILE, [])
    if not HISTORY_FILE.exists():
        save_json(HISTORY_FILE, {})

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config created at {CFG_FILE}. Add your Amazon affiliate tag for commissions.",
            "warning"
        )
        # Don't exit; run with defaults

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    initialize_files()

    # Start scanner thread
    threading.Thread(target=scanner_loop, args=(config,), daemon=True).start()

    # Heartbeat
    def heartbeat():
        while True:
            time.sleep(20)
            try:
                requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
                    "bot_name": BOT_NAME,
                    "status": "online",
                }, timeout=3)
            except Exception:
                pass
    threading.Thread(target=heartbeat, daemon=True).start()

    port = config.get("web_port", 5073)
    post_to_hub(f"💄 Price Tracker Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
