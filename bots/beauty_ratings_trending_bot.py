#!/usr/bin/env python3
"""
beauty_ratings_trending_bot.py — Beauty Supplements/Products Rating & Trending Platform
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tracks Amazon product ratings & review counts for beauty supplements / products.
Computes trend indicators and alerts the BotController hub when ratings drop
or review velocity surges.

Real data from Amazon product pages (no API key required).
Add ASINs via the web dashboard.

Requirements:
    pip install flask requests beautifulsoup4

Configuration:
    On first run, `beauty_ratings_config.json` is created.
    Add your list of product ASINs (Amazon Standard Identification Numbers).
"""

import json
import re
import time
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request, redirect, url_for

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "beauty_ratings_trending"
BOT_NAME = "Beauty Ratings & Trends"

CFG_FILE      = Path(__file__).with_name("beauty_ratings_config.json")
PRODUCTS_FILE = Path(__file__).with_name("beauty_products.json")
HISTORY_FILE  = Path(__file__).with_name("beauty_history.json")

DEFAULT_CONFIG = {
    "web_port": 5071,
    "fetch_interval_minutes": 60,
    "rating_change_alert_threshold": 0.3,   # alert if rating drops by this amount
    "review_spike_factor": 1.5,             # alert if reviews/day doubles
    "trend_window_days": 7,
    "products": [
        {"asin": "B08H5K9KH4", "name": "Vital Proteins Collagen Peptides"},
        {"asin": "B09HR99BMH", "name": "Nutrafol Women’s Hair Growth Supplement"},
        {"asin": "B01MEEJ7D5", "name": "Hum Nutrition Daily Cleanse"},
        {"asin": "B07NJFMPTG", "name": "Moon Juice Magnesi-Om"},
        {"asin": "B01LWA6Y5G", "name": "OLLY Undeniable Beauty Gummy"},
        {"asin": "B079KGPWMS", "name": "Sports Research Biotin 10000mcg"},
        {"asin": "B08T9VVQPY", "name": "MaryRuth's Organic Liquid Probiotic"},
        {"asin": "B09FSVMJX4", "name": "Ancient Nutrition Bone Broth Protein"},
    ]
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

# ── Amazon scraper ──────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def fetch_product_data(asin):
    """Return dict with rating, review_count, product_name or None."""
    url = f"https://www.amazon.com/dp/{asin}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Look for JSON-LD script
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    data = data[0] if data else {}
                if data.get("@type") == "Product":
                    name = data.get("name", "")
                    agg = data.get("aggregateRating", {})
                    rating = agg.get("ratingValue")
                    count = agg.get("reviewCount")
                    if rating is not None and count is not None:
                        return {
                            "name": name.strip(),
                            "rating": float(rating),
                            "review_count": int(count),
                        }
            except json.JSONDecodeError:
                continue

        # Fallback: parse HTML visible text (less reliable, as second source)
        rating_elem = soup.select_one('[data-asin="' + asin + '"] .a-icon-alt') or \
                      soup.select_one('#acrPopover .a-icon-alt')
        if rating_elem:
            match = re.search(r'([\d.]+) out of', rating_elem.text)
            rating_val = float(match.group(1)) if match else None
        else:
            rating_val = None

        count_elem = soup.select_one('#acrCustomerReviewText')
        count_val = None
        if count_elem:
            count_match = re.search(r'(\d[\d,]*)', count_elem.text)
            if count_match:
                count_val = int(count_match.group(1).replace(',', ''))

        name = soup.title.text.replace("Amazon.com:", "").strip() if soup.title else None

        if rating_val is not None and count_val is not None:
            return {
                "name": name or "",
                "rating": rating_val,
                "review_count": count_val
            }
        return None
    except Exception:
        return None

# ── Trend computation ───────────────────────────────────────────────────────
def compute_trend(asin, current_rating, current_reviews, history, config):
    """Return dict with rating_trend, review_velocity_spike, and alert if needed."""
    window = config.get("trend_window_days", 7)
    now = datetime.utcnow()
    cutoff = now - timedelta(days=window)

    entries = history.get(asin, [])
    # Keep only recent entries
    entries = [e for e in entries if e["timestamp"] > cutoff.timestamp()]
    history[asin] = entries  # update in place (but we'll save later)

    if not entries:
        return {"rating_trend": "neutral", "review_spike": False}

    oldest = entries[0]
    # Rating trend: compare current to oldest in window
    rating_trend = "stable"
    if oldest.get("rating") and current_rating:
        diff = current_rating - oldest["rating"]
        if diff < -0.1:
            rating_trend = "down"
        elif diff > 0.1:
            rating_trend = "up"

    # Review velocity: average reviews/day in window
    total_reviews = current_reviews - oldest.get("review_count", current_reviews)
    days_span = max(1, (now.timestamp() - oldest["timestamp"]) / 86400)
    velocity = total_reviews / days_span if days_span > 0 else 0

    # Compare to previous window (if available)
    spike = False
    older_cutoff = cutoff - timedelta(days=window)
    older_entries = [e for e in history.get(asin, []) if e["timestamp"] < cutoff.timestamp() and e["timestamp"] > older_cutoff.timestamp()]
    if older_entries:
        oldest_older = older_entries[0]
        prev_total = oldest["review_count"] - oldest_older["review_count"]
        prev_days = max(1, (cutoff.timestamp() - oldest_older["timestamp"]) / 86400)
        prev_velocity = prev_total / prev_days if prev_days > 0 else 0
        if prev_velocity > 0 and velocity > prev_velocity * config.get("review_spike_factor", 1.5):
            spike = True

    # Also check for significant drop vs last saved point
    alert = None
    last_entry = entries[-1]
    if last_entry.get("rating") and current_rating:
        rating_drop = last_entry["rating"] - current_rating
        if rating_drop >= config.get("rating_change_alert_threshold", 0.3):
            alert = {"type": "rating_drop", "drop": round(rating_drop, 2)}

    if spike:
        alert = {"type": "review_spike", "velocity_increase": f"{velocity:.1f}/day"}

    return {"rating_trend": rating_trend, "review_spike": spike, "alert": alert}

# ── Scanner loop ────────────────────────────────────────────────────────────
def scan_products(config):
    products = load_json(PRODUCTS_FILE, [])
    history = load_json(HISTORY_FILE, {})
    now = datetime.utcnow()
    timestamp = now.timestamp()

    for prod in products:
        asin = prod["asin"]
        data = fetch_product_data(asin)
        if data is None:
            post_to_hub(f"❌ Failed to fetch data for {prod.get('name', asin)}", "warning")
            continue

        # Store the current data point
        entry = {
            "timestamp": timestamp,
            "rating": data["rating"],
            "review_count": data["review_count"]
        }
        if asin not in history:
            history[asin] = []
        history[asin].append(entry)
        # Keep only last 30 days of data
        cutoff = timestamp - 30*86400
        history[asin] = [e for e in history[asin] if e["timestamp"] > cutoff]

        # Compute trends and alerts
        trend_info = compute_trend(asin, data["rating"], data["review_count"], history, config)

        # Update product info with latest values
        prod["name"] = data["name"] or prod.get("name", asin)
        prod["rating"] = data["rating"]
        prod["review_count"] = data["review_count"]
        prod["last_fetched"] = now.isoformat()
        prod["trend"] = trend_info.get("rating_trend", "neutral")
        prod["spike"] = trend_info.get("review_spike", False)

        # Post alert if needed
        if trend_info.get("alert"):
            alert = trend_info["alert"]
            if alert["type"] == "rating_drop":
                post_to_hub(
                    f"📉 Rating drop for {prod['name']}: {alert['drop']} points",
                    "error",
                    {"asin": asin, "current_rating": data["rating"], "drop": alert["drop"]}
                )
            elif alert["type"] == "review_spike":
                post_to_hub(
                    f"📈 Review spike for {prod['name']}: {alert.get('velocity_increase', '')}",
                    "warning",
                    {"asin": asin, "review_count": data["review_count"], "velocity": alert["velocity_increase"]}
                )

    save_json(PRODUCTS_FILE, products)
    save_json(HISTORY_FILE, history)

    # Post summary
    ratings = [p["rating"] for p in products if "rating" in p]
    if ratings:
        avg_rating = sum(ratings) / len(ratings)
        post_to_hub(
            f"💄 Tracked {len(products)} products. Average rating: {avg_rating:.2f}",
            "info",
            {"product_count": len(products), "avg_rating": round(avg_rating, 2)}
        )

# ── Flask web interface ─────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

INDEX_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Beauty Ratings & Trends</title>
<style>
  body { font-family:Arial; max-width:950px; margin:40px auto; background:#fdfbf7; color:#222; }
  h1 { color:#6b3e5e; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:10px; border-bottom:1px solid #eee; text-align:left; }
  th { background:#f5eff3; }
  .trend-up { color:green; font-weight:bold; }
  .trend-down { color:red; font-weight:bold; }
  .spike { background:#fff3cd; }
  input, button { padding:8px; margin:4px; border:1px solid #ccc; border-radius:4px; }
  button { background:#6b3e5e; color:white; cursor:pointer; }
  a { color:#6b3e5e; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>💄 Beauty Product Ratings & Trends</h1>
<p>Real‑time tracking of Amazon ratings & review counts. Alerts when ratings drop or reviews spike.</p>

<div class="card">
  <h2>Add a Product</h2>
  <form method="POST" action="/add">
    <label>Amazon ASIN:</label>
    <input type="text" name="asin" placeholder="B0XXXXXXX" required>
    <label>Display Name (optional):</label>
    <input type="text" name="name" placeholder="Product name">
    <button type="submit">Add Product</button>
  </form>
</div>

<div class="card">
  <h2>Tracked Products</h2>
  <table>
    <tr>
      <th>ASIN</th><th>Product</th><th>Rating</th><th>Reviews</th>
      <th>Trend</th><th>Spike?</th><th>Last Fetched</th>
    </tr>
    {% for p in products %}
    <tr class="{% if p.spike %}spike{% endif %}">
      <td><a href="https://www.amazon.com/dp/{{ p.asin }}" target="_blank">{{ p.asin }}</a></td>
      <td>{{ p.name }}</td>
      <td>{{ p.rating }}</td>
      <td>{{ p.review_count }}</td>
      <td class="{% if p.trend == 'up' %}trend-up{% elif p.trend == 'down' %}trend-down{% else %}trend-stable{% endif %}">
        {{ p.trend }}</td>
      <td>{{ '🔥' if p.spike else '' }}</td>
      <td>{{ p.last_fetched[:16] if p.last_fetched else 'never' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% if not products %}
  <p>No products yet. Add one above.</p>
  {% endif %}
</div>

<p class="small">Data refreshes every {{ interval }} minutes. Trend arrows indicate rating movement over the last 7 days.</p>
</body>
</html>
"""

@app.route("/")
def index():
    products = load_json(PRODUCTS_FILE, [])
    interval = app.config["CFG"].get("fetch_interval_minutes", 60)
    return render_template_string(INDEX_TEMPLATE, products=products, interval=interval)

@app.route("/add", methods=["POST"])
def add_product():
    asin = request.form.get("asin", "").strip()
    name = request.form.get("name", "").strip()
    if not asin:
        return "ASIN required", 400
    products = load_json(PRODUCTS_FILE, [])
    # Check if already exists
    if any(p["asin"] == asin for p in products):
        return redirect(url_for("index"))
    products.append({
        "asin": asin,
        "name": name or asin,
        "rating": None,
        "review_count": None,
        "last_fetched": None,
        "trend": "neutral",
        "spike": False
    })
    save_json(PRODUCTS_FILE, products)
    post_to_hub(f"➕ Product added: {name or asin} (ASIN: {asin})", "info")
    return redirect(url_for("index"))

# ── Background scanner thread ──────────────────────────────────────────────
def scanner_loop(config):
    interval = config.get("fetch_interval_minutes", 60) * 60
    while True:
        scan_products(config)
        time.sleep(interval)

# ── Initialization ──────────────────────────────────────────────────────────
def initialize_files(config):
    # Create products file from default config if not existing
    if not PRODUCTS_FILE.exists():
        save_json(PRODUCTS_FILE, config.get("products", []))
    # Create empty history
    if not HISTORY_FILE.exists():
        save_json(HISTORY_FILE, {})

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub("Config created. Default beauty products loaded.", "info")
    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    initialize_files(config)

    # Start scanner
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

    port = config.get("web_port", 5071)
    post_to_hub(f"💄 Beauty Ratings Platform live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
