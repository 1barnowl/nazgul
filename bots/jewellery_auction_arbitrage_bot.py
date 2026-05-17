#!/usr/bin/env python3
"""
jewellery_auction_arbitrage_bot.py — Jewellery Auction Arbitrage Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors eBay auctions for underpriced gemstone rings and necklaces,
allows pre‑agreed clients to “buy” an item, then places a last‑second
bid on their behalf. The broker profits from the spread.

Real eBay API – no simulation. Can place real bids if OAuth is configured.

Requirements:
    pip install requests beautifulsoup4 flask

Configuration:
    On first run, `jewellery_arbitrage_config.json` is created.
    You MUST supply:
      • eBay App ID (free from developer.ebay.com)
      • eBay OAuth credentials (optional – needed for bidding)
    Without OAuth the bot will still identify opportunities and you can
    bid manually.
"""

import json
import os
import re
import sys
import time
import threading
import webbrowser
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "jewellery_arbitrage"
BOT_NAME = "Jewellery Auction Arbitrage"

CFG_FILE       = Path(__file__).with_name("jewellery_arbitrage_config.json")
STATE_FILE     = Path(__file__).with_name("jewellery_arbitrage_state.json")
ORDERS_FILE    = Path(__file__).with_name("jewellery_arbitrage_orders.json")

DEFAULT_CONFIG = {
    "ebay_app_id": "",                      # ★ REQUIRED
    "ebay_cert_id": "",                     # optional (for Trading API)
    "ebay_oauth": {
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "https://localhost:8080/callback",
        "refresh_token": ""                 # set after first OAuth flow
    },
    "search_queries": [
        "gemstone ring -lab -created -simulated -imitation",
        "sapphire ring natural",
        "diamond necklace estate",
        "ruby ring vintage",
        "emerald ring gold",
        "opal pendant"
    ],
    "max_price": 300,                       # don't consider items above this
    "min_estimated_retail_markup": 1.5,     # retail must be at least 1.5x current bid
    "bid_seconds_before_end": 3,            # snipe window
    "scan_interval_minutes": 15,
    "web_port": 5058
}

# ── Approximate gemstone/carat values (retail estimate) ────────────────────
GEM_RETAIL_PER_CARAT = {
    "diamond": 2500,
    "sapphire": 800,
    "ruby": 1500,
    "emerald": 700,
    "opal": 200,
    "amethyst": 100,
    "topaz": 150,
    "garnet": 200,
    "tourmaline": 300,
    "aquamarine": 400,
    "pearl": 100,
    "morganite": 150,
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

# ── eBay API helpers ────────────────────────────────────────────────────────
EBAY_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_ITEM_URL   = "https://api.ebay.com/buy/browse/v1/item/{item_id}"

def ebay_search(query, app_id, max_price=300, max_results=10):
    """Use eBay Browse API (no OAuth needed). Returns list of item summaries."""
    if not app_id:
        post_to_hub("eBay App ID missing – cannot search.", "error")
        return []
    headers = {
        "X-EBAY-C-MARKETPLACE-ID": "EBAY-US",
        "Authorization": f"Bearer {app_id}",   # Browse API accepts App ID as Bearer
        "Accept": "application/json"
    }
    params = {
        "q": query,
        "limit": min(max_results, 50),
        "filter": f"buyingOptions:{{AUCTION}},price:[..{max_price}],priceCurrency:USD"
    }
    try:
        r = requests.get(EBAY_BROWSE_URL, headers=headers, params=params, timeout=20)
        if r.status_code == 200:
            data = r.json()
            return data.get("itemSummaries", [])
        else:
            post_to_hub(f"eBay search failed ({r.status_code}): {r.text[:200]}", "error")
            return []
    except Exception as e:
        post_to_hub(f"eBay search exception: {e}", "error")
        return []

def get_item_details(item_id, app_id):
    """Fetch full item details (including description) from Browse API."""
    if not app_id:
        return None
    headers = {
        "X-EBAY-C-MARKETPLACE-ID": "EBAY-US",
        "Authorization": f"Bearer {app_id}",
        "Accept": "application/json"
    }
    try:
        r = requests.get(EBAY_ITEM_URL.format(item_id=item_id), headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        else:
            return None
    except Exception:
        return None

def extract_gemstone_info(title, description, item_specifics=None):
    """
    Naively estimate carat weight and gem type from text.
    Returns (gem_type, est_carats) or (None, None).
    """
    text = (title + " " + (description or "")).lower()
    # Look for carat patterns: "1.5 ct", "1.50ct", "1 carat", etc.
    carat_match = re.search(r"(\d+\.?\d*)\s*(?:ct|c[ .]?t[ .]?|carat)", text)
    carats = float(carat_match.group(1)) if carat_match else None
    gem_type = None
    for gem in GEM_RETAIL_PER_CARAT:
        if gem in text:
            gem_type = gem
            break
    # Item specifics may contain "Gemstone" -> "Diamond", "Metal" -> ...
    if item_specifics:
        for spec in item_specifics:
            if spec.get("name", "").lower() == "gemstone":
                val = spec.get("value", "").lower()
                for gem in GEM_RETAIL_PER_CARAT:
                    if gem in val:
                        gem_type = gem
                        break
    return gem_type, carats

def estimate_retail_value(title, description, item_specifics=None):
    """Rough retail price for jewellery, or None if cannot guess."""
    gem, carats = extract_gemstone_info(title, description, item_specifics)
    if gem and carats and carats > 0:
        per_carat = GEM_RETAIL_PER_CARAT.get(gem, 200)
        return per_carat * carats
    return None

# ── OAuth & Bidding ─────────────────────────────────────────────────────────
def get_oauth_token(config):
    """Exchange refresh token for access token (eBay OAuth). Returns token or None."""
    oa = config.get("ebay_oauth", {})
    client_id = oa.get("client_id")
    client_secret = oa.get("client_secret")
    refresh_token = oa.get("refresh_token")
    if not (client_id and client_secret and refresh_token):
        return None
    try:
        r = requests.post("https://api.ebay.com/identity/v1/oauth2/token",
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          data={
                              "grant_type": "refresh_token",
                              "refresh_token": refresh_token,
                              "scope": "https://api.ebay.com/oauth/api_scope"
                          },
                          auth=(client_id, client_secret),
                          timeout=10)
        if r.status_code == 200:
            return r.json()["access_token"]
        else:
            post_to_hub(f"OAuth token refresh failed: {r.text[:200]}", "error")
            return None
    except Exception as e:
        post_to_hub(f"OAuth exception: {e}", "error")
        return None

def place_bid(access_token, item_id, bid_amount, end_time_iso):
    """
    Place a bid using the eBay Trading API (PlaceOffer).
    Requires item.Site (country), end time, etc. We hardcode site US.
    """
    if not access_token:
        return False
    # Trading API endpoint
    url = "https://api.ebay.com/ws/api.dll"
    headers = {
        "X-EBAY-API-SITEID": "0",   # US
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-CALL-NAME": "PlaceOffer",
        "X-EBAY-API-IAF-TOKEN": access_token,
        "Content-Type": "text/xml"
    }
    # Construct XML body
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<PlaceOfferRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ItemID>{item_id}</ItemID>
  <Offer>
    <Action>Bid</Action>
    <Quantity>1</Quantity>
    <MaxBid currencyID="USD">{bid_amount}</MaxBid>
  </Offer>
</PlaceOfferRequest>"""
    try:
        r = requests.post(url, headers=headers, data=xml, timeout=10)
        if r.status_code == 200 and "Success" in r.text:
            return True
        else:
            post_to_hub(f"Bid failed: {r.text[:300]}", "error")
            return False
    except Exception as e:
        post_to_hub(f"Bid exception: {e}", "error")
        return False

# ── Order management ────────────────────────────────────────────────────────
def load_orders():
    if ORDERS_FILE.exists():
        with open(ORDERS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_orders(orders):
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders, f, indent=2)

# ── Web interface (Flask) ───────────────────────────────────────────────────
# We'll start a Flask server in a thread. The client can view deals and pay.
from flask import Flask, render_template_string, request, redirect, url_for

app_flask = Flask(__name__)

# We'll need access to the global deal list and config
deals_cache = []
config_global = {}
orders_global = {}

HTML_DEAL_LIST = """
<!DOCTYPE html>
<html>
<head>
  <title>Jewellery Arbitrage – Deals</title>
  <style>
    body { font-family: Arial; max-width: 800px; margin: 20px auto; background: #f5f0eb; }
    .deal { background: #fff; padding: 15px; margin: 12px 0; border-radius: 6px; box-shadow: 0 2px 6px rgba(0,0,0,0.1); }
    .deal h3 { margin: 0; color: #6b4e3d; }
    .price { color: #c00; font-weight: bold; }
    .retail { color: #060; }
    button { background: #6b4e3d; color: white; border: none; padding: 8px 16px; cursor: pointer; border-radius: 4px; }
    .paid { background: #d4edda; border-color: #c3e6cb; }
    .info { font-size: 0.9em; color: #555; }
  </style>
</head>
<body>
<h1>💎 Live eBay Arbitration Deals</h1>
<p>Click "Buy & Pay" to reserve this item. You will be charged the current bid price + $50 broker fee. We will snipe for you.</p>
{% for deal in deals %}
<div class="deal {% if deal.item_id in orders %}paid{% endif %}">
  <h3>{{ deal.title }}</h3>
  <div class="info">Ends: {{ deal.end_time }} | ID: {{ deal.item_id }}</div>
  <div>Current bid: <span class="price">${{ deal.current_bid }}</span> 
       Est. Retail: <span class="retail">${{ deal.est_retail }}</span></div>
  {% if deal.item_id in orders %}
    <p><strong>PAID – Snipe scheduled</strong></p>
  {% else %}
    <form method="POST" action="{{ url_for('pay') }}">
      <input type="hidden" name="item_id" value="{{ deal.item_id }}">
      <button type="submit">Buy & Pay (${{ deal.current_bid + 50 }})</button>
    </form>
  {% endif %}
</div>
{% else %}
<p>No deals at the moment. Refresh later.</p>
{% endfor %}
</body>
</html>
"""

@app_flask.route("/")
def list_deals():
    # Refresh cache from the main thread's global list
    return render_template_string(HTML_DEAL_LIST, deals=deals_cache, orders=orders_global)

@app_flask.route("/pay", methods=["POST"])
def pay():
    item_id = request.form.get("item_id")
    if not item_id:
        return redirect(url_for("list_deals"))
    # Find deal in cache
    deal = next((d for d in deals_cache if d["item_id"] == item_id), None)
    if not deal:
        return "Deal not found", 404
    # In production you would charge the client. Here we simulate immediate payment.
    # Record order
    orders_global[item_id] = {
        "paid": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bid_amount": deal["current_bid"] + 1.0,   # we will bid current bid + increment
        "end_time": deal["end_time"],
        "title": deal["title"],
        "broker_fee": 50.0
    }
    save_orders(orders_global)
    post_to_hub(
        f"💰 Order placed for {deal['title']} (item {item_id}). Will snipe at ${orders_global[item_id]['bid_amount']:.2f}.",
        "warning",
        {"item_id": item_id, "bid_amount": orders_global[item_id]["bid_amount"]}
    )
    return redirect(url_for("list_deals"))

def start_web_interface(port):
    threading.Thread(target=lambda: app_flask.run(host="127.0.0.1", port=port, debug=False, use_reloader=False), daemon=True).start()
    webbrowser.open(f"http://localhost:{port}")

# ── Main scanning logic ────────────────────────────────────────────────────
def scan(config, state, orders):
    app_id = config.get("ebay_app_id", "").strip()
    if not app_id:
        post_to_hub("eBay App ID not set. Please add it to the config file.", "error")
        return []

    queries = config.get("search_queries", ["gemstone ring"])
    max_price = config.get("max_price", 300)
    min_markup = config.get("min_estimated_retail_markup", 1.5)
    new_deals = []

    for query in queries:
        items = ebay_search(query, app_id, max_price, max_results=5)
        for item in items:
            item_id = item.get("itemId")
            if item_id in state.get("seen_items", []):
                continue

            # Get full item details for description and specifics
            details = get_item_details(item_id, app_id)
            title = item.get("title", "")
            description = ""
            specifics = None
            if details:
                description = details.get("description", "")
                specifics = details.get("localizedAspects")
            # Estimate retail
            est_retail = estimate_retail_value(title, description, specifics)
            if est_retail is None:
                # try to use item price and see if it's cheap
                # If we can't estimate, skip
                continue

            current_bid = item.get("currentBidPrice", {}).get("value", 0)
            if not current_bid:
                current_bid = item.get("price", {}).get("value", 0)
            if current_bid <= 0:
                continue

            markup = est_retail / current_bid if current_bid > 0 else 999
            if markup < min_markup:
                continue

            end_time_str = item.get("itemEndDate", "")
            end_time_dt = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
            # Skip if auction ends in less than a minute (too late to snipe)
            if end_time_dt < datetime.now(timezone.utc) + timedelta(seconds=60):
                continue

            deal = {
                "item_id": item_id,
                "title": title,
                "url": item.get("itemWebUrl", f"https://www.ebay.com/itm/{item_id}"),
                "current_bid": current_bid,
                "est_retail": round(est_retail, 2),
                "end_time": end_time_str,
                "end_time_dt": end_time_dt.isoformat(),
                "markup": round(markup, 2),
                "query": query
            }
            new_deals.append(deal)
            state.setdefault("seen_items", []).append(item_id)

    # Update global cache for the web interface
    global deals_cache
    deals_cache = new_deals

    # Post each new deal to hub
    for d in new_deals:
        if d["item_id"] not in state.get("posted_deals", []):
            state.setdefault("posted_deals", []).append(d["item_id"])
            post_to_hub(
                f"💎 Deal: {d['title'][:80]} – Bid ${d['current_bid']:.0f}, Retail ${d['est_retail']:.0f} (x{d['markup']})",
                "info",
                d
            )
    # Save state
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    # ── Snipe scheduler ─────────────────────────────────────────────────────
    # For orders that are pending and auction is about to end
    access_token = get_oauth_token(config)   # may be None if OAuth not set
    for item_id, order in list(orders.items()):
        if not order.get("paid"):
            continue
        end_str = order.get("end_time")
        if not end_str:
            continue
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except:
            continue
        now = datetime.now(timezone.utc)
        seconds_left = (end_dt - now).total_seconds()
        bid_seconds = config.get("bid_seconds_before_end", 3)
        if seconds_left <= bid_seconds and seconds_left > -5:
            # Snipe!
            bid_amount = order.get("bid_amount", order.get("current_bid", 0) + 1.0)
            if access_token:
                success = place_bid(access_token, item_id, bid_amount, end_str)
                if success:
                    post_to_hub(
                        f"🎯 Snipe placed! Bid ${bid_amount:.2f} on item {item_id} with {seconds_left:.0f}s left.",
                        "warning",
                        {"item_id": item_id, "bid_amount": bid_amount}
                    )
                    # Remove order to prevent multiple bids
                    del orders[item_id]
                    save_orders(orders)
                else:
                    post_to_hub(
                        f"❌ Failed to snipe item {item_id}. OAuth may be invalid.",
                        "error"
                    )
            else:
                # No OAuth token; just alert
                post_to_hub(
                    f"⏰ Snipe time for {item_id} but OAuth not configured. Manual bid needed!",
                    "error"
                )
                # Don't remove order so we keep trying (or user can manually cancel)

    return new_deals

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            "Config file created. Add your eBay App ID (and OAuth for bidding). Then restart.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    state = {}
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

    global orders_global
    orders_global = load_orders()

    post_to_hub("💎 Jewellery Arbitrage Bot online – scanning eBay for underpriced gems.", "info")

    # Start web interface for client orders
    web_port = config.get("web_port", 5058)
    start_web_interface(web_port)

    interval = config.get("scan_interval_minutes", 15) * 60
    while True:
        try:
            scan(config, state, orders_global)
        except Exception as e:
            post_to_hub(f"Scan error: {e}", "error")
        time.sleep(interval)

if __name__ == "__main__":
    main()
