#!/usr/bin/env python3
"""
pet_care_affiliate_box_bot.py — Pet Care Affiliate Box Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Recommends monthly dog subscription boxes (BarkBox, PupJoy,
Bullymake, Super Chewer) based on a dog’s size, age, and
chewing style. Every recommendation includes a referral
/ affiliate link so you earn a bonus for each new subscriber.

Requirements:
    pip install flask requests

Configuration:
    On first run a file `pet_box_config.json` is created.
    Add your referral codes or affiliate IDs to earn.
"""

import json
import time
import threading
import webbrowser
from pathlib import Path

import requests
from flask import Flask, render_template_string, request

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "pet_box_affiliate"
BOT_NAME = "Pet Subscription Box Affiliate"

CFG_FILE = Path(__file__).with_name("pet_box_config.json")
DEFAULT_CONFIG = {
    "web_port": 5066,
    "referral_codes": {
        "barkbox": "",          # e.g. "YOURCODE" → https://barkbox.com/r/YOURCODE
        "pupjoy": "",           # e.g. "YOURCODE" → https://pupjoy.com/refer/YOURCODE
        "bullymake": "",        # e.g. "YOURCODE" → https://bullymake.com/?ref=YOURCODE
        "superchewer": "",      # e.g. "YOURCODE" → https://superchewer.com/r/YOURCODE
    },
    "generic_affiliate_param": "ref",  # fallback
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

# ── Box definitions ─────────────────────────────────────────────────────────
BOXES = {
    "barkbox": {
        "name": "BarkBox",
        "url": "https://www.barkbox.com/",
        "ref_param": "r",             # BarkBox referral links: https://barkbox.com/r/CODE
        "desc": "Themed box with 2 toys, 2 treats, and a chew, tailored to dog size.",
        "best_for": {"size": ["small","medium","large"],
                     "age": ["puppy","adult"],
                     "chew_style": ["gentle","moderate"]},
        "price": 35.00
    },
    "pupjoy": {
        "name": "PupJoy",
        "url": "https://www.pupjoy.com/",
        "ref_param": "ref",           # PupJoy uses https://pupjoy.com?ref=CODE
        "desc": "Customisable box with high‑quality toys, treats, chews, and accessories.",
        "best_for": {"size": ["small","medium","large","giant"],
                     "age": ["puppy","adult","senior"],
                     "chew_style": ["gentle","moderate","power"]},
        "price": 39.99
    },
    "bullymake": {
        "name": "Bullymake",
        "url": "https://www.bullymake.com/",
        "ref_param": "ref",
        "desc": "Durable toys and high‑value treats for power chewers. 14‑day chew replacement guarantee.",
        "best_for": {"size": ["medium","large","giant"],
                     "age": ["adult"],
                     "chew_style": ["power"]},
        "price": 39.00
    },
    "superchewer": {
        "name": "Super Chewer",
        "url": "https://www.superchewer.com/",
        "ref_param": "r",
        "desc": "BarkBox’s heavy‑duty line – tough nylon and rubber toys, plus all‑natural chews.",
        "best_for": {"size": ["medium","large","giant"],
                     "age": ["adult"],
                     "chew_style": ["moderate","power"]},
        "price": 39.00
    }
}

# ── Link builder ───────────────────────────────────────────────────────────
def build_referral_link(box_key, config):
    box = BOXES[box_key]
    code = config.get("referral_codes", {}).get(box_key, "")
    if not code:
        # Fallback generic
        param = config.get("generic_affiliate_param", "ref")
        return f"{box['url']}?{param}={code}" if code else box["url"]
    # Use official referral format if known
    if box_key == "barkbox" or box_key == "superchewer":
        return f"{box['url']}r/{code}"
    if box_key == "pupjoy":
        return f"{box['url']}ref/{code}"
    if box_key == "bullymake":
        return f"{box['url']}?ref={code}"
    return f"{box['url']}?{box['ref_param']}={code}"

# ── Matching logic ─────────────────────────────────────────────────────────
def recommend_boxes(size, age, chew_style):
    """
    Return ranked list of box keys that match the dog profile.
    """
    ranked = []
    for box_key, box in BOXES.items():
        bf = box["best_for"]
        size_match = size in bf["size"]
        age_match = age in bf["age"]
        chew_match = chew_style in bf["chew_style"]
        if size_match and age_match and chew_match:
            ranked.insert(0, box_key)  # perfect match at front
        elif size_match and (age_match or chew_match):
            ranked.append(box_key)     # partial match
        # else ignore
    return ranked

# ── Flask web app ───────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

HTML_PAGE = """
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Dog Subscription Box Finder</title>
<style>
  body { font-family:Arial; max-width:650px; margin:40px auto; background:#fefdf5; color:#222; }
  h1 { color:#6b5e3b; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  label { font-weight:bold; display:block; margin-top:10px; }
  select, button { width:100%; padding:10px; margin:5px 0 10px; border:1px solid #ccc; border-radius:4px; }
  button { background:#6b5e3b; color:white; font-size:16px; cursor:pointer; }
  .box-card { background:#f9f6ed; padding:15px; margin:12px 0; border-left:5px solid #6b5e3b; border-radius:4px; }
  .box-card h3 { margin-top:0; }
  .price { color:#6b5e3b; font-weight:bold; }
  a { color:#6b5e3b; font-weight:bold; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>🐶 Monthly Dog Box Matchmaker</h1>
<p>Tell us about your pup and we'll recommend the best subscription boxes with referral links (you earn a bonus when they subscribe).</p>
<form method="GET" action="/">
  <div class="card">
    <label>Dog Size</label>
    <select name="size">
      <option value="small" {% if sel_size=='small' %}selected{% endif %}>Small (0‑20 lbs)</option>
      <option value="medium" {% if sel_size=='medium' %}selected{% endif %}>Medium (21‑50 lbs)</option>
      <option value="large" {% if sel_size=='large' %}selected{% endif %}>Large (51‑90 lbs)</option>
      <option value="giant" {% if sel_size=='giant' %}selected{% endif %}>Giant (91+ lbs)</option>
    </select>
    <label>Age Group</label>
    <select name="age">
      <option value="puppy" {% if sel_age=='puppy' %}selected{% endif %}>Puppy (under 1 year)</option>
      <option value="adult" {% if sel_age=='adult' %}selected{% endif %}>Adult (1‑7 years)</option>
      <option value="senior" {% if sel_age=='senior' %}selected{% endif %}>Senior (8+ years)</option>
    </select>
    <label>Chewing Style</label>
    <select name="chew">
      <option value="gentle" {% if sel_chew=='gentle' %}selected{% endif %}>Gentle – plays nicely, rarely destroys toys</option>
      <option value="moderate" {% if sel_chew=='moderate' %}selected{% endif %}>Moderate – loves to chew, occasionally rips toys</option>
      <option value="power" {% if sel_chew=='power' %}selected{% endif %}>Power Chewer – destroys everything in minutes</option>
    </select>
    <button type="submit">Find Boxes</button>
  </div>
</form>
{% if results %}
<div class="card">
  <h2>Recommended Boxes</h2>
  {% for box in results %}
  <div class="box-card">
    <h3>{{ box.name }}</h3>
    <p>{{ box.desc }}</p>
    <p><span class="price">${{ "%.2f"|format(box.price) }}/month</span></p>
    <a href="{{ box.link }}" target="_blank">Subscribe with our referral →</a>
  </div>
  {% endfor %}
  <p class="small">Prices may vary. Referral bonuses are applied automatically.</p>
</div>
{% endif %}
</body>
</html>
"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    size = request.args.get("size", "").strip()
    age = request.args.get("age", "").strip()
    chew = request.args.get("chew", "").strip()
    results = []
    if size and age and chew:
        box_keys = recommend_boxes(size, age, chew)
        for bk in box_keys:
            link = build_referral_link(bk, cfg)
            box = BOXES[bk]
            results.append({
                "name": box["name"],
                "desc": box["desc"],
                "price": box["price"],
                "link": link
            })
        post_to_hub(
            f"🐶 Matched {len(box_keys)} boxes for {size}/{age}/{chew} dog",
            "info",
            {"size": size, "age": age, "chew": chew, "boxes": box_keys}
        )
    return render_template_string(HTML_PAGE,
                                  sel_size=size, sel_age=age, sel_chew=chew,
                                  results=results)

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
            f"Config file created at {CFG_FILE}. Add your referral codes to earn.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    start_heartbeat()

    port = config.get("web_port", 5066)
    post_to_hub(f"🐶 Pet Box Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
