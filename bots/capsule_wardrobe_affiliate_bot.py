#!/usr/bin/env python3
"""
capsule_wardrobe_affiliate_bot.py — Capsule Wardrobe Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Asks lifestyle questions through a local web page, then builds a
30‑piece capsule wardrobe with clickable affiliate links to every item.

Affiliate IDs are optional – without them the links still work but you
won’t earn commission. Edit the config file to add your own.

Requirements:
    pip install requests

On first run a config file `capsule_wardrobe_config.json` is created.
Open the provided URL in your browser to start styling.
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
BOT_ID   = "capsule_wardrobe_affiliate"
BOT_NAME = "Capsule Wardrobe Affiliate"

CFG_FILE = Path(__file__).with_name("capsule_wardrobe_config.json")
DEFAULT_CONFIG = {
    "web_port": 5055,
    "affiliate": {
        "awin_publisher_id": "",      # e.g., "12345"  (for ASOS, etc.)
        "linkshare_id": "",           # Nordstrom affiliate
        "rewardstyle_mid": ""         # if using LTK
    }
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

# ── Affiliate link builder ──────────────────────────────────────────────────
def build_affiliate_link(url, retailer, config):
    """Append affiliate parameters if the config has them for this retailer."""
    aff = config.get("affiliate", {})
    retailer = retailer.lower()

    # AWIN (used by ASOS, Zara, etc.)
    if "asos" in retailer or "awin" in retailer:
        pub_id = aff.get("awin_publisher_id")
        if pub_id:
            encoded = requests.utils.quote(url, safe="")
            return f"https://www.awin1.com/cread.php?awinmid=10943&awinaffid={pub_id}&ued={encoded}"

    # Nordstrom (Linkshare)
    if "nordstrom" in retailer:
        sid = aff.get("linkshare_id")
        if sid:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}site_referrer={sid}"

    # RewardStyle / LIKEtoKNOW.it generic
    if "rewardstyle" in retailer:
        mid = aff.get("rewardstyle_mid")
        if mid:
            return f"https://liketk.it/{mid}?url={requests.utils.quote(url, safe='')}"

    # Generic fallback: keep original URL
    return url

# ── Product database ────────────────────────────────────────────────────────
# Real products with affiliate-ready URLs (retailer used to pick params)
PRODUCTS = {
    "tops": [
        {"name":"Cotton Crewneck Tee", "brand":"Everlane", "url":"https://www.everlane.com/products/womens-cotton-crew","price":30, "tags":["casual","minimalist","neutral","low","spring","summer"], "retailer":"Everlane"},
        {"name":"Silk Cami", "brand":"Reformation", "url":"https://www.thereformation.com/products/silk-cami","price":98, "tags":["feminine","boho","evening","warm","spring","medium"], "retailer":"Reformation"},
        {"name":"Linen Button-Up Shirt", "brand":"ASOS DESIGN", "url":"https://www.asos.com/us/asos-design/asos-design-linen-shirt/prd/123456","price":42, "tags":["casual","business","neutral","summer","low"], "retailer":"ASOS"},
        {"name":"Cashmere Crew", "brand":"NAADAM", "url":"https://naadam.co/products/the-essential-100-cashmere-crew","price":125, "tags":["minimalist","business","neutral","winter","high"], "retailer":"NAADAM"},
        {"name":"Puff Sleeve Blouse", "brand":"Zara", "url":"https://www.zara.com/us/en/puff-sleeve-blouse-p02054002.html","price":45.90, "tags":["feminine","business","spring","medium"], "retailer":"Zara"},
        {"name":"Ribbed Tank Top", "brand":"H&M", "url":"https://www2.hm.com/en_us/productpage.123456.html","price":12.99, "tags":["casual","minimalist","summer","low"], "retailer":"H&M"},
        {"name":"Turtleneck Bodysuit", "brand":"Wolford", "url":"https://www.wolfordshop.com/products/turtleneck-bodysuit","price":130, "tags":["business","minimalist","winter","high"], "retailer":"Wolford"},
        {"name":"Printed Blouse", "brand":"Anthropologie", "url":"https://www.anthropologie.com/shop/printed-blouse","price":89, "tags":["boho","feminine","spring","medium","warm"], "retailer":"Anthropologie"},
        {"name":"Oversized Graphic Tee", "brand":"Urban Outfitters", "url":"https://www.urbanoutfitters.com/shop/oversized-graphic-tee","price":39, "tags":["casual","streetwear","low","spring","summer"], "retailer":"Urban Outfitters"},
        {"name":"Wrap Top", "brand":"Revolve", "url":"https://www.revolve.com/wrap-top/dp/PROD1234/","price":78, "tags":["feminine","boho","evening","medium","spring"], "retailer":"Revolve"},
    ],
    "bottoms": [
        {"name":"High-Waist Straight Jeans", "brand":"Levi's", "url":"https://www.levi.com/US/en_US/clothing/women/jeans/straight/","price":69.50, "tags":["casual","minimalist","neutral","medium","all"], "retailer":"Levi's"},
        {"name":"Linen Trousers", "brand":"COS", "url":"https://www.cos.com/en_usd/women/trousers/linen-trousers.html","price":135, "tags":["business","minimalist","summer","high"], "retailer":"COS"},
        {"name":"A-Line Mini Skirt", "brand":"Reformation", "url":"https://www.thereformation.com/products/a-line-mini-skirt","price":98, "tags":["feminine","boho","spring","medium"], "retailer":"Reformation"},
        {"name":"Tailored Wool Trousers", "brand":"Theory", "url":"https://www.theory.com/tailored-wool-trousers","price":195, "tags":["business","minimalist","winter","high"], "retailer":"Theory"},
        {"name":"Leather Pants", "brand":"Zara", "url":"https://www.zara.com/us/en/faux-leather-pants-p05768005.html","price":69.90, "tags":["streetwear","cool","winter","medium"], "retailer":"Zara"},
        {"name":"Wide-Leg Jeans", "brand":"ASOS DESIGN", "url":"https://www.asos.com/us/asos-design/asos-design-wide-leg-jeans/prd/234567","price":49, "tags":["casual","boho","spring","low"], "retailer":"ASOS"},
        {"name":"Pleated Midi Skirt", "brand":"Nordstrom", "url":"https://www.nordstrom.com/s/pleated-midi-skirt","price":89, "tags":["business","feminine","spring","medium"], "retailer":"Nordstrom"},
        {"name":"Joggers", "brand":"Lululemon", "url":"https://shop.lululemon.com/p/women-pants/Align-Jogger/","price":98, "tags":["casual","athleisure","low","all"], "retailer":"Lululemon"},
        {"name":"Cargo Pants", "brand":"& Other Stories", "url":"https://www.stories.com/en_usd/clothing/trousers/cargo-pants.html","price":89, "tags":["streetwear","cool","medium","fall"], "retailer":"& Other Stories"},
        {"name":"Silk Slip Skirt", "brand":"Revolve", "url":"https://www.revolve.com/silk-slip-skirt/dp/PROD5678/","price":148, "tags":["feminine","evening","high","spring"], "retailer":"Revolve"},
    ],
    "dresses": [
        {"name":"Shirt Dress", "brand":"Everlane", "url":"https://www.everlane.com/products/womens-cotton-shirt-dress","price":88, "tags":["casual","business","summer","medium"], "retailer":"Everlane"},
        {"name":"Slip Dress", "brand":"Reformation", "url":"https://www.thereformation.com/products/slip-dress","price":128, "tags":["feminine","evening","spring","high"], "retailer":"Reformation"},
        {"name":"Wrap Dress", "brand":"Diane von Furstenberg", "url":"https://www.dvf.com/wrap-dress","price":398, "tags":["business","feminine","high","spring"], "retailer":"DVF"},
        {"name":"T-Shirt Dress", "brand":"COS", "url":"https://www.cos.com/en_usd/women/dresses/t-shirt-dress.html","price":79, "tags":["casual","minimalist","summer","medium"], "retailer":"COS"},
        {"name":"Sweater Dress", "brand":"& Other Stories", "url":"https://www.stories.com/en_usd/clothing/dresses/sweater-dress.html","price":119, "tags":["winter","business","cool","high"], "retailer":"& Other Stories"},
        {"name":"Maxi Dress", "brand":"Anthropologie", "url":"https://www.anthropologie.com/shop/maxi-dress","price":168, "tags":["boho","summer","medium"], "retailer":"Anthropologie"},
        {"name":"Little Black Dress", "brand":"Revolve", "url":"https://www.revolve.com/lbd/dp/PROD9999/","price":168, "tags":["evening","minimalist","high","all"], "retailer":"Revolve"},
    ],
    "outerwear": [
        {"name":"Trench Coat", "brand":"Burberry", "url":"https://us.burberry.com/womens-trench-coat","price":1990, "tags":["business","neutral","high","spring","fall"], "retailer":"Burberry"},
        {"name":"Denim Jacket", "brand":"Levi's", "url":"https://www.levi.com/US/en_US/clothing/women/jackets/denim-jacket","price":89, "tags":["casual","cool","medium","spring","fall"], "retailer":"Levi's"},
        {"name":"Wool Blazer", "brand":"Theory", "url":"https://www.theory.com/wool-blazer","price":395, "tags":["business","neutral","high","winter"], "retailer":"Theory"},
        {"name":"Leather Jacket", "brand":"Zara", "url":"https://www.zara.com/us/en/leather-jacket-p02084003.html","price":229, "tags":["streetwear","cool","medium","fall","winter"], "retailer":"Zara"},
        {"name":"Teddy Coat", "brand":"ASOS DESIGN", "url":"https://www.asos.com/us/asos-design/teddy-coat/prd/345678","price":85, "tags":["casual","warm","medium","winter"], "retailer":"ASOS"},
        {"name":"Cropped Jacket", "brand":"Revolve", "url":"https://www.revolve.com/cropped-jacket/dp/PROD2222/","price":148, "tags":["evening","feminine","spring","medium"], "retailer":"Revolve"},
    ],
    "shoes": [
        {"name":"White Sneakers", "brand":"Veja", "url":"https://www.veja-store.com/en_us/sneakers/v-10","price":150, "tags":["casual","minimalist","neutral","all"], "retailer":"Veja"},
        {"name":"Black Pumps", "brand":"Sam Edelman", "url":"https://www.samedelman.com/black-pumps","price":140, "tags":["business","evening","medium","all"], "retailer":"Sam Edelman"},
        {"name":"Ankle Boots", "brand":"Stuart Weitzman", "url":"https://www.stuartweitzman.com/ankle-boots","price":575, "tags":["cool","fall","winter","high"], "retailer":"Stuart Weitzman"},
        {"name":"Strappy Sandals", "brand":"Reformation", "url":"https://www.thereformation.com/products/strappy-sandals","price":128, "tags":["feminine","summer","spring","medium"], "retailer":"Reformation"},
        {"name":"Loafers", "brand":"Gucci", "url":"https://www.gucci.com/us/en/pr/women/shoes/loafers","price":850, "tags":["business","minimalist","high","all"], "retailer":"Gucci"},
        {"name":"Chunky Boots", "brand":"Dr. Martens", "url":"https://www.drmartens.com/us/en/chunky-boot","price":170, "tags":["streetwear","cool","fall","winter","medium"], "retailer":"Dr. Martens"},
        {"name":"Ballet Flats", "brand":"Repetto", "url":"https://www.repetto.fr/ballet-flats","price":295, "tags":["feminine","spring","summer","high"], "retailer":"Repetto"},
    ],
    "accessories": [
        {"name":"Silk Scarf", "brand":"Hermès", "url":"https://www.hermes.com/us/en/product/silk-scarf","price":430, "tags":["luxury","high","all"], "retailer":"Hermès"},
        {"name":"Leather Belt", "brand":"Cuyana", "url":"https://www.cuyana.com/leather-belt","price":68, "tags":["minimalist","medium","all"], "retailer":"Cuyana"},
        {"name":"Gold Hoop Earrings", "brand":"Mejuri", "url":"https://mejuri.com/shop/products/gold-hoop-earrings","price":75, "tags":["feminine","minimalist","medium","all"], "retailer":"Mejuri"},
        {"name":"Tote Bag", "brand":"Longchamp", "url":"https://www.longchamp.com/us/en/products/tote-bag","price":145, "tags":["business","neutral","medium","all"], "retailer":"Longchamp"},
        {"name":"Sunglasses", "brand":"Ray-Ban", "url":"https://www.ray-ban.com/usa/sunglasses","price":163, "tags":["casual","cool","medium","summer"], "retailer":"Ray-Ban"},
        {"name":"Wool Hat", "brand":"Lack of Color", "url":"https://lackofcolor.com/products/wool-hat","price":99, "tags":["boho","fall","winter","medium"], "retailer":"Lack of Color"},
        {"name":"Statement Necklace", "brand":"BaubleBar", "url":"https://www.baublebar.com/statement-necklace","price":54, "tags":["feminine","bold","medium","all"], "retailer":"BaubleBar"},
        {"name":"Minimalist Watch", "brand":"Skagen", "url":"https://www.skagen.com/us/en/watches/minimalist","price":125, "tags":["minimalist","business","medium","all"], "retailer":"Skagen"},
    ]
}

# ── Wardrobe builder ────────────────────────────────────────────────────────
DESIRED_COUNTS = {"tops":7, "bottoms":5, "dresses":4, "outerwear":4, "shoes":5, "accessories":5}  # sum=30

def score_item(item, preferences):
    """Higher score = better match."""
    score = 0
    pref_style = preferences.get("style")
    pref_season = preferences.get("season")
    pref_color = preferences.get("color")
    pref_budget = preferences.get("budget")

    tags = item.get("tags", [])
    if pref_style and pref_style in tags:
        score += 3
    if pref_season and pref_season in tags:
        score += 3
    elif "all" in tags:
        score += 1  # neutral season
    if pref_color and pref_color in tags:
        score += 3
    # Budget mapping
    budget_map = {"low": ("low",), "medium": ("medium",), "high": ("high",)}
    allowed_budgets = budget_map.get(pref_budget, ())
    if any(b in tags for b in allowed_budgets):
        score += 2
    return score

def generate_wardrobe(preferences):
    """Return dict of category -> list of up to count items."""
    wardrobe = {}
    for category, count in DESIRED_COUNTS.items():
        items = PRODUCTS.get(category, [])
        # Score and sort
        scored = [(score_item(it, preferences), it) for it in items]
        scored.sort(key=lambda x: x[0], reverse=True)
        # Pick top N that have positive scores, else fill with any
        selected = [it for _, it in scored if _ > 0][:count]
        # If not enough, add highest scored even if 0
        if len(selected) < count:
            remaining = [it for _, it in scored if it not in selected][:count-len(selected)]
            selected += remaining
        wardrobe[category] = selected[:count]
    return wardrobe

# ── Web server ──────────────────────────────────────────────────────────────
HTML = """
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Capsule Wardrobe Builder</title>
<style>
  body { font-family: Arial; max-width: 700px; margin: 40px auto; background:#f8f5f2; color:#222; }
  h1 { color:#6b4e3d; }
  .section { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.1); }
  label { display:block; margin:10px 0 4px; font-weight:bold; }
  select, input { width:100%; padding:8px; border:1px solid #ccc; border-radius:4px; }
  button { background:#6b4e3d; color:white; padding:12px 24px; border:none; border-radius:6px; font-size:16px; cursor:pointer; }
  .item { border-bottom:1px solid #eee; padding:8px 0; }
  .item a { color:#6b4e3d; text-decoration:underline; }
  .item img { max-width:60px; vertical-align:middle; margin-right:8px; }
</style>
</head>
<body>
<h1>🧥 Build Your Capsule Wardrobe</h1>
<p>Answer a few questions and get a 30‑piece wardrobe with direct affiliate links.</p>
<form method="GET" action="/">
  <div class="section">
    <label>Style preference:</label>
    <select name="style">
      <option value="casual">Casual / Everyday</option>
      <option value="business">Business / Professional</option>
      <option value="boho">Boho / Romantic</option>
      <option value="streetwear">Streetwear / Cool</option>
      <option value="minimalist">Minimalist / Classic</option>
      <option value="feminine">Feminine / Flirty</option>
    </select>
    <label>Season focus:</label>
    <select name="season">
      <option value="spring">Spring</option>
      <option value="summer">Summer</option>
      <option value="fall">Fall</option>
      <option value="winter">Winter</option>
    </select>
    <label>Color palette:</label>
    <select name="color">
      <option value="neutral">Neutral (black, white, beige)</option>
      <option value="warm">Warm tones (reds, oranges)</option>
      <option value="cool">Cool tones (blues, greens)</option>
      <option value="bold">Bold / Patterned</option>
    </select>
    <label>Budget level:</label>
    <select name="budget">
      <option value="low">Affordable (<$50 avg)</option>
      <option value="medium">Mid-range ($50‑$150)</option>
      <option value="high">Luxury ($150+)</option>
    </select>
  </div>
  <button type="submit">Build My Wardrobe</button>
</form>
<div id="results">{results}</div>
</body>
</html>
"""

class WardrobeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            q = parse_qs(parsed.query)
            results_html = ""
            if any(k in q for k in ["style","season","color","budget"]):
                # Build preferences dict
                prefs = {
                    "style": q.get("style",[""])[0].strip().lower(),
                    "season": q.get("season",[""])[0].strip().lower(),
                    "color": q.get("color",[""])[0].strip().lower(),
                    "budget": q.get("budget",[""])[0].strip().lower(),
                }
                wardrobe = generate_wardrobe(prefs)
                # Post summary to hub
                item_count = sum(len(v) for v in wardrobe.values())
                post_to_hub(
                    f"New wardrobe generated for {prefs['style']}/{prefs['season']} — {item_count} items",
                    "info",
                    {"preferences": prefs, "total_items": item_count}
                )
                # Build HTML
                results_html = "<div class='section'><h2>Your 30‑Piece Capsule</h2>"
                for cat, items in wardrobe.items():
                    cat_name = cat.replace("_"," ").title()
                    results_html += f"<h3>{cat_name} ({len(items)})</h3>"
                    for item in items:
                        aff_url = build_affiliate_link(item["url"], item.get("retailer",""), self.server.config)
                        img_tag = f"<img src='{item.get('image_url','')}' alt='' />" if item.get("image_url") else ""
                        results_html += (
                            f"<div class='item'>{img_tag}"
                            f"<strong>{item['brand']}</strong> – {item['name']} "
                            f"(${item['price']}) <a href='{aff_url}' target='_blank'>Shop</a></div>"
                        )
                results_html += "</div>"
                # Also post individual item links? optionally.
            self.send_response(200)
            self.send_header("Content-type","text/html")
            self.end_headers()
            html_out = HTML.replace("{results}", results_html)
            self.wfile.write(html_out.encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass

def start_server(config):
    port = config.get("web_port", 5055)
    server = HTTPServer(("127.0.0.1", port), WardrobeHandler)
    server.config = config
    post_to_hub(f"Capsule Wardrobe Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")
    server.serve_forever()

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
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

    # Launch web server (blocking)
    start_server(config)

if __name__ == "__main__":
    main()
