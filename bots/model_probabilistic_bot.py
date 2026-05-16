#!/usr/bin/env python3
"""
model_probabilistic_bot.py — Model‑Based Probabilistic Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Builds a probability model from historical polls / economic
indicators (e.g., FiveThirtyEight polls), compares its
estimate with Polymarket odds, and bets only when there is
a significant disagreement.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests numpy scipy web3

Configuration
─────────────
Place `model_probabilistic_config.json` in the same directory:

{
  "model": {
    "source": "fivethirtyeight_polls",
    "data_url": "https://raw.githubusercontent.com/fivethirtyeight/data/master/polls/2024/president_polls.csv",
    "candidate": "Biden",               // candidate name as it appears in the "answer" column
    "poll_weight_days": 30,             // exponential decay in days
    "min_polls": 5,
    "polling_error_std": 0.03           // historical polling error (~3%)
  },
  "betting": {
    "polymarket": {
      "clob_api_key": "0x...",
      "clob_private_key": "0xPRIVATE_KEY",
      "proxy_wallet": "0xPROXY_WALLET_ADDRESS"
    },
    "market_search_term": "Joe Biden win 2024 presidential election",
    "max_bet_usd": 100.0,
    "min_probability_diff": 0.05        // bet only if |p_model - p_market| > this
  },
  "state_file": "model_probabilistic_state.json",
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
from typing import Optional

import numpy as np
import requests
from scipy.stats import norm
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "model_probabilistic_bot"
BOT_NAME = "Model‑Based Probabilistic"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "model_probabilistic_config.json"
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

# ── State management ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Poll data processing ─────────────────────────────────────────
def fetch_polls_csv(url: str) -> list:
    """Download polls CSV and return list of dicts."""
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        # The CSV might have a BOM; use utf-8-sig
        reader = csv.DictReader(io.StringIO(resp.text))
        return [row for row in reader]
    except Exception as e:
        _post(f"Error fetching polls: {e}", "error")
        return []

def aggregate_polls(polls: list, candidate: str, config: dict) -> Optional[float]:
    """
    Compute weighted poll average for a specific candidate using
    exponential decay by date and sample size.
    Returns the polling proportion (0-1) or None if not enough polls.
    """
    now = datetime.now(timezone.utc)
    decay_days = float(config.get("poll_weight_days", 30))
    min_polls = int(config.get("min_polls", 5))
    candidate_lower = candidate.strip().lower()

    weights = []
    values = []
    for poll in polls:
        # Fields: question_id, poll_id, start_date, end_date, answer, sample_size, party, ...
        try:
            end_date_str = poll.get("end_date")
            sample_size_str = poll.get("sample_size")
            answer = poll.get("answer", "")
            if not end_date_str or not sample_size_str:
                continue
            # Check if answer matches candidate
            if answer.strip().lower() != candidate_lower:
                continue
            end_date = datetime.strptime(end_date_str, "%m/%d/%y").replace(tzinfo=timezone.utc)
            days_ago = (now - end_date).days
            if days_ago < 0:
                days_ago = 0
            sample_size = int(sample_size_str)
            # Weight: sample_size * exp(-days_ago/decay_days)
            weight = sample_size * math.exp(-days_ago / decay_days)
            # Polling percentage: the "pct" column (0-100)
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
    """
    Convert polling average for a candidate to win probability
    using a normal CDF.  For a two-candidate race, the probability
    is norm.cdf(lead / std_error) where lead = poll_avg - 0.5.
    """
    lead = poll_avg - 0.5
    if std_error <= 0:
        return 0.5
    return norm.cdf(lead / std_error)

# ── Polymarket integration ───────────────────────────────────────
POLYMARKET_CLOB = "https://clob.polymarket.com"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

def find_polymarket_market(search_term: str) -> Optional[dict]:
    """Find a Polymarket market matching the search term."""
    resp = requests.get(f"{POLYMARKET_GAMMA}/markets", params={
        "search": search_term,
        "closed": "false",
        "limit": 3
    })
    if resp.status_code == 200:
        markets = resp.json()
        if markets:
            return markets[0]
    return None

def get_market_price(token_id: str) -> Optional[float]:
    """Get mid‑price (avg of best bid/ask) for a token."""
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
    """Create an EIP‑712 signed limit order for Polymarket CLOB."""
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

def place_order(signed_order: dict, api_key: str, api_secret: str) -> Optional[str]:
    headers = {
        "POLY-API-KEY": api_key,
        "POLY-SIGNATURE": api_secret,
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(f"{POLYMARKET_CLOB}/order", json=signed_order, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("id")
        else:
            _post(f"Order error: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Order submission error: {e}", "error")
        return None

def execute_bet(market: dict, direction: str, max_bet_usd: float,
                api_key: str, private_key: str, proxy_wallet: str) -> bool:
    tokens = market.get("tokens", [])
    token_id = None
    for t in tokens:
        if direction == "buy_yes" and t.get("outcome") == "Yes":
            token_id = t["token_id"]
        elif direction == "buy_no" and t.get("outcome") == "No":
            token_id = t["token_id"]
    if not token_id:
        _post(f"No matching token for {direction}", "error")
        return False

    price = get_market_price(token_id)
    if price is None:
        _post("Cannot get market price", "error")
        return False

    size = int(max_bet_usd / price) if price > 0 else 1
    if size < 1:
        size = 1

    signed = sign_order(private_key, proxy_wallet, token_id, price, size, "BUY")
    order_id = place_order(signed, api_key, api_key)  # using api_key as secret
    if order_id:
        _post(f"Bet placed: {direction} token {token_id}, size {size}, price {price}, order {order_id}", "error")
        return True
    return False

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Model‑Based Probabilistic Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        model_cfg = config.get("model", {})
        betting_cfg = config.get("betting", {})
        state_file = config.get("state_file", "model_probabilistic_state.json")
        state = load_state(state_file)

        # Step 1: Compute model probability
        polls = fetch_polls_csv(model_cfg.get("data_url", ""))
        if not polls:
            _post("No polls data fetched", "error")
            time.sleep(3600)
            continue

        candidate = model_cfg.get("candidate", "Biden")
        poll_avg = aggregate_polls(polls, candidate, model_cfg)
        if poll_avg is None:
            _post("Insufficient polling data", "warning")
            time.sleep(3600)
            continue

        std_error = float(model_cfg.get("polling_error_std", 0.03))
        model_prob = poll_average_to_probability(poll_avg, std_error)
        _post(f"Model probability for {candidate}: {model_prob:.4f} (poll avg {poll_avg:.3f})",
              "info", {"candidate": candidate, "model_prob": model_prob, "poll_avg": poll_avg})

        # Step 2: Get market price from Polymarket
        market_search = betting_cfg.get("market_search_term", "")
        market = find_polymarket_market(market_search)
        if not market:
            _post(f"No Polymarket market found for '{market_search}'", "warning")
        else:
            # Price of YES token
            yes_token = next((t for t in market["tokens"] if t["outcome"] == "Yes"), None)
            if yes_token:
                market_price = get_market_price(yes_token["token_id"])
                if market_price is None:
                    _post("Could not retrieve market price", "warning")
                else:
                    diff = model_prob - market_price
                    _post(f"Market price: {market_price:.4f}, diff: {diff:.4f}",
                          "info", {"market_price": market_price, "diff": diff})

                    min_diff = float(betting_cfg.get("min_probability_diff", 0.05))
                    if abs(diff) >= min_diff:
                        direction = "buy_yes" if diff > 0 else "buy_no"
                        pm_cfg = betting_cfg.get("polymarket", {})
                        api_key = pm_cfg.get("clob_api_key")
                        private_key = pm_cfg.get("clob_private_key")
                        proxy_wallet = pm_cfg.get("proxy_wallet")
                        if api_key and private_key and proxy_wallet:
                            execute_bet(market, direction,
                                        float(betting_cfg.get("max_bet_usd", 100.0)),
                                        api_key, private_key, proxy_wallet)
                        else:
                            _post("Polymarket credentials missing", "error")
                    else:
                        _post("No significant disagreement; no bet", "info")

        # Save state (can store last bet time etc.)
        save_state(state_file, state)

        poll_interval_h = float(config.get("poll_interval_hours", 6))
        _heartbeat()
        time.sleep(poll_interval_h * 3600)

if __name__ == "__main__":
    main()
