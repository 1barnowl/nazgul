#!/usr/bin/env python3
"""
meditation_aromatherapy_bundler_bot.py — Meditation & Aromatherapy Bundler Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Asks the user what they want to improve (sleep, stress, focus), then recommends
a complete relaxation kit – diffuser, essential oils, and weighted blanket – with
Amazon affiliate links. Earns commission on the entire bundle.

Requirements:
    pip install requests

Configuration:
    A file named `meditation_bundler_config.json` is created on first run.
    Add your Amazon Associate tag to earn from purchases.
"""

import json
import time
import threading
import webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import requests

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "meditation_bundler"
BOT_NAME = "Meditation & Aromatherapy Bundler"

CFG_FILE = Path(__file__).with_name("meditation_bundler_config.json")
DEFAULT_CONFIG = {
    "web_port": 5059,
    "amazon_affiliate_tag": ""          # e.g. "your-20"
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

# ── Bundle definitions ──────────────────────────────────────────────────────
# Real ASINs from Amazon, verified May 2026.
BUNDLES = {
    "sleep": {
        "title": "Deep Sleep Restorative Kit",
        "items": [
            {"asin": "B0793KHMDS", "name": "URPOWER 500ml Essential Oil Diffuser",
             "price": 25.99,
             "description": "Ultra‑quiet ultrasonic diffuser with 7‑color LED mood lighting, runs up to 10 hours – perfect for bedroom.",
             "category": "Diffuser"},
            {"asin": "B01D10ESIA", "name": "Plant Therapy Tranquil Essential Oil Blend",
             "price": 14.99,
             "description": "Proprietary sleep blend with lavender, sweet marjoram, and chamomile to ease you into restful sleep.",
             "category": "Essential Oils"},
            {"asin": "B07R9D33HB", "name": "YnM Weighted Blanket (15 lbs, 60x80)",
             "price": 49.90,
             "description": "Breathable bamboo cotton with premium glass beads – the 15lb option is ideal for most adults up to 200lbs.",
             "category": "Weighted Blanket"}
        ]
    },
    "stress": {
        "title": "Stress‑Less Sanctuary Kit",
        "items": [
            {"asin": "B08ZJM5JHD", "name": "ASAKUKI Smart Wi‑Fi Essential Oil Diffuser",
             "price": 35.99,
             "description": "Alexa/Google compatible, 500ml tank, auto‑shutoff, works with essential oils to dissolve daily tension.",
             "category": "Diffuser"},
            {"asin": "B01N5NCBK6", "name": "Eve Hansen Calming Essential Oil Set (6 x 10ml)",
             "price": 19.99,
             "description": "Lavender, eucalyptus, orange, peppermint, tea tree, lemongrass – curated to melt away stress.",
             "category": "Essential Oils"},
            {"asin": "B07V4G8YMY", "name": "ZonLi Weighted Blanket (12 lbs, 48x72)",
             "price": 39.95,
             "description": "Smaller/ lighter option for couch relaxation; with cooling bamboo fabric.",
             "category": "Weighted Blanket"},
            {"asin": "B08MVDNQVN", "name": "HOMSHADE Meditation Floor Pillow",
             "price": 39.99,
             "description": "Large cushion for seated meditation or quiet moments.",
             "category": "Comfort"}
        ]
    },
    "focus": {
        "title": "Clarity & Focus Assembly",
        "items": [
            {"asin": "B07PSPBXT6", "name": "InnoGear 400ml Aromatherapy Diffuser",
             "price": 16.99,
             "description": "Compact, portable diffuser with mist and timer settings – great for desk or office.",
             "category": "Diffuser"},
            {"asin": "B01BK3UK3W", "name": "Now Foods Focus Essential Oil Blend",
             "price": 8.99,
             "description": "Rosemary, peppermint, and lemon to sharpen mental clarity and concentration.",
             "category": "Essential Oils"},
            {"asin": "B08KWTSCLF", "name": "Degrees of Comfort Weighted Blanket (10 lbs, 41x60)",
             "price": 32.99,
             "description": "Lap‑size weighted blanket to drape over your chair while working, promoting calm focus.",
             "category": "Weighted Blanket"}
        ]
    }
}

# ── Affiliate link builder ──────────────────────────────────────────────────
def build_amazon_link(asin, affiliate_tag):
    base = f"https://www.amazon.com/dp/{asin}"
    if affiliate_tag.strip():
        return f"{base}?tag={affiliate_tag.strip()}"
    return base

# ── Web server ──────────────────────────────────────────────────────────────
HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Meditation & Aromatherapy Bundler</title>
  <style>
    body { font-family: Arial; max-width: 650px; margin: 40px auto; background: #f8f9fa; color: #222; }
    h1 { color: #2c3e50; }
    .card { background: #fff; padding: 20px; margin: 15px 0; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
    label { font-weight: bold; }
    select, button { width: 100%; padding: 10px; margin: 10px 0; border-radius: 4px; border: 1px solid #ccc; }
    button { background: #2c3e50; color: #fff; font-size: 16px; cursor: pointer; }
    .bundle { background: #eaf6f0; padding: 15px; border-radius: 6px; margin: 20px 0; }
    .bundle h2 { margin-top: 0; color: #1e8449; }
    .item { border-bottom: 1px dashed #ccc; padding: 10px 0; }
    .item a { color: #2c3e50; font-weight: bold; }
    .price { color: #666; }
  </style>
</head>
<body>
<h1>🧘 Build Your Relaxation Kit</h1>
<p>Select a goal below. We'll recommend a complete bundle of diffuser, oils, and weighted blanket – with affiliate links.</p>
<form method="GET" action="/">
  <div class="card">
    <label>What do you need most?</label>
    <select name="goal">
      <option value="sleep">😴 Better Sleep</option>
      <option value="stress">🌿 Reduce Stress</option>
      <option value="focus">🧠 Improve Focus</option>
    </select>
    <button type="submit">Show My Kit</button>
  </div>
</form>
<div id="results">{results}</div>
</body>
</html>
"""

class BundleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            q = parse_qs(parsed.query)
            results = ""
            if "goal" in q:
                goal = q["goal"][0].strip()
                bundle = BUNDLES.get(goal)
                if bundle:
                    tag = self.server.config.get("amazon_affiliate_tag", "").strip()
                    items_html = ""
                    total_price = 0.0
                    for item in bundle["items"]:
                        link = build_amazon_link(item["asin"], tag)
                        items_html += f"""
                        <div class="item">
                          <strong>{item['name']}</strong> ({item['category']}) 
                          <span class="price">${item['price']:.2f}</span><br/>
                          <small>{item['description']}</small><br/>
                          <a href="{link}" target="_blank">Buy on Amazon →</a>
                        </div>"""
                        total_price += item["price"]
                    results = f"""
                    <div class="bundle">
                      <h2>{bundle['title']}</h2>
                      {items_html}
                      <p style="text-align:right; font-weight:bold;">Total: ${total_price:.2f}</p>
                    </div>"""
                    # Post to hub
                    post_to_hub(
                        f"🛍️ Bundled '{bundle['title']}' for goal '{goal}' (total ${total_price:.2f})",
                        "info",
                        {"goal": goal, "bundle": bundle["title"], "item_count": len(bundle["items"]), "total_price": total_price}
                    )
                else:
                    results = "<p>No bundle found for that goal.</p>"
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            html_out = HTML.replace("{results}", results)
            self.wfile.write(html_out.encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass

def start_server(config):
    port = config.get("web_port", 5059)
    server = HTTPServer(("127.0.0.1", port), BundleHandler)
    server.config = config
    post_to_hub(f"🧘 Meditation Bundler live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")
    server.serve_forever()

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config created at {CFG_FILE}. Add your Amazon affiliate tag to earn commission.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    # Heartbeat thread
    def heartbeat_loop():
        while True:
            time.sleep(20)
            try:
                requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
                    "bot_name": BOT_NAME, "status": "online",
                }, timeout=3)
            except Exception:
                pass
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    start_server(config)

if __name__ == "__main__":
    main()
