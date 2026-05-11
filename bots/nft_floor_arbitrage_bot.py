#!/usr/bin/env python3
"""
nft_floor_arbitrage_bot.py — NFT Floor Sweep Arbitrage Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors listings on OpenSea, Blur, and X2Y2 for NFTs that are
priced significantly below the cross‑market floor. Alerts when
a profitable flip is possible.

Optionally, if you supply a wallet private key and a transaction
module, the bot can attempt to buy and relist automatically.

SETUP
─────
1. Install dependencies:
      pip install requests web3

2. (Optional) Set environment variables:
      OS_API_KEY     = your OpenSea API key
      BLUR_API_KEY   = if required
      X2Y2_API_KEY   = if required
      WALLET_PK      = private key for automated buying

3. Attach to BotController.
"""

import os
import json
import time
import requests
from decimal import Decimal
from web3 import Web3
from eth_account import Account
from eth_account.signers.local import LocalAccount

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "nft_floor_arbitrage_bot"
BOT_NAME = "NFT Floor Arb Bot"

HEARTBEAT_INTERVAL = 20
_last_hb = 0.0

# ── Hub helpers ────────────────────────────────────────────────────────────────
def _post(summary, level="info", payload=None):
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID, "bot_name": BOT_NAME,
            "summary": summary, "level": level, "payload": payload or {}
        }, timeout=5)
    except Exception:
        pass

def _heartbeat():
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    _last_hb = time.time()
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME, "status": "online"
        }, timeout=3)
    except Exception:
        pass

def _wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Configuration ──────────────────────────────────────────────────────────────
OPENSEA_API_KEY = os.getenv("OS_API_KEY", "")
BLUR_API_KEY    = os.getenv("BLUR_API_KEY", "")
X2Y2_API_KEY    = os.getenv("X2Y2_API_KEY", "")
WALLET_PRIVATE  = os.getenv("WALLET_PK", "")

# A list of NFT collection slugs or contract addresses to monitor (e.g. "boredapeyachtclub")
COLLECTIONS = [
    "boredapeyachtclub",
    "mutant-ape-yacht-club",
    "azuki",
    "clonex",
    "pudgypenguins"
]

MIN_PRICE_DIFF_PCT = 5.0   # alert if price is X% below estimated true floor
SCAN_INTERVAL = 60          # seconds

# ── Price fetching (real APIs) ─────────────────────────────────────────────────
OPENSEA_API = "https://api.opensea.io/api/v2"

def get_opensea_floor(collection_slug):
    """Get the lowest listed price (in ETH) for a collection on OpenSea."""
    headers = {"Accept": "application/json"}
    if OPENSEA_API_KEY:
        headers["X-API-KEY"] = OPENSEA_API_KEY
    url = f"{OPENSEA_API}/listings/{collection_slug}/best?limit=1"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "listings" in data and len(data["listings"]) > 0:
                listing = data["listings"][0]
                price_wei = int(listing["price"]["current"]["value"])
                decimals = int(listing["price"]["current"]["decimals"])
                price_eth = price_wei / 10**decimals
                return price_eth
    except Exception as e:
        _post(f"OpenSea fetch error: {e}", "warning")
    return None

def get_blur_floor(collection_slug):
    """Fetch floor from Blur's public API (unofficial but widely used)."""
    # Blur doesn't have a simple public REST endpoint; we can use the OpenSea shared API or a community endpoint.
    # Using a known Blur unofficial API (subject to change). We'll attempt to get lowest listing.
    try:
        url = f"https://core-api.prod.blur.io/v1/collections/{collection_slug}/stats"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # Extract floor price in ETH (might be in wei string)
            floor = float(data.get("floorPrice", 0))
            if floor > 0:
                return floor
    except:
        pass
    return None

def get_x2y2_floor(collection_slug):
    """Fetch floor from X2Y2 API."""
    try:
        url = f"https://api.x2y2.org/v1/collections/{collection_slug}/stats"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            floor = data.get("floor_price", 0)
            if floor > 0:
                return float(floor)
    except:
        pass
    return None

# ── True floor calculation ────────────────────────────────────────────────────
def estimate_true_floor(prices):
    """
    Given a list of floor prices from different markets, estimate the
    'true' market floor by taking the **second lowest** to avoid being
    skewed by a single outlier.
    """
    valid = [p for p in prices if p is not None and p > 0]
    if len(valid) == 0:
        return None
    valid.sort()
    if len(valid) == 1:
        return valid[0]
    # If the lowest is significantly lower than the second, it might be the arbitrage item itself.
    # We'll use the second lowest as the true market price.
    return valid[1]

# ── Arbitrage detection ────────────────────────────────────────────────────────
def scan_collections():
    for slug in COLLECTIONS:
        os_price = get_opensea_floor(slug)
        blur_price = get_blur_floor(slug)
        x2y2_price = get_x2y2_floor(slug)
        prices = [os_price, blur_price, x2y2_price]
        true_floor = estimate_true_floor(prices)
        if true_floor is None:
            continue

        # Determine if any individual market is significantly below the true floor
        markets = {"OpenSea": os_price, "Blur": blur_price, "X2Y2": x2y2_price}
        for market, price in markets.items():
            if price is None:
                continue
            diff_pct = ((true_floor - price) / true_floor) * 100
            if diff_pct >= MIN_PRICE_DIFF_PCT:
                payload = {
                    "collection": slug,
                    "market": market,
                    "listing_price": round(price, 4),
                    "estimated_true_floor": round(true_floor, 4),
                    "diff_pct": round(diff_pct, 2)
                }
                _post(
                    f"🔥 ARB: {slug} on {market} at {price:.4f} ETH vs floor {true_floor:.4f} ETH ({diff_pct:.1f}% below)",
                    "error" if diff_pct > 10 else "warning",
                    payload
                )

    _heartbeat()

# ── Optional automated execution ──────────────────────────────────────────────
def execute_arb(market, collection_slug, token_id, buy_price):
    """
    Placeholder for actual on‑chain purchase and relist.
    If you set WALLET_PRIVATE and have the required marketplace contracts,
    the bot will try to submit a transaction. Currently not implemented
    due to platform‑specific complexity.
    """
    if not WALLET_PRIVATE:
        _post("Execution skipped: no wallet private key set.", "info")
        return
    # Example (pseudocode):
    # account = Account.from_key(WALLET_PRIVATE)
    # tx = construct_buy_tx(market, collection_slug, token_id, buy_price)
    # signed = account.sign_transaction(tx)
    # w3.eth.send_raw_transaction(signed.rawTransaction)
    _post(f"Execution stub: would buy {collection_slug} #{token_id} on {market} for {buy_price} ETH.", "info")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    _post("NFT Floor Arbitrage Scanner online. Monitoring cross‑market floors.", "info")

    while True:
        scan_collections()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
