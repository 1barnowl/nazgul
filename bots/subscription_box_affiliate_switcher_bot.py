#!/usr/bin/env python3
"""
subscription_box_affiliate_switcher_bot.py — Subscription Box Affiliate Switcher Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Recommends the best fashion rental service (Nuuly or RTR) based on user size and
style. Every recommendation includes a referral/affiliate link so you earn a bonus
when someone signs up through you.

Requirements:
    pip install requests

Configuration:
    A file named `subscription_switcher_config.json` will be created on first run.
    Add your Nuuly and RTR referral URLs to start earning referral bonuses.
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
BOT_ID   = "subscription_switcher"
BOT_NAME = "Subscription Box Affiliate Switcher"

CFG_FILE = Path(__file__).with_name("subscription_switcher_config.json")
DEFAULT_CONFIG = {
    "web_port": 5056,
    "nuuly_referral_url": "",   # e.g., "https://www.nuuly.com/referral/XYZ"
    "rtr_referral_url": ""      # e.g., "https://rtr.app.link/XYZ" or your affiliate link
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

# ── Recommendation engine ──────────────────────────────────────────────────
# Real data about service strengths. Based on publicly available info.
def recommend(size, style, config):
    """
    Returns (primary_service, primary_reason, primary_affiliate_link,
             secondary_service, secondary_reason, secondary_affiliate_link)
    """
    nuuly_ref = config.get("nuuly_referral_url", "").strip()
    rtr_ref = config.get("rtr_referral_url", "").strip()
    nuuly_link = nuuly_ref if nuuly_ref else "https://www.nuuly.com/"
    rtr_link = rtr_ref if rtr_ref else "https://www.renttherunway.com/"

    # Normalize inputs
    size = size.strip().upper()
    style = style.strip().lower()

    # Size logic: Nuuly carries sizes 00-32 (XS-3X), RTR has 0-22W. Both cover a wide range.
    # For sizes above 22/24, Nuuly is stronger (goes to 32). We'll flag that.
    size_num = None
    if size.startswith("XS") or size == "0":
        size_num = 0
    elif size in ("S", "4", "6"):
        size_num = 6
    elif size in ("M", "8", "10"):
        size_num = 10
    elif size in ("L", "12", "14"):
        size_num = 14
    elif size in ("XL", "16", "18"):
        size_num = 18
    elif size in ("XXL", "2X", "20", "22"):
        size_num = 22
    elif size in ("3X", "24", "26", "28", "30", "32"):
        size_num = 30
    else:
        size_num = None  # unknown

    # Style strengths
    nuuly_styles = ["boho", "casual", "trendy", "vintage", "romantic"]
    rtr_styles = ["professional", "minimalist", "special occasion", "designer", "formal"]

    # Decision rules
    primary = "Nuuly"
    primary_reason = ""
    secondary = "Rent the Runway"
    secondary_reason = ""

    # If size is very large, Nuuly may be the only realistic choice
    if size_num and size_num > 22:
        primary = "Nuuly"
        primary_reason = f"Nuuly offers sizes up to 32 (3X), while RTR tops out at 22W. For size {size} your best selection is Nuuly."
        secondary = "Rent the Runway"
        secondary_reason = "RTR has a limited plus range but still offers some designer pieces."
    elif style in nuuly_styles and style not in rtr_styles:
        primary = "Nuuly"
        primary_reason = f"Nuuly excels at {style} styles, with brands like Anthropologie, Free People, and Urban Outfitters."
        secondary = "Rent the Runway"
        secondary_reason = "RTR is stronger for formal and business attire."
    elif style in rtr_styles and style not in nuuly_styles:
        primary = "Rent the Runway"
        primary_reason = f"Rent the Runway is the go-to for {style} looks, carrying hundreds of designer labels and formalwear."
        secondary = "Nuuly"
        secondary_reason = "Nuuly leans casual and boho, but still has some elevated pieces."
    else:
        # Tie-breaker: check pricing. Nuuly $98/month for 6 items, RTR starts at $89 for 4 items.
        # For budget-conscious users Nuuly gives more bang. We'll assume user wants value.
        primary = "Nuuly"
        primary_reason = "With 6 items for $98/month, Nuuly offers more value. Both services carry a great selection."
        secondary = "Rent the Runway"
        secondary_reason = "RTR starts at $89/month for 4 items and provides access to premium designer labels."

    # Build affiliate links
    primary_link = nuuly_link if primary == "Nuuly" else rtr_link
    secondary_link = rtr_link if secondary == "Rent the Runway" else nuuly_link

    return (primary, primary_reason, primary_link,
            secondary, secondary_reason, secondary_link)

# ── Web server ─────────────────────────────────────────────────────────────
HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Fashion Rental Service Finder</title>
<style>
  body { font-family:Arial; max-width:600px; margin:40px auto; background:#fafafa; color:#222; }
  h1 { color:#4b2c5e; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.1); }
  label { font-weight:bold; display:block; margin:10px 0 4px; }
  select, button { width:100%; padding:10px; border:1px solid #ccc; border-radius:4px; }
  button { background:#4b2c5e; color:white; font-size:16px; cursor:pointer; }
  .result { margin-top:20px; padding:15px; background:#eef; border-radius:8px; }
  .result a { color:#4b2c5e; font-weight:bold; }
  .secondary { background:#eee; }
</style>
</head>
<body>
<h1>👗 Find Your Perfect Rental Subscription</h1>
<p>Tell us your size and style — we'll match you to the best fashion rental service.</p>
<form method="GET" action="/">
  <div class="card">
    <label>Your usual dress size:</label>
    <select name="size">
      <option value="XS">XS / 0</option>
      <option value="S">S / 4-6</option>
      <option value="M">M / 8-10</option>
      <option value="L">L / 12-14</option>
      <option value="XL">XL / 16-18</option>
      <option value="XXL">XXL / 20-22</option>
      <option value="3X">3X / 24-32</option>
    </select>
    <label>Your style vibe:</label>
    <select name="style">
      <option value="casual">Casual / Everyday</option>
      <option value="boho">Boho / Romantic</option>
      <option value="trendy">Trendy / Edgy</option>
      <option value="professional">Professional / Business</option>
      <option value="minimalist">Minimalist / Classic</option>
      <option value="special occasion">Special Occasion / Formal</option>
    </select>
  </div>
  <button type="submit">Get My Recommendation</button>
</form>
<div id="results">{results}</div>
</body>
</html>
"""

class SwitchHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            q = parse_qs(parsed.query)
            results = ""
            if "size" in q and "style" in q:
                size = q["size"][0]
                style = q["style"][0]
                (prim, prim_reason, prim_link,
                 sec, sec_reason, sec_link) = recommend(size, style, self.server.config)
                # Post to hub
                payload = {
                    "size": size,
                    "style": style,
                    "primary_recommendation": prim,
                    "primary_link": prim_link,
                    "secondary_recommendation": sec,
                    "secondary_link": sec_link
                }
                post_to_hub(
                    f"👚 User size {size}, style {style} → Recommended {prim} (over {sec})",
                    "info",
                    payload
                )
                results = f"""
                <div class="result">
                  <h2>Top Pick: {prim}</h2>
                  <p>{prim_reason}</p>
                  <p><a href="{prim_link}" target="_blank">Sign up for {prim} with our referral →</a></p>
                </div>
                <div class="result secondary">
                  <h3>Runner‑up: {sec}</h3>
                  <p>{sec_reason}</p>
                  <p><a href="{sec_link}" target="_blank">Check out {sec} →</a></p>
                </div>
                """
            self.send_response(200)
            self.send_header("Content-type","text/html")
            self.end_headers()
            html_out = HTML.replace("{results}", results)
            self.wfile.write(html_out.encode())
        else:
            self.send_error(404)
    def log_message(self, format, *args):
        pass

def start_server(config):
    port = config.get("web_port", 5056)
    server = HTTPServer(("127.0.0.1", port), SwitchHandler)
    server.config = config
    post_to_hub(f"Subscription Switcher Bot live at http://localhost:{port}", "info")
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

    # Heartbeat
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
