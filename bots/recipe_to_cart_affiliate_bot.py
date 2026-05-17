#!/usr/bin/env python3
"""
recipe_to_cart_affiliate_bot.py — Recipe‑to‑Grocery Cart Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
User picks a recipe; the bot generates an Instacart affiliate basket
(a list of links that pre‑fill searches for each ingredient).  Earns
commission when the user shops via the provided affiliate links.

Requirements:
    pip install flask requests

Configuration:
    On first run, `recipe_cart_config.json` is created.
    Fill in your Instacart affiliate tracking URL from Impact.
"""

import json
import time
import threading
import webbrowser
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, render_template_string, request

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "recipe_to_cart_affiliate"
BOT_NAME = "Recipe‑to‑Cart Affiliate"

CFG_FILE = Path(__file__).with_name("recipe_cart_config.json")
DEFAULT_CONFIG = {
    "web_port": 5063,
    "instacart_affiliate_base_url": "",  # ★ from Impact: e.g. "https://instacart.oloiyb.net/c/123456/67890/9876?u="
    "recipes": {
        "Simple Pasta": [
            "pasta", "tomato sauce", "garlic", "olive oil", "parmesan"
        ],
        "Chicken Salad": [
            "chicken breast", "mixed greens", "cherry tomatoes",
            "cucumber", "balsamic vinegar", "olive oil"
        ],
        "Vegan Smoothie": [
            "banana", "spinach", "almond milk", "protein powder",
            "flax seeds"
        ],
        "Stir Fry": [
            "chicken thigh", "broccoli", "soy sauce", "rice",
            "sesame oil", "ginger"
        ]
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

# ── Instacart link builder ──────────────────────────────────────────────────
def build_instacart_search_link(ingredient, affiliate_base):
    """
    Given an ingredient name and the affiliate base URL (from Impact),
    create a deep link that opens Instacart with a search for the ingredient.
    """
    # The base URL from Impact is like: https://instacart.oloiyb.net/c/123456/67890/9876?u=
    if not affiliate_base:
        # Fallback direct Instacart search (no commission)
        return f"https://www.instacart.com/store?search={quote(ingredient)}"
    # The affiliate base ends with "?u=" – we need to append the encoded Instacart search URL
    instacart_search_url = f"https://www.instacart.com/store?search={quote(ingredient)}"
    # The Impact link requires the destination URL to be URL-encoded.
    # If the affiliate_base already contains a placeholder, we just concatenate.
    if "?u=" in affiliate_base:
        return affiliate_base + quote(instacart_search_url, safe="")
    else:
        # Assume it's a full tracking URL that we can just append &url=
        separator = "&" if "?" in affiliate_base else "?"
        return f"{affiliate_base}{separator}url={quote(instacart_search_url, safe='')}"

# ── Flask interface ─────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CFG"] = {}

INDEX_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Recipe to Grocery Cart</title>
<style>
  body { font-family:Arial; max-width:700px; margin:40px auto; background:#faf8f5; color:#222; }
  h1 { color:#6b4c3b; }
  .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
  select, input[type=text] { width:100%; padding:10px; margin:5px 0; border:1px solid #ccc; border-radius:4px; }
  button { background:#6b4c3b; color:white; padding:10px 20px; border:none; border-radius:6px; cursor:pointer; }
  .ingredient-list { list-style:none; padding:0; }
  .ingredient-list li { margin:4px 0; }
  .ingredient-list a { color:#6b4c3b; text-decoration:underline; }
  .small { font-size:0.9em; color:#888; }
</style>
</head>
<body>
<h1>🛒 Recipe → Instacart Basket</h1>
<p>Pick a recipe and we'll generate an Instacart shopping list with our affiliate links — you earn commissions when they shop.</p>

<form method="GET" action="/">
  <div class="card">
    <label for="recipe">Choose a recipe:</label>
    <select name="recipe" onchange="this.form.submit()">
      <option value="">-- Select --</option>
      {% for name, _ in recipes.items() %}
        <option value="{{ name }}" {% if selected == name %}selected{% endif %}>{{ name }}</option>
      {% endfor %}
    </select>
    <p class="small">Or enter custom ingredients (comma‑separated):</p>
    <input type="text" name="custom" placeholder="e.g. eggs, milk, bread" value="{{ custom }}">
    <button type="submit">Generate Basket</button>
  </div>
</form>

{% if items %}
<div class="card">
  <h2>Your Shopping List</h2>
  <ul class="ingredient-list">
  {% for item in items %}
    <li><a href="{{ item.link }}" target="_blank">{{ item.ingredient }}</a></li>
  {% endfor %}
  </ul>
  <p class="small">Click each ingredient to search Instacart with our affiliate link.</p>
</div>
{% endif %}
</body>
</html>
"""

@app.route("/")
def index():
    cfg = app.config["CFG"]
    recipes = cfg.get("recipes", {})
    selected = request.args.get("recipe", "")
    custom = request.args.get("custom", "").strip()
    items = []

    ingredients = []
    if selected and selected in recipes:
        ingredients = recipes[selected]
    elif custom:
        ingredients = [i.strip() for i in custom.split(",") if i.strip()]

    affiliate_base = cfg.get("instacart_affiliate_base_url", "").strip()
    if ingredients:
        for ing in ingredients:
            link = build_instacart_search_link(ing, affiliate_base)
            items.append({"ingredient": ing.title(), "link": link})
        # Post to hub
        post_to_hub(
            f"🛒 Generated Instacart basket for {'custom mix' if custom else selected}: {', '.join(ingredients[:5])}...",
            "info",
            {"ingredients": ingredients, "affiliate_base_used": bool(affiliate_base)}
        )

    return render_template_string(INDEX_TEMPLATE,
                                  recipes=recipes, selected=selected,
                                  custom=custom, items=items)

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
            f"Config created at {CFG_FILE}. Add your Instacart affiliate base URL from Impact.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    app.config["CFG"] = config
    start_heartbeat()

    port = config.get("web_port", 5063)
    post_to_hub(f"🛒 Recipe‑to‑Cart Bot live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
