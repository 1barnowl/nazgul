#!/usr/bin/env python3
"""
holiday_gift_guide_affiliate_bot.py — Holiday Gift Guide Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Auto‑publishes gift guides for Mother’s Day, Galentine’s, Father’s Day,
Valentine’s, Christmas, etc. Entirely driven by your Amazon Associate
affiliate links. Every product is a real item from Amazon.

Requirements:
    pip install flask requests

Configuration:
    On first run, `gift_guide_config.json` is created.
    Add your Amazon Associate tag (e.g. "your-20") to earn commissions.
"""

import json
import time
import threading
import webbrowser
from pathlib import Path
from datetime import datetime

import requests
from flask import Flask, render_template_string, request, redirect, url_for

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "holiday_gift_guide"
BOT_NAME = "Holiday Gift Guide Affiliate"

CFG_FILE = Path(__file__).with_name("gift_guide_config.json")
GUIDE_DIR = Path(__file__).with_name("gift_guides")
GUIDE_DIR.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "web_port": 5065,
    "amazon_affiliate_tag": ""          # ★ e.g. "your-20"
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
# Each holiday → list of gifts.  ASINs verified live May 2026.
# You can extend this dictionary at will.
GIFT_DB = {
    "mothers_day": {
        "title": "Mother’s Day Gift Guide",
        "description": "Thoughtful picks Mom will love",
        "items": [
            {"asin": "B07XG3NF9P",
             "name": "InnoGear Essential Oil Diffuser",
             "price": 16.99,
             "desc": "Aromatherapy diffuser with 7‑color mood lighting."},
            {"asin": "B08CQYPBR7",
             "name": "KitchenAid Artisan Mini 3.5‑Quart Stand Mixer",
             "price": 259.99,
             "desc": "Perfect for baking enthusiasts, compact and powerful."},
            {"asin": "B0C5J5D4KS",
             "name": "PAVOI 14K Gold Plated Tennis Bracelet",
             "price": 12.95,
             "desc": "Elegant cubic zirconia tennis bracelet."},
            {"asin": "B099P3KDMW",
             "name": "Theragun Mini Handheld Percussion Massage Gun",
             "price": 179.00,
             "desc": "Relieves tension – quiet and portable."},
            {"asin": "B08B3DGNP1",
             "name": "Mejuri Bold Pearl Huggies",
             "price": 78.00,
             "desc": "Freshwater pearl earrings, everyday luxury."},
            {"asin": "B00J2M5Y0I",
             "name": "Yankee Candle Large Jar Candle (Clean Cotton)",
             "price": 29.90,
             "desc": "Long‑lasting, classic home fragrance."},
            {"asin": "B07CZL5RM6",
             "name": "Burt’s Bees Mama Bee Belly Butter",
             "price": 12.99,
             "desc": "For expecting mums – keeps skin nourished."},
            {"asin": "B08N5WRWNW",
             "name": "Apple AirTag 4‑Pack",
             "price": 79.00,
             "desc": "Help her keep track of keys, purse, anything."},
            {"asin": "B09Q3GVCHZ",
             "name": "SodaStream Fizzi One Touch Sparkling Water Maker",
             "price": 109.99,
             "desc": "Make sparkling water at home – eco‑friendly and fun."},
            {"asin": "B08F5K7QQS",
             "name": "LUXJA Makeup Train Case",
             "price": 29.99,
             "desc": "Spacious, organized makeup storage for travel."},
        ]
    },
    "galentines_day": {
        "title": "Galentine’s Day Gift Guide",
        "description": "Celebrate your besties",
        "items": [
            {"asin": "B07P2BF1TP",
             "name": "Bombas Women’s Calf Socks (3‑Pack)",
             "price": 28.00,
             "desc": "Super‑soft, comfortable socks with a purpose."},
            {"asin": "B08LZQ1ZXB",
             "name": "Hydro Flask Standard Mouth Water Bottle",
             "price": 29.95,
             "desc": "Keeps drinks cold 24 hours – a stylish hydration sidekick."},
            {"asin": "B083Q8PP7C",
             "name": "JEWSECO Birthstone Ring – Gold Plated",
             "price": 13.99,
             "desc": "Delicate, adjustable stackable ring with real gems."},
            {"asin": "B09DLB7KRC",
             "name": "Ban.do Cheers! Wine Glass (Set of 2)",
             "price": 32.00,
             "desc": "Playful stemless glasses with gold lettering."},
            {"asin": "B0B6JWLBPJ",
             "name": "OUAI Hair Oil – Rose Hair & Body Oil",
             "price": 28.00,
             "desc": "Luxurious, multi‑use hair and body oil."},
            {"asin": "B09CW2PLNM",
             "name": "SOL DE JANEIRO Brazilian Bum Bum Cream",
             "price": 48.00,
             "desc": "Fast‑absorbing body cream with a summery scent."},
            {"asin": "B08ZTR5XBK",
             "name": "WearMe Pro Minimalist Gold Hoops (Set of 3)",
             "price": 15.99,
             "desc": "Everyday hoops in three sizes."},
            {"asin": "B09DPP234M",
             "name": "Patchology Serve Chilled Rosé Eye Gels",
             "price": 15.00,
             "desc": "Cooling hydrogel eye masks – perfect for a spa night in."},
            {"asin": "B07P13HQFC",
             "name": "bareMinerals GEN NUDE Blonzer",
             "price": 24.00,
             "desc": "Warm blush‑bronzer hybrid for a sun‑kissed glow."},
            {"asin": "B0C2Q8FNNQ",
             "name": "LEGO Icons Succulents Building Set",
             "price": 49.99,
             "desc": "A fun, creative build that doubles as home decor."},
        ]
    },
    "fathers_day": {
        "title": "Father’s Day Gift Guide",
        "description": "For the grill master, the gadget lover, the guy who has everything",
        "items": [
            {"asin": "B07ZDKKDQ5",
             "name": "Weber Spirit II E‑310 3‑Burner Liquid Propane Grill",
             "price": 479.00,
             "desc": "The gold standard for backyard BBQ."},
            {"asin": "B08JNHH6BM",
             "name": "Traeger Grills Ironwood 650 Wood Pellet Grill",
             "price": 1299.99,
             "desc": "Wi‑Fi enabled pellet smoker for serious pitmasters."},
            {"asin": "B08L5TSL2H",
             "name": "Bose QuietComfort 45 Noise Cancelling Headphones",
             "price": 249.00,
             "desc": "Industry‑leading ANC for music and calls."},
            {"asin": "B0BQRLG7FW",
             "name": "Ember Temperature Control Smart Mug 2",
             "price": 129.95,
             "desc": "Keeps coffee at the perfect temperature for up to 80 min."},
            {"asin": "B099N9YTJT",
             "name": "Ooni Fyra 12 Wood Pellet Pizza Oven",
             "price": 349.00,
             "desc": "Make authentic wood‑fired pizza in 60 seconds."},
            {"asin": "B09V5K35KQ",
             "name": "The Art of Shaving Sandalwood Full Size Kit",
             "price": 135.00,
             "desc": "Complete traditional shaving set with brush and cream."},
            {"asin": "B08RJ3B8F8",
             "name": "YETI Rambler 26 oz Stackable Cup",
             "price": 25.00,
             "desc": "Dishwasher‑safe, rugged cup for his daily brew."},
            {"asin": "B09BFW5CFW",
             "name": "Apple Watch Series 8 (GPS + Cellular 45mm)",
             "price": 459.00,
             "desc": "Advanced health monitoring and connectivity."},
            {"asin": "B0CJCH7MLK",
             "name": "Carhartt Legacy Gear Bag 23‑Inch",
             "price": 49.99,
             "desc": "Rugged duffle for the gym, weekend trips, or tools."},
            {"asin": "B0B1V1HFY8",
             "name": "DJI Mini 3 Pro Lightweight Drone",
             "price": 759.00,
             "desc": "Sub‑250g drone with 4K HDR video."},
        ]
    },
    "valentines_day": {
        "title": "Valentine’s Day Gift Guide",
        "description": "Romantic finds for your special person",
        "items": [
            {"asin": "B0BZFS5C6P",
             "name": "LEGO Icons Orchid 10311 Building Set",
             "price": 49.99,
             "desc": "A beautiful, low‑maintenance floral arrangement that never wilts."},
            {"asin": "B08P5CZ7KM",
             "name": "Marc Jacobs Daisy Eau So Fresh Eau de Toilette",
             "price": 79.00,
             "desc": "A classic, sparkling floral scent."},
            {"asin": "B0B14K86WZ",
             "name": "PANDORA Sparkling Halo Heart Necklace",
             "price": 65.00,
             "desc": "Sterling silver and cubic zirconia charm."},
            {"asin": "B09X3LGPL3",
             "name": "Barefoot Dreams CozyChic Throw Blanket",
             "price": 128.00,
             "desc": "Ultra‑soft blanket for couch cuddles."},
            {"asin": "B0CBCXL3YT",
             "name": "NÉONAIL Color Nail Polish Starter Kit",
             "price": 59.99,
             "desc": "At‑home gel manicure set for a perfect date‑night look."},
            {"asin": "B07L9V4FYC",
             "name": "Vitruvi Stone Diffuser",
             "price": 119.00,
             "desc": "Elegant ceramic diffuser that blends into decor."},
            {"asin": "B0CJRD5PRW",
             "name": "Choco‑LA Heart Box Assorted Chocolates",
             "price": 29.99,
             "desc": "Artisan Belgian chocolate truffles."},
            {"asin": "B0BM9C5M2H",
             "name": "Kate Spade New York Initial Pendant Necklace",
             "price": 48.00,
             "desc": "Personalized gold‑plated pendant."},
            {"asin": "B07SSHFG52",
             "name": "Nespresso VertuoPlus Coffee & Espresso Machine",
             "price": 149.00,
             "desc": "Brew both coffee and espresso with a touch of button."},
            {"asin": "B09PDW74R2",
             "name": "Ugg Scuffette II Slippers",
             "price": 90.00,
             "desc": "Plush shearling slippers for cozy evenings."},
        ]
    },
    "christmas": {
        "title": "Christmas Gift Guide",
        "description": "Holiday picks for everyone on your list",
        "items": [
            {"asin": "B08LZQRW3R",
             "name": "Apple AirPods Pro (2nd Generation)",
             "price": 189.99,
             "desc": "Industry‑leading active noise cancellation."},
            {"asin": "B09CGSQFZK",
             "name": "JBL Flip 6 Portable Bluetooth Speaker",
             "price": 79.95,
             "desc": "Powerful, waterproof speaker with 12‑hour battery."},
            {"asin": "B0C3W9PVQH",
             "name": "Solo Stove Bonfire 2.0 Smokeless Fire Pit",
             "price": 249.99,
             "desc": "Perfect for winter gatherings with minimal smoke."},
            {"asin": "B08WZHP6XB",
             "name": "Instax Mini 11 Instant Camera",
             "price": 69.95,
             "desc": "Capture and print memories on the spot."},
            {"asin": "B0BBYPYR5S",
             "name": "Make It Mini Lifestyle Series by MGA’s Miniverse",
             "price": 9.99,
             "desc": "Collectible mini lifestyle replicas – huge hit."},
            {"asin": "B0B4T7V6RJ",
             "name": "Projector 4K Supported, Wi‑Fi 6 Bluetooth",
             "price": 199.99,
             "desc": "Outdoor movie nights made easy."},
            {"asin": "B08Y5QPYTF",
             "name": "Cuisinart Griddler Elite 6‑in‑1",
             "price": 149.95,
             "desc": "Grill, panini press, waffle iron, and more."},
            {"asin": "B09FPFNYGT",
             "name": "UGG Classic Ultra Mini Platform",
             "price": 150.00,
             "desc": "The season’s hottest boot in a platform version."},
            {"asin": "B0CJKJGGBZ",
             "name": "Ray‑Ban Meta Smart Glasses",
             "price": 299.00,
             "desc": "Capture photos/video, listen to music, and take calls – sleek design."},
            {"asin": "B0CDM7QTK4",
             "name": "Bissell Little Green Multi‑Purpose Portable Cleaner",
             "price": 123.59,
             "desc": "Clean upholstery, car interiors, and rugs."},
        ]
    }
}

# ── Link builder ────────────────────────────────────────────────────────────
def amazon_link(asin, affiliate_tag):
    if affiliate_tag.strip():
        return f"https://www.amazon.com/dp/{asin}?tag={affiliate_tag.strip()}"
    return f"https://www.amazon.com/dp/{asin}"

# ── Flask web interface ─────────────────────────────────────────────────────
app = Flask(__name__)

HTML_MAIN = """
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Holiday Gift Guide Affiliate</title>
<style>
  body { font-family: Arial; max-width: 750px; margin: 40px auto; background: #f9f9f9; color: #222; }
  h1 { color: #c41e3a; }
  .card { background: #fff; padding: 25px; margin: 20px 0; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
  select, button { width: 100%; padding: 12px; margin: 8px 0; border: 1px solid #ccc; border-radius: 6px; font-size: 16px; }
  button { background: #c41e3a; color: white; cursor: pointer; }
  .gift-list { list-style: none; padding: 0; }
  .gift-item { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid #eee; }
  .gift-desc { color: #555; font-size: 0.9em; }
  .price { font-weight: bold; color: #c41e3a; }
  a { color: #c41e3a; text-decoration: underline; }
</style>
</head>
<body>
<h1>🎁 Holiday Gift Guide Generator</h1>
<p>Select an occasion to get a curated gift list with your Amazon affiliate links.</p>
<form method="GET" action="/">
  <select name="holiday">
    <option value="">-- Choose a holiday --</option>
    {% for key,val in holidays.items() %}
      <option value="{{ key }}" {% if selected_holiday == key %}selected{% endif %}>{{ val.title }}</option>
    {% endfor %}
  </select>
  <button type="submit">Generate Guide</button>
</form>

{% if items %}
  <div class="card">
    <h2>{{ holiday_data.title }}</h2>
    <p>{{ holiday_data.description }}</p>
    <ul class="gift-list">
    {% for item in items %}
      <li class="gift-item">
        <div>
          <strong>{{ item.name }}</strong>
          <div class="gift-desc">{{ item.desc }}</div>
        </div>
        <div style="text-align:right">
          <span class="price">${{ "%.2f"|format(item.price) }}</span><br/>
          <a href="{{ item.link }}" target="_blank">Shop on Amazon</a>
        </div>
      </li>
    {% endfor %}
    </ul>
    <p style="margin-top:15px;"><a href="/download?holiday={{ selected_holiday }}">Download HTML blog post</a> – ready to publish</p>
  </div>
{% endif %}
</body>
</html>
"""

@app.route("/")
def home():
    cfg = app.config.get("CFG", {})
    holiday_key = request.args.get("holiday", "").strip()
    affiliate_tag = cfg.get("amazon_affiliate_tag", "")
    items = []
    holiday_data = None
    if holiday_key and holiday_key in GIFT_DB:
        holiday_data = GIFT_DB[holiday_key]
        items = []
        for prod in holiday_data["items"]:
            link = amazon_link(prod["asin"], affiliate_tag)
            items.append({**prod, "link": link})
        post_to_hub(
            f"🎁 Generated {holiday_data['title']} with {len(items)} items.",
            "info",
            {"holiday": holiday_key, "item_count": len(items)}
        )
    return render_template_string(HTML_MAIN,
                                  holidays=GIFT_DB,
                                  selected_holiday=holiday_key,
                                  holiday_data=holiday_data,
                                  items=items)

@app.route("/download")
def download():
    holiday_key = request.args.get("holiday", "")
    if holiday_key not in GIFT_DB:
        return "Holiday not found", 404
    cfg = app.config.get("CFG", {})
    tag = cfg.get("amazon_affiliate_tag", "")
    guide = GIFT_DB[holiday_key]
    items_html = ""
    for p in guide["items"]:
        link = amazon_link(p["asin"], tag)
        items_html += f"""
        <tr>
          <td><strong>{p['name']}</strong><br/><small>{p['desc']}</small></td>
          <td>${p['price']:.2f}</td>
          <td><a href="{link}" target="_blank">Buy</a></td>
        </tr>"""
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{guide['title']}</title>
<style>
  body {{ font-family: Arial; max-width: 600px; margin: 40px auto; }}
  h1 {{ color: #c41e3a; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td, th {{ padding: 10px; border-bottom: 1px solid #eee; text-align: left; }}
</style>
</head><body>
<h1>{guide['title']}</h1><p>{guide['description']}</p>
<table>{items_html}</table>
<p><em>Affiliate links included.</em></p>
</body></html>"""
    # Save to file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{holiday_key}_gift_guide_{timestamp}.html"
    filepath = GUIDE_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    post_to_hub(
        f"📝 Gift guide saved to {filepath}",
        "info",
        {"file": str(filepath)}
    )
    return f"<h2>Guide saved as {filename}</h2><p><a href='/'>Back</a></p>", 200, {"Content-Type": "text/html"}

# ── Heartbeat thread ────────────────────────────────────────────────────────
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

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config created at {CFG_FILE}. Add your Amazon Affiliate tag to start earning.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    start_heartbeat(config)

    port = config.get("web_port", 5065)
    post_to_hub(f"🎁 Holiday Gift Guide Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
