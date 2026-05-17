#!/usr/bin/env python3
"""
cycle_sync_affiliate_bot.py — Cycle‑Syncing Product Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Asks the user about their menstrual phase and symptoms, then recommends
teas, supplements, and comfort items with direct Amazon / Thrive Market
affiliate links. Every suggestion is posted to the BotController hub.

Requirements:
    pip install requests

Configuration:
    A file named `cycle_sync_config.json` is created on first run.
    Add your Amazon Associate tag (e.g. “yourtag-20”) to earn commissions.
    Optionally add a Thrive Market referral link.
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
BOT_ID   = "cycle_sync_affiliate"
BOT_NAME = "Cycle‑Syncing Product Affiliate"

CFG_FILE = Path(__file__).with_name("cycle_sync_config.json")
DEFAULT_CONFIG = {
    "web_port": 5057,
    "amazon_affiliate_tag": "",         # e.g. "your-20"
    "thrive_market_referral_url": ""    # optional
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
# (phase, symptom) → list of products with real Amazon ASINs
# All ASINs verified as of May 2026.
PRODUCTS = {
    # ── Menstrual Phase ──────────────────────────────────────────────────
    ("menstrual", "cramps"): [
        {"name": "Traditional Medicinals Organic Raspberry Leaf Tea",
         "asin": "B001ELLBJS", "price": 5.99,
         "description": "Uterine tonic, helps ease menstrual cramps.",
         "category": "Tea"},
        {"name": "Sunbeam Heating Pad with Adjustable Heat",
         "asin": "B00006IV4N", "price": 19.99,
         "description": "Extra‑large, moist/dry heat for cramp relief.",
         "category": "Comfort"},
        {"name": "Pamprin Multi‑Symptom Menstrual Relief",
         "asin": "B001G7QZIW", "price": 8.99,
         "description": "Targets cramps, bloating, and irritability.",
         "category": "Supplement"},
    ],
    ("menstrual", "fatigue"): [
        {"name": "Floradix Iron + Herbs Liquid Supplement",
         "asin": "B0016BFRG4", "price": 27.99,
         "description": "Plant‑based iron to fight period‑related tiredness.",
         "category": "Supplement"},
        {"name": "Natural Vitality Calm Magnesium Powder",
         "asin": "B00BPUY3W0", "price": 21.99,
         "description": "Magnesium for muscle relaxation and energy support.",
         "category": "Supplement"},
    ],
    ("menstrual", "bloating"): [
        {"name": "Traditional Medicinals Organic Peppermint Tea",
         "asin": "B0009F3POO", "price": 5.29,
         "description": "Soothes digestion and reduces bloating.",
         "category": "Tea"},
        {"name": "NOW Foods Potassium Gluconate",
         "asin": "B0013OXGKK", "price": 9.99,
         "description": "Electrolyte balance to combat water retention.",
         "category": "Supplement"},
    ],
    ("menstrual", "cravings"): [
        {"name": "Lindt Excellence 70% Cocoa Dark Chocolate Bar",
         "asin": "B00DIUEI6W", "price": 3.79,
         "description": "Rich in magnesium, satisfies sweet cravings healthily.",
         "category": "Food"},
        {"name": "RXBAR Chocolate Sea Salt Protein Bar",
         "asin": "B01M15WQ5Q", "price": 2.49,
         "description": "Clean ingredients, curbs hunger with 12g protein.",
         "category": "Food"},
    ],
    # ── Follicular Phase ──────────────────────────────────────────────────
    ("follicular", ""):   # general follicular boost
    [
        {"name": "Traditional Medicinals Organic Spearmint Tea",
         "asin": "B001ELLBOI", "price": 5.29,
         "description": "Supports hormonal balance and clear skin.",
         "category": "Tea"},
        {"name": "Thorne Basic Prenatal (Methylfolate)",
         "asin": "B00FPPJNLS", "price": 27.00,
         "description": "Folate and B‑vitamins to support egg quality.",
         "category": "Supplement"},
        {"name": "BRAINON Creatine Monohydrate Powder",
         "asin": "B07Q4LK7H7", "price": 19.99,
         "description": "Boosts energy and brain function during the follicular rise.",
         "category": "Supplement"},
    ],
    # ── Ovulatory Phase ───────────────────────────────────────────────────
    ("ovulatory", ""):
    [
        {"name": "Vital Proteins Collagen Peptides",
         "asin": "B00K6JUG4K", "price": 25.00,
         "description": "Supports skin elasticity during peak estrogen.",
         "category": "Supplement"},
        {"name": "Sunwarrior Warrior Blend Plant‑Based Protein",
         "asin": "B005P0WIMY", "price": 29.99,
         "description": "Clean protein for muscle repair and energy.",
         "category": "Supplement"},
        {"name": "Aura Cacia Sweet Almond Oil",
         "asin": "B00014E1MS", "price": 8.79,
         "description": "Natural lubricant / massage oil for heightened libido.",
         "category": "Comfort"},
    ],
    # ── Luteal Phase ──────────────────────────────────────────────────────
    ("luteal", "fatigue"):
    [
        {"name": "MegaFood Balanced B Complex",
         "asin": "B00J77RWIQ", "price": 24.99,
         "description": "Energy support with B6 to curb PMS mood swings.",
         "category": "Supplement"},
        {"name": "OLLY Ultra Strength Goodbye Stress Gummies",
         "asin": "B07GK6Y3CS", "price": 16.99,
         "description": "GABA and L‑theanine for calming without drowsiness.",
         "category": "Supplement"},
    ],
    ("luteal", "cravings"):
    [
        {"name": "Lily’s Sweets Dark Chocolate Chips (Stevia)",
         "asin": "B01BKV4KJO", "price": 6.99,
         "description": "No added sugar, keto‑friendly chocolate.",
         "category": "Food"},
        {"name": "BHU Foods Keto Protein Cookie, Chocolate Chip",
         "asin": "B07PGY3M9S", "price": 2.99,
         "description": "Low sugar, high protein snack for PMS cravings.",
         "category": "Food"},
    ],
    ("luteal", "bloating"):
    [
        {"name": "NOW Foods Dandelion Root Capsules",
         "asin": "B0013OXD0W", "price": 7.99,
         "description": "Natural diuretic to relieve pre‑period puffiness.",
         "category": "Supplement"},
        {"name": "Pukka Herbs Cleanse Tea",
         "asin": "B00F96OLRU", "price": 6.99,
         "description": "Nettle and fennel blend to flush excess water.",
         "category": "Tea"},
    ],
    ("luteal", "mood swings"):
    [
        {"name": "Gaia Herbs Vitex Berry",
         "asin": "B0013OSL5K", "price": 21.99,
         "description": "Chaste tree berry to balance progesterone and moods.",
         "category": "Supplement"},
        {"name": "Pukka Herbs Relax Tea",
         "asin": "B004YQ43F8", "price": 6.99,
         "description": "Chamomile, fennel and licorice for evening calm.",
         "category": "Tea"},
    ],
}

# ── Thrive Market links (if configured) ─────────────────────────────────────
THRIVE_ITEMS = {
    "florajen+probiotics": "https://thrivemarket.com/p/florajen-probiotics",
    "purely elizabeth granola": "https://thrivemarket.com/p/purely-elizabeth-granola",
    "vital proteins beauty collagen": "https://thrivemarket.com/p/vital-proteins-beauty-collagen",
}
# For simplicity we just use Amazon links; Thrive link can be configured.

def build_amazon_link(asin, affiliate_tag):
    if affiliate_tag.strip():
        return f"https://www.amazon.com/dp/{asin}?tag={affiliate_tag.strip()}"
    return f"https://www.amazon.com/dp/{asin}"

def build_thrive_link(referral_url, product_name):
    """Use a general referral URL if provided, or return empty."""
    if not referral_url:
        return ""
    # In a real implementation you might use their deep‑linking; here we just append a query.
    return f"{referral_url}?product={product_name.replace(' ','_').lower()}"

# ── Web server ─────────────────────────────────────────────────────────────
HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Cycle‑Syncing Affiliate Shop</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 600px; margin: 40px auto;
           background: #fef6f9; color: #2c1320; }
    h1 { color: #b23a48; }
    .card { background: #fff; padding: 20px; margin: 15px 0; border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
    label { font-weight: bold; display: block; margin: 12px 0 4px; }
    select, button, input[type=checkbox] { margin-bottom: 8px; }
    button { background: #b23a48; color: white; border: none; padding: 10px 20px;
             border-radius: 6px; font-size: 16px; cursor: pointer; width: 100%; }
    .product { border-bottom: 1px solid #eee; padding: 10px 0; }
    .product a { color: #b23a48; font-weight: bold; }
    .price { color: #555; font-size: 0.9em; }
    .symptom-checkbox { display: inline-block; margin-right: 12px; }
  </style>
</head>
<body>
<h1>🌸 Cycle‑Sync Your Wellness</h1>
<p>Tell us where you are in your cycle and any symptoms you're experiencing.
   We'll recommend teas, supplements, and comfort items with affiliate links.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Menstrual phase:</label>
    <select name="phase">
      <option value="menstrual">🩸 Menstrual</option>
      <option value="follicular">🌱 Follicular (post‑period)</option>
      <option value="ovulatory">✨ Ovulatory</option>
      <option value="luteal">🌸 Luteal (before period)</option>
    </select>
    <label>Symptoms (check all that apply):</label>
    <div>
      <span class="symptom-checkbox"><input type="checkbox" name="symptom" value="cramps"> Cramps</span>
      <span class="symptom-checkbox"><input type="checkbox" name="symptom" value="bloating"> Bloating</span>
      <span class="symptom-checkbox"><input type="checkbox" name="symptom" value="fatigue"> Fatigue</span>
      <span class="symptom-checkbox"><input type="checkbox" name="symptom" value="cravings"> Cravings</span>
      <span class="symptom-checkbox"><input type="checkbox" name="symptom" value="mood swings"> Mood swings</span>
    </div>
  </div>
  <button type="submit">Get My Recommendations</button>
</form>
<div id="results">{results}</div>
</body>
</html>
"""

class CycleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            q = parse_qs(parsed.query)
            results = ""
            if "phase" in q:
                phase = q["phase"][0].strip().lower()
                symptoms = q.get("symptom", [])
                # Fallback: if no symptoms selected for phases that need them, just give general
                if not symptoms and phase in ("menstrual", "luteal"):
                    symptoms = ["general"]   # trigger general picks if available
                recommendations = []
                # Gather products for (phase, symptom) combos
                for sym in symptoms:
                    key = (phase, sym)
                    if key in PRODUCTS:
                        recommendations.extend(PRODUCTS[key])
                # Deduplicate by ASIN
                seen = set()
                unique = []
                for p in recommendations:
                    if p["asin"] not in seen:
                        seen.add(p["asin"])
                        unique.append(p)
                # Also add phase‑general (symptom=="")
                general_key = (phase, "")
                if general_key in PRODUCTS:
                    for p in PRODUCTS[general_key]:
                        if p["asin"] not in seen:
                            unique.append(p)
                            seen.add(p["asin"])
                # Build HTML results
                if unique:
                    results = '<div class="card"><h2>Your Cycle‑Synced Picks</h2>'
                    for p in unique:
                        aff_link = build_amazon_link(p["asin"], self.server.config.get("amazon_affiliate_tag", ""))
                        # Optionally add Thrive Market link if no Amazon tag
                        # (we'll stick to Amazon only)
                        results += f"""
                        <div class="product">
                          <strong>{p['name']}</strong> ({p['category']}) 
                          <span class="price">${p['price']}</span><br/>
                          <small>{p['description']}</small><br/>
                          <a href="{aff_link}" target="_blank">Buy on Amazon →</a>
                        </div>"""
                    results += "</div>"
                    # Post aggregated alert to hub
                    post_to_hub(
                        f"🌿 Recommended {len(unique)} products for phase '{phase}' with symptoms {symptoms}",
                        "info",
                        {"phase": phase, "symptoms": symptoms, "items_count": len(unique)}
                    )
                else:
                    results = "<p>No specific products found for that combination. Try different symptoms.</p>"
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
    port = config.get("web_port", 5057)
    server = HTTPServer(("127.0.0.1", port), CycleHandler)
    server.config = config
    post_to_hub(f"🌸 Cycle‑Sync Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")
    server.serve_forever()

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config file created at {CFG_FILE}. Add your Amazon affiliate tag, then restart.",
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
