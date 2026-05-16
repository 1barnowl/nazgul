#!/usr/bin/env python3
"""
mispricing_correction_bot.py — Mispricing Correction Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans for stale prediction markets where the probability
implied by the price doesn’t reflect new information
(from news headlines). Bets on the correction and exits
when the price adjusts.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `mispricing_correction_config.json` in the same directory:

{
  "news": {
    "api_key": "YOUR_NEWSAPI_KEY",
    "keywords": ["bitcoin", "ethereum", "SEC", "Fed"],
    "freshness_minutes": 15,
    "sentiment_impact_factor": 0.3
  },
  "polymarket": {
    "clob_api_key": "0x...",
    "clob_private_key": "0xPRIVATE_KEY",
    "proxy_wallet": "0xPROXY_WALLET_ADDRESS"
  },
  "betting": {
    "max_bet_usd": 50,
    "min_mispricing": 0.05,
    "exit_price_tolerance": 0.02,
    "max_holding_minutes": 30,
    "dry_run": true
  },
  "state_file": "mispricing_correction_state.json",
  "poll_interval_seconds": 30,
  "heartbeat_interval": 30
}
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "mispricing_correction_bot"
BOT_NAME = "Mispricing Correction"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "mispricing_correction_config.json"
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

# ── State persistence ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"open_positions": {}}  # key = market question, value = bet details

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── News fetching and sentiment ──────────────────────────────────
NEWSAPI_URL = "https://newsapi.org/v2/everything"

def fetch_recent_news(api_key: str, keywords: list, freshness_minutes: int) -> List[dict]:
    """Fetch news articles from the last `freshness_minutes`."""
    from datetime import timezone
    to_time = datetime.now(timezone.utc)
    from_time = to_time - timedelta(minutes=freshness_minutes)
    query = " OR ".join(keywords)
    articles = []
    try:
        resp = requests.get(NEWSAPI_URL, params={
            "apiKey": api_key,
            "q": query,
            "from": from_time.isoformat(),
            "to": to_time.isoformat(),
            "language": "en",
            "pageSize": 20,
        }, timeout=10)
        if resp.status_code == 200:
            articles = resp.json().get("articles", [])
        else:
            _post(f"NewsAPI error: {resp.status_code}", "warning")
    except Exception as e:
        _post(f"NewsAPI request error: {e}", "error")
    return articles

POSITIVE_WORDS = ["bullish", "buy", "moon", "pump", "green", "rally", "win", "good", "positive", "up", "approval", "approved"]
NEGATIVE_WORDS = ["bearish", "sell", "dump", "crash", "red", "fear", "loss", "bad", "negative", "down", "rejection", "rejected"]

def compute_sentiment(articles: List[dict]) -> float:
    """Average sentiment score from -1 (negative) to +1 (positive)."""
    if not articles:
        return 0.0
    scores = []
    for art in articles:
        text = (art.get("title", "") + " " + art.get("description", "")).lower()
        pos = sum(1 for w in POSITIVE_WORDS if w in text)
        neg = sum(1 for w in NEGATIVE_WORDS if w in text)
        if pos + neg > 0:
            scores.append((pos - neg) / (pos + neg))
        else:
            scores.append(0.0)
    if scores:
        return sum(scores) / len(scores)
    return 0.0

# ── Polymarket API helpers ───────────────────────────────────────
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"

def find_market(keyword: str) -> Optional[dict]:
    resp = requests.get(f"{POLYMARKET_GAMMA}/markets", params={
        "search": keyword,
        "closed": "false",
        "limit": 1
    })
    if resp.status_code == 200:
        markets = resp.json()
        if markets:
            return markets[0]
    return None

def get_market_price(token_id: str) -> Optional[float]:
    book = requests.get(f"{POLYMARKET_CLOB}/book?token_id={token_id}").json()
    asks = book.get("asks", [])
    bids = book.get("bids", [])
    if asks and bids:
        return (float(asks[0]["price"]) + float(bids[0]["price"])) / 2.0
    if asks:
        return float(asks[0]["price"])
    if bids:
        return float(bids[0]["price"])
    return None

def sign_order(private_key: str, proxy_wallet: str,
               token_id: str, price: float, size: int, side: str) -> dict:
    domain = {
        "name": "Polymarket CTF Exchange",
        "version": "1",
        "chainId": 137,
        "verifyingContract": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    }
    types = {
        "Order": [
            {"name": "salt", "type": "uint256"},
            {"name": "maker", "type": "address"},
            {"name": "signer", "type": "address"},
            {"name": "taker", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
            {"name": "makerAmount", "type": "uint256"},
            {"name": "takerAmount", "type": "uint256"},
            {"name": "expiration", "type": "uint256"},
            {"name": "nonce", "type": "uint256"},
            {"name": "feeRateBps", "type": "uint256"},
            {"name": "side", "type": "uint8"},
            {"name": "signatureType", "type": "uint8"}
        ]
    }
    account = Account.from_key(private_key)
    salt = int.from_bytes(os.urandom(32), "big")
    nonce = int(time.time() * 1000)
    expiration = int(time.time()) + 3600

    if side == "BUY":
        maker_amount = int(size * price * 1e6)
        taker_amount = int(size * 1e6)
    else:
        maker_amount = int(size * 1e6)
        taker_amount = int(size * price * 1e6)

    order = {
        "salt": salt,
        "maker": proxy_wallet,
        "signer": proxy_wallet,
        "taker": "0x0000000000000000000000000000000000000000",
        "tokenId": int(token_id),
        "makerAmount": maker_amount,
        "takerAmount": taker_amount,
        "expiration": expiration,
        "nonce": nonce,
        "feeRateBps": 0,
        "side": 0 if side == "BUY" else 1,
        "signatureType": 0
    }
    encoded = encode_typed_data(full_message={
        "domain": domain,
        "types": types,
        "primaryType": "Order",
        "message": order
    })
    signed = account.sign_message(encoded)
    order["signature"] = signed.signature.hex()
    return order

def submit_order(signed_order: dict, api_key: str, api_secret: str) -> Optional[str]:
    headers = {
        "POLY-API-KEY": api_key,
        "POLY-SIGNATURE": api_secret,
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(f"{POLYMARKET_CLOB}/order", json=signed_order,
                             headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("id")
        else:
            _post(f"Order error: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Order submission error: {e}", "error")
        return None

def place_bet(private_key, proxy_wallet, api_key, api_secret,
              token_id, price, size, side) -> Optional[str]:
    signed = sign_order(private_key, proxy_wallet, token_id, price, size, side)
    return submit_order(signed, api_key, api_secret)

def cancel_order(order_id, api_key):
    headers = {"POLY-API-KEY": api_key}
    requests.delete(f"{POLYMARKET_CLOB}/order/{order_id}", headers=headers)

# ── Main logic ───────────────────────────────────────────────────
def process_cycle(config: dict, state: dict):
    news_cfg = config["news"]
    betting_cfg = config["betting"]
    pm_cfg = config["polymarket"]

    # 1. Fetch news and compute sentiment
    articles = fetch_recent_news(news_cfg["api_key"], news_cfg["keywords"],
                                 news_cfg["freshness_minutes"])
    sentiment = compute_sentiment(articles)
    if not articles:
        _post("No recent news", "info")
        return

    # Map sentiment to a fair probability: baseline 0.5 + sentiment * impact
    fair_prob = 0.5 + sentiment * news_cfg["sentiment_impact_factor"]
    fair_prob = max(0.0, min(1.0, fair_prob))  # clamp

    _post(f"News sentiment: {sentiment:.3f}, fair prob: {fair_prob:.3f}", "info")

    # 2. Find a relevant market (using first keyword as search)
    keyword = news_cfg["keywords"][0]
    market = find_market(keyword)
    if not market:
        _post("No matching market found", "info")
        return

    market_question = market.get("question", "")
    tokens = market.get("tokens", [])
    # Find the YES token and its price
    yes_token_id = None
    for t in tokens:
        if t.get("outcome") == "Yes":
            yes_token_id = t["token_id"]
            break
    if not yes_token_id:
        return
    current_price = get_market_price(yes_token_id)
    if current_price is None:
        return

    _post(f"Market '{market_question}' price: {current_price:.4f}", "info")

    # 3. Manage open positions
    open_pos = state.get("open_positions", {})
    if market_question in open_pos:
        pos = open_pos[market_question]
        # Check if price has corrected
        entry_price = pos["entry_price"]
        direction = pos["direction"]  # "buy_yes" or "buy_no"
        target_price = pos["fair_price_at_entry"]
        exit_tolerance = betting_cfg["exit_price_tolerance"]
        max_holding = int(betting_cfg["max_holding_minutes"])

        # Determine if we should exit
        should_exit = False
        if direction == "buy_yes":
            # Expect price to rise towards fair_prob
            if current_price >= target_price - exit_tolerance:
                should_exit = True
        else:  # buy_no (we bought NO token)
            # Expect price of NO token to rise (i.e., YES price to fall)
            no_price = 1.0 - current_price
            if no_price >= (1.0 - target_price) - exit_tolerance:  # equivalent to YES price <= target + tol
                should_exit = True

        # Also exit if holding time exceeded
        entry_time = datetime.fromisoformat(pos["entry_time"])
        if datetime.now(timezone.utc) - entry_time > timedelta(minutes=max_holding):
            _post(f"Position time limit exceeded for {market_question}, exiting", "warning")
            should_exit = True

        if should_exit:
            # Cancel any remaining orders and close position
            for oid in pos.get("order_ids", []):
                cancel_order(oid, pm_cfg["clob_api_key"])
            _post(f"Exiting position on {market_question} at price {current_price:.4f}", "info")
            del open_pos[market_question]
            state["open_positions"] = open_pos
        return

    # 4. Check for new mispricing opportunity
    min_mispricing = float(betting_cfg["min_mispricing"])
    deviation = fair_prob - current_price
    if abs(deviation) < min_mispricing:
        return

    # Determine bet direction
    if deviation > 0:
        direction = "buy_yes"
        token_id = yes_token_id
        bet_price = current_price  # we'll place limit around market price
    else:
        direction = "buy_no"
        # Need NO token ID
        no_token_id = None
        for t in tokens:
            if t.get("outcome") == "No":
                no_token_id = t["token_id"]
                break
        if not no_token_id:
            return
        token_id = no_token_id
        bet_price = 1.0 - current_price  # approximate; better to query NO order book but we'll assume symmetric

    # Place bet
    max_bet_usd = float(betting_cfg["max_bet_usd"])
    size = int(max_bet_usd / bet_price) if bet_price > 0 else 0
    if size < 1:
        return

    api_key = pm_cfg["clob_api_key"]
    private_key = pm_cfg["clob_private_key"]
    proxy_wallet = pm_cfg["proxy_wallet"]
    dry_run = betting_cfg.get("dry_run", True)

    if dry_run:
        _post(f"[DRY] Would bet {size} shares on {direction} for '{market_question}' at {bet_price:.4f}", "info")
        # record position to prevent repeated bets
        state.setdefault("open_positions", {})[market_question] = {
            "direction": direction,
            "entry_price": bet_price,
            "fair_price_at_entry": fair_prob,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "order_ids": []
        }
        return

    order_id = place_bet(private_key, proxy_wallet, api_key, api_key,
                         token_id, bet_price, size, "BUY")
    if order_id:
        _post(f"Bet placed: {direction} order {order_id}", "error", {"market": market_question, "order_id": order_id})
        state.setdefault("open_positions", {})[market_question] = {
            "direction": direction,
            "entry_price": bet_price,
            "fair_price_at_entry": fair_prob,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "order_ids": [order_id]
        }
    else:
        _post("Failed to place order", "error")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Mispricing Correction Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "mispricing_correction_state.json")
        state = load_state(state_file)

        process_cycle(config, state)
        save_state(state_file, state)

        poll_sec = int(config.get("poll_interval_seconds", 30))
        _heartbeat()
        time.sleep(poll_sec)

if __name__ == "__main__":
    main()
