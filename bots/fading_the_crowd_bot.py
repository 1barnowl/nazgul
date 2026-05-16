#!/usr/bin/env python3
"""
fading_the_crowd_bot.py — Fading‑the‑Crowd Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Identifies markets where crowd bias pushed the price too far
from an objective reference (e.g., polls, economic data) and
bets heavily against the crowd.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests numpy scipy web3

Configuration
─────────────
Place `fading_the_crowd_config.json` in the same directory:

{
  "reference": {
    "source": "fivethirtyeight_polls",
    "data_url": "https://raw.githubusercontent.com/fivethirtyeight/data/master/polls/2024/president_polls.csv",
    "candidate": "Biden",
    "poll_weight_days": 30,
    "min_polls": 5,
    "polling_error_std": 0.03
  },
  "betting": {
    "polymarket": {
      "clob_api_key": "0x...",
      "clob_private_key": "0xPRIVATE_KEY",
      "proxy_wallet": "0xPROXY_WALLET_ADDRESS"
    },
    "markets": [
      {
        "search_term": "Joe Biden win 2024 presidential election",
        "max_bet_usd": 100.0,
        "fade_threshold": 0.08
      }
    ]
  },
  "state_file": "fading_the_crowd_state.json",
  "poll_interval_hours": 6,
  "heartbeat_interval": 30
}
"""

import csv
import io
import json
import os
import time
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List

import numpy as np
import requests
from scipy.stats import norm
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "fading_the_crowd_bot"
BOT_NAME = "Fading‑the‑Crowd"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "fading_the_crowd_config.json"
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
        return {}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Poll aggregation ─────────────────────────────────────────────
def fetch_polls_csv(url: str) -> list:
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        return [row for row in reader]
    except Exception as e:
        _post(f"Error fetching polls: {e}", "error")
        return []

def aggregate_polls(polls: list, candidate: str, config: dict) -> Optional[float]:
    """Compute weighted poll average for a specific candidate."""
    now = datetime.now(timezone.utc)
    decay_days = float(config.get("poll_weight_days", 30))
    min_polls = int(config.get("min_polls", 5))
    candidate_lower = candidate.strip().lower()

    weights = []
    values = []
    for poll in polls:
        try:
            end_date_str = poll.get("end_date")
            sample_size_str = poll.get("sample_size")
            answer = poll.get("answer", "")
            if not end_date_str or not sample_size_str:
                continue
            if answer.strip().lower() != candidate_lower:
                continue
            end_date = datetime.strptime(end_date_str, "%m/%d/%y").replace(tzinfo=timezone.utc)
            days_ago = (now - end_date).days
            if days_ago < 0:
                days_ago = 0
            sample_size = int(sample_size_str)
            weight = sample_size * math.exp(-days_ago / decay_days)
            pct_str = poll.get("pct")
            if pct_str is None:
                continue
            pct = float(pct_str) / 100.0
            weights.append(weight)
            values.append(pct)
        except (ValueError, KeyError):
            continue

    if len(values) < min_polls:
        return None

    total_weight = sum(weights)
    if total_weight == 0:
        return None
    avg = sum(v * w for v, w in zip(values, weights)) / total_weight
    return avg

def poll_average_to_probability(poll_avg: float, std_error: float) -> float:
    """Convert polling average to win probability (normal CDF)."""
    lead = poll_avg - 0.5
    if std_error <= 0:
        return 0.5
    return norm.cdf(lead / std_error)

# ── Polymarket integration ───────────────────────────────────────
POLYMARKET_CLOB = "https://clob.polymarket.com"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

def find_polymarket_market(search_term: str) -> Optional[dict]:
    resp = requests.get(f"{POLYMARKET_GAMMA}/markets", params={
        "search": search_term,
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
    """Sign an EIP‑712 order for Polymarket CLOB."""
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
    expiration = int(time.time() + 3600)

    if side == "BUY":
        maker_amount = int(size * price * 1e6)
        taker_amount = int(size * 1e6)
    else:  # SELL
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

# ── Bet execution ────────────────────────────────────────────────
def execute_fade_bet(reference_prob: float, market: dict, market_cfg: dict,
                     pm_cfg: dict, state: dict):
    """Place bet against the crowd based on reference vs market price."""
    search_term = market_cfg.get("search_term", "")
    fade_threshold = float(market_cfg.get("fade_threshold", 0.08))
    max_bet = float(market_cfg.get("max_bet_usd", 100.0))

    # Get the YES token (always the primary token)
    tokens = market.get("tokens", [])
    yes_token_id = None
    for t in tokens:
        if t.get("outcome") == "Yes":
            yes_token_id = t["token_id"]
            break
    if not yes_token_id:
        _post(f"No YES token in market {search_term}", "error")
        return

    market_price = get_market_price(yes_token_id)
    if market_price is None:
        _post(f"Could not get market price for {search_term}", "warning")
        return

    diff = reference_prob - market_price
    if abs(diff) < fade_threshold:
        _post(f"No fade signal: ref {reference_prob:.3f}, market {market_price:.3f}, diff {diff:.3f}",
              "info")
        return

    # Determine direction: if market is overpriced, sell YES; if underpriced, buy YES
    if diff < 0:  # market > reference → overpriced
        direction = "sell_yes"   # sell YES token
        side = "SELL"
        token_id = yes_token_id
        expected_profit = market_price - reference_prob
    else:         # market < reference → underpriced
        direction = "buy_yes"
        side = "BUY"
        token_id = yes_token_id
        expected_profit = reference_prob - market_price

    _post(f"Fade signal: {direction} for {search_term}. "
          f"Ref {reference_prob:.3f}, Market {market_price:.3f}, "
          f"expected edge {expected_profit:.3f}",
          "warning", {"direction": direction, "ref": reference_prob, "market": market_price})

    # Execute bet
    pm_api_key = pm_cfg.get("clob_api_key")
    pm_private_key = pm_cfg.get("clob_private_key")
    proxy_wallet = pm_cfg.get("proxy_wallet")
    if not pm_api_key or not pm_private_key or not proxy_wallet:
        _post("Polymarket credentials missing", "error")
        return

    price = market_price  # we'll take the last price; more advanced: place limit order around mid
    size = int(max_bet / price) if price > 0 else 0
    if size < 1:
        size = 1

    signed = sign_order(pm_private_key, proxy_wallet, token_id, price, size, side)
    order_id = submit_order(signed, pm_api_key, pm_api_key)  # using api_key as secret
    if order_id:
        _post(f"Fade bet placed: order {order_id}", "error", {"order_id": order_id, "direction": direction, "size": size, "price": price})
        # Record bet in state (optional)
        state.setdefault("orders", []).append(order_id)

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Fading‑the‑Crowd Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        ref_cfg = config.get("reference", {})
        markets = config.get("betting", {}).get("markets", [])
        pm_cfg = config.get("betting", {}).get("polymarket", {})
        state_file = config.get("state_file", "fading_the_crowd_state.json")
        state = load_state(state_file)

        # Compute reference probability (one source for all markets for simplicity)
        ref_prob = None
        if ref_cfg.get("source") == "fivethirtyeight_polls":
            polls = fetch_polls_csv(ref_cfg.get("data_url", ""))
            if polls:
                poll_avg = aggregate_polls(polls, ref_cfg.get("candidate", "Biden"), ref_cfg)
                if poll_avg is not None:
                    std_error = float(ref_cfg.get("polling_error_std", 0.03))
                    ref_prob = poll_average_to_probability(poll_avg, std_error)
                    _post(f"Reference probability: {ref_prob:.3f} (poll avg {poll_avg:.3f})",
                          "info", {"poll_avg": poll_avg, "ref_prob": ref_prob})
                else:
                    _post("Insufficient poll data for reference", "warning")
            else:
                _post("Failed to fetch polls", "error")
        else:
            _post(f"Unsupported reference source: {ref_cfg.get('source')}", "error")
            time.sleep(3600)
            continue

        if ref_prob is None:
            _post("No reference probability computed, skipping fade check", "warning")
        else:
            for market_cfg in markets:
                search_term = market_cfg.get("search_term", "")
                market = find_polymarket_market(search_term)
                if not market:
                    _post(f"No Polymarket market found for '{search_term}'", "warning")
                    continue
                execute_fade_bet(ref_prob, market, market_cfg, pm_cfg, state)

        save_state(state_file, state)
        poll_hours = float(config.get("poll_interval_hours", 6))
        _heartbeat()
        time.sleep(poll_hours * 3600)

if __name__ == "__main__":
    main()
