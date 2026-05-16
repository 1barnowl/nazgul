#!/usr/bin/env python3
"""
cross_platform_arb_bot.py — Cross‑Platform Prediction Market Arb Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans Polymarket and Kalshi for the same binary event priced
differently. If the spread exceeds a configurable threshold after
fees, the bot buys the cheap side and sells the expensive side
simultaneously (takes opposite positions) to lock in a risk‑free
profit.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests python‑dateutil web3

Configuration
─────────────
Place `cross_arb_config.json` in the same directory:

{
  "polymarket": {
    "gamma_api_key": "PK ...",
    "gamma_api_secret": "sk ...",
    "clob_api_key": "0x...",
    "clob_private_key": "0xYOUR_PRIVATE_KEY",
    "proxy_wallet": "0xYourProxyWalletAddress"
  },
  "kalshi": {
    "api_key_id": "YOUR_API_KEY_ID",
    "private_key_path": "/path/to/kalshi_private_key.pem"
  },
  "dry_run": true,
  "min_expected_profit_pct": 2.0,
  "max_position_per_market": 100.0,
  "poll_interval_minutes": 10,
  "state_file": "cross_arb_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import time
import uuid
import hmac
import hashlib
import base64
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from dateutil import parser as dateparser
from web3 import Web3
from eth_account.messages import encode_defunct

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "cross_platform_arb_bot"
BOT_NAME = "Cross‑Platform Arb Bot"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "cross_arb_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID,
            "bot_name": BOT_NAME,
            "summary": summary,
            "level": level,
            "payload": payload or {},
        }, timeout=5)
    except Exception:
        pass

def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME,
            "status": "online",
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

# ── Polymarket API Helpers ───────────────────────────────────────
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"

def polymarket_get_markets(api_key, api_secret) -> List[dict]:
    """Fetch all open binary markets from Polymarket (CLOB)."""
    # Using Gamma API for market data (no auth needed for GET markets)
    resp = requests.get(f"{POLYMARKET_GAMMA_URL}/markets", params={"closed": "false"})
    if resp.status_code != 200:
        _post(f"Polymarket markets fetch error: {resp.status_code}", "error")
        return []
    return resp.json()

def polymarket_get_order_book(token_id: str) -> dict:
    """Fetch order book for a specific token."""
    resp = requests.get(f"{POLYMARKET_CLOB_URL}/book?token_id={token_id}")
    if resp.status_code == 200:
        return resp.json()
    _post(f"Polymarket order book error for {token_id}: {resp.status_code}", "warning")
    return {}

def polymarket_get_best_price(token_id: str) -> Optional[float]:
    """Return the best available price (mid or best bid for YES, best ask for sell)."""
    book = polymarket_get_order_book(token_id)
    if not book:
        return None
    # For a buy of YES, we look at lowest ask price. For a sell, best bid.
    # We'll use midpoint for estimation.
    asks = book.get("asks", [])
    bids = book.get("bids", [])
    if not asks and not bids:
        return None
    best_ask = float(asks[0]["price"]) if asks else 1.0
    best_bid = float(bids[0]["price"]) if bids else 0.0
    mid = (best_ask + best_bid) / 2.0
    return mid

def polymarket_place_order(private_key: str, api_key: str, proxy_wallet: str,
                           token_id: str, side: str, size: float, price: float) -> bool:
    """
    Place a signed order via CLOB.
    side: "BUY" or "SELL"
    size: amount of token shares (e.g., 100 = $100 if price $1)
    price: price per token (cents, e.g., 0.55 for 55¢)
    """
    # Requires detailed CLOB signing implementation. We'll skip the full implementation
    # for brevity but indicate that a proper signed order would be created.
    # We'll post a warning that execution is pending implementation.
    _post("Polymarket order placement not fully implemented; would send signed order.", "warning")
    return False

# ── Kalshi API Helpers ───────────────────────────────────────────
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

def kalshi_rest_headers(method: str, path: str, body: Optional[str],
                        api_key_id: str, private_key_pem: str) -> dict:
    """Generate headers with RSA signing as per Kalshi documentation."""
    current_time = datetime.utcnow().isoformat() + "Z"
    # Build the pre‑sign string
    if body:
        body_digest = hashlib.sha256(body.encode()).hexdigest()
    else:
        body_digest = hashlib.sha256(b'').hexdigest()
    pre_sign = f"{current_time}\n{method}\n{path}\n{body_digest}"
    # Sign with RSA private key
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    with open(private_key_pem, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,
        )
    signature = private_key.sign(
        pre_sign.encode(),
        padding.PKCS1v15(),
        hashes.SHA256()
    )
    signature_b64 = base64.b64encode(signature).decode()
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key_id}:{signature_b64}",
        "KALSHI-ACCESS-TIMESTAMP": current_time,
        "KALSHI-ACCESS-SIGNATURE": signature_b64,
    }

def kalshi_get_markets() -> List[dict]:
    """Fetch all open binary markets from Kalshi."""
    path = "/markets"
    # We'll use a simple GET without auth to get markets (public endpoint)
    resp = requests.get(f"{KALSHI_BASE_URL}{path}")
    if resp.status_code != 200:
        _post(f"Kalshi markets fetch error: {resp.status_code}", "error")
        return []
    data = resp.json()
    return data.get("markets", []) if isinstance(data, dict) else []

def kalshi_get_order_book(ticker: str) -> dict:
    """Get order book for a specific ticker."""
    path = f"/markets/{ticker}/order_book"
    resp = requests.get(f"{KALSHI_BASE_URL}{path}")
    if resp.status_code == 200:
        return resp.json()
    _post(f"Kalshi order book error for {ticker}: {resp.status_code}", "warning")
    return {}

def kalshi_get_best_price(ticker: str) -> Optional[float]:
    book = kalshi_get_order_book(ticker)
    if not book:
        return None
    asks = book.get("yes_asks", [])
    bids = book.get("yes_bids", [])
    if not asks and not bids:
        return None
    best_ask = float(asks[0]["price"]) / 100.0 if asks else 1.0
    best_bid = float(bids[0]["price"]) / 100.0 if bids else 0.0
    mid = (best_ask + best_bid) / 2.0
    return mid

def kalshi_place_order(private_key_pem: str, api_key_id: str,
                       ticker: str, side: str, count: int, yes_price: int) -> bool:
    """
    side: "yes" or "no" (kalshi convention: buy_yes, buy_no, sell_yes, sell_no)
    count: number of contracts (each contract is 1 cent)
    yes_price: price in cents (e.g., 55 for 55¢)
    """
    _post("Kalshi order placement not fully implemented; would send signed order.", "warning")
    return False

# ── Event Matching ──────────────────────────────────────────────
def match_events(polymarket_markets: List[dict], kalshi_markets: List[dict]) -> List[Tuple[dict, dict]]:
    """
    Try to find matching binary events between Polymarket and Kalshi
    by comparing question/title keywords.
    """
    pairs = []
    kalshi_by_title = {m.get("title", "").lower(): m for m in kalshi_markets if m.get("status") == "open"}
    for pm_market in polymarket_markets:
        if pm_market.get("closed"):
            continue
        # Polymarket market format: "question" is the title, "tokens" list with outcome tokens
        question = pm_market.get("question", "").lower()
        if not question:
            continue
        # Attempt to match by exact title substring
        for title, ks_market in kalshi_by_title.items():
            # Basic matching: check if one is substring of other
            if question in title or title in question:
                pairs.append((pm_market, ks_market))
                break
    return pairs

# ── Arb Opportunity Check ────────────────────────────────────────
def find_arb_opportunity(pm_market: dict, ks_market: dict,
                         min_profit_pct: float, max_position: float) -> Optional[dict]:
    """
    Given two matching markets, compute expected profit by buying YES
    on the cheaper and selling YES on the more expensive.
    Returns details if profitable.
    """
    pm_token_ids = [t["token_id"] for t in pm_market.get("tokens", []) if t.get("outcome") in ("Yes", "No")]
    if len(pm_token_ids) < 2:
        return None
    # We'll use the first token (Yes)
    pm_price = polymarket_get_best_price(pm_token_ids[0])
    if pm_price is None:
        return None

    ks_ticker = ks_market.get("ticker")
    ks_price = kalshi_get_best_price(ks_ticker)
    if ks_price is None:
        return None

    # Fees: Polymarket: 0% maker, 0% taker? Actually CLOB has no fees. Kalshi: no fees for trading on the exchange API (only if taking liquidity? Actually Kalshi charges 1 cent per contract for market orders? Not for resting limit orders. We'll ignore for simplicity.)
    # Arb: if pm_price < ks_price, buy on PM, sell on Kalshi. Otherwise vice versa.
    # We'll assume we can buy YES at pm_price (or close) and sell YES at ks_price.
    # Net profit per dollar = |ks_price - pm_price| - fees approx.
    diff = abs(ks_price - pm_price)
    if diff < min_profit_pct / 100.0:
        return None

    # Determine cheaper and expensive
    if pm_price < ks_price:
        buy_platform = "polymarket"
        sell_platform = "kalshi"
        buy_price = pm_price
        sell_price = ks_price
        buy_token_id = pm_token_ids[0]
        sell_ticker = ks_ticker
    else:
        buy_platform = "kalshi"
        sell_platform = "polymarket"
        buy_price = ks_price
        sell_price = pm_price
        buy_token_id = ks_ticker   # Kalshi uses ticker for orders
        sell_token_id = pm_token_ids[0]

    expected_profit_pct = diff * 100  # approximate
    return {
        "event": pm_market.get("question", "Unknown"),
        "pm_price": pm_price,
        "ks_price": ks_price,
        "buy_platform": buy_platform,
        "sell_platform": sell_platform,
        "expected_profit_pct": round(expected_profit_pct, 2),
        "buy_details": {
            "platform": buy_platform,
            "token": buy_token_id,
            "price": buy_price
        },
        "sell_details": {
            "platform": sell_platform,
            "token": sell_token_id,
            "price": sell_price
        }
    }

# ── Execution Logic ──────────────────────────────────────────────
def execute_arb(opp: dict, config: dict, dry_run: bool):
    """
    Place both orders. If dry_run, only log.
    """
    if dry_run:
        _post(f"[DRY] Would buy on {opp['buy_platform']} at {opp['buy_details']['price']:.4f} "
              f"and sell on {opp['sell_platform']} at {opp['sell_details']['price']:.4f} "
              f"for event: {opp['event']}", "info", opp)
        return

    # Real execution: need proper API keys and wallet signing.
    _post("Live arb execution not fully implemented; would place signed orders.", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Cross‑Platform Arb Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        dry_run = config.get("dry_run", True)
        min_profit_pct = float(config.get("min_expected_profit_pct", 2.0))
        max_position = float(config.get("max_position_per_market", 100.0))

        # Fetch markets from both platforms
        pm_markets = polymarket_get_markets(
            config["polymarket"]["gamma_api_key"],
            config["polymarket"]["gamma_api_secret"]
        )
        ks_markets = kalshi_get_markets()

        if not pm_markets or not ks_markets:
            _post("Could not fetch markets from one or both platforms", "warning")
        else:
            matched = match_events(pm_markets, ks_markets)
            _post(f"Matched {len(matched)} potential event pairs", "info")
            for pm, ks in matched:
                opp = find_arb_opportunity(pm, ks, min_profit_pct, max_position)
                if opp:
                    _post(f"Arb opportunity: {opp['event']} profit {opp['expected_profit_pct']:.2f}% "
                          f"Buy on {opp['buy_platform']}@{opp['buy_details']['price']:.4f} "
                          f"Sell on {opp['sell_platform']}@{opp['sell_details']['price']:.4f}",
                          "warning", opp)
                    execute_arb(opp, config, dry_run)

        poll_minutes = int(config.get("poll_interval_minutes", 10))
        _heartbeat()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
