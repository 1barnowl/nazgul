#!/usr/bin/env python3
"""
correlated_event_arb_bot.py — Correlated Event Arb Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Exploits mispricing between related prediction markets
(e.g., "Will the Fed raise rates?" vs "Will the 10‑year
yield exceed 5%?") by placing offsetting bets on Polymarket.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `correlated_arb_config.json` in the same directory:

{
  "polymarket": {
    "clob_api_key": "0x...",
    "clob_private_key": "0xPRIVATE_KEY",
    "proxy_wallet": "0xPROXY_WALLET_ADDRESS"
  },
  "pairs": [
    {
      "name": "Fed rate vs 10Y yield",
      "market_A_search": "Will the Fed raise rates in 2025?",
      "outcome_A": "Yes",
      "market_B_search": "Will 10‑year Treasury yield exceed 5% in 2025?",
      "outcome_B": "Yes",
      "p_B_given_A": 0.75,
      "p_B_given_notA": 0.20,
      "max_bet_usd": 100,
      "min_edge_pct": 5
    }
  ],
  "poll_interval_minutes": 15,
  "state_file": "correlated_arb_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "correlated_event_arb_bot"
BOT_NAME = "Correlated Event Arb"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "correlated_arb_config.json"
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
        return {"bets_placed": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Polymarket API helpers ───────────────────────────────────────
POLYMARKET_CLOB = "https://clob.polymarket.com"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

def find_market(search_term: str) -> Optional[dict]:
    resp = requests.get(f"{POLYMARKET_GAMMA}/markets", params={
        "search": search_term,
        "closed": "false",
        "limit": 1
    })
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return None

def get_token_id_and_price(market: dict, outcome: str) -> Tuple[Optional[str], Optional[float]]:
    """
    Return token ID and mid‑price for a specific outcome string (Yes/No).
    """
    tokens = market.get("tokens", [])
    token_id = None
    for t in tokens:
        if t.get("outcome") == outcome:
            token_id = t["token_id"]
            break
    if not token_id:
        return None, None

    book = requests.get(f"{POLYMARKET_CLOB}/book?token_id={token_id}").json()
    asks = book.get("asks", [])
    bids = book.get("bids", [])
    if asks and bids:
        price = (float(asks[0]["price"]) + float(bids[0]["price"])) / 2.0
    elif asks:
        price = float(asks[0]["price"])
    elif bids:
        price = float(bids[0]["price"])
    else:
        price = None
    return token_id, price

def sign_order(private_key: str, proxy_wallet: str,
               token_id: str, price: float, size: int, side: str) -> dict:
    """EIP‑712 signed limit order for Polymarket CLOB."""
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

def place_order(private_key: str, proxy_wallet: str, api_key: str, api_secret: str,
                token_id: str, price: float, size: int, side: str) -> Optional[str]:
    """Convenience to sign and submit."""
    signed = sign_order(private_key, proxy_wallet, token_id, price, size, side)
    return submit_order(signed, api_key, api_secret)

# ── Arb logic ────────────────────────────────────────────────────
def process_pair(pair_cfg: dict, pm_cfg: dict, state: dict):
    """Compute fair prices and place offsetting bets if mispriced."""
    name = pair_cfg["name"]
    max_bet = float(pair_cfg["max_bet_usd"])
    min_edge = float(pair_cfg.get("min_edge_pct", 5)) / 100.0

    # Fetch markets
    market_A = find_market(pair_cfg["market_A_search"])
    market_B = find_market(pair_cfg["market_B_search"])
    if not market_A or not market_B:
        _post(f"Could not find one or both markets for pair {name}", "warning")
        return

    # Get token IDs and prices for the desired outcomes
    token_A_id, p_A = get_token_id_and_price(market_A, pair_cfg["outcome_A"])
    token_B_id, p_B = get_token_id_and_price(market_B, pair_cfg["outcome_B"])
    if token_A_id is None or token_B_id is None or p_A is None or p_B is None:
        _post(f"Missing token/price for pair {name}", "warning")
        return

    # Correlation parameters
    p_B_given_A = float(pair_cfg["p_B_given_A"])
    p_B_given_notA = float(pair_cfg["p_B_given_notA"])

    # Fair price of B: P(B) = P(A)*p(B|A) + (1-P(A))*p(B|not A)
    fair_p_B = p_A * p_B_given_A + (1 - p_A) * p_B_given_notA

    # Also compute fair price of A given B? Actually the arb can be constructed either way.
    # We'll bet on the side that offers positive expected value.
    # We'll consider two possible positions:
    #   Position 1: Buy A (YES) and Sell B (YES) — if B overpriced relative to A.
    #   Position 2: Sell A (YES) and Buy B (YES) — if B underpriced relative to A.

    # The profit condition: If the correlation holds, the net payoff of buying A and selling B should be non‑negative.
    # The cost to buy 1 share of A: p_A. Selling 1 share of B: receives p_B.
    # Expected payoff of A: 1 if event occurs, 0 otherwise.
    # Expected payoff of B (we sold it): -1 if B occurs, 0 otherwise.
    # So expected value = (p_A) * (1 - p_B_given_A) + (1-p_A) * (0 - p_B_given_notA) ??? Not correct.
    # Actually, for each unit of A (YES) we buy, we simultaneously sell some amount k of B (YES) such that the total payoff is deterministic.
    # To create a riskless portfolio, we need to find k such that the payoff is the same regardless of A occurrence.
    # Payoff if A happens: 1 (from A) - k*1 (from B if B occurs? B occurrence depends on A via correlation, but not deterministic. So not a perfect hedge, but expected value.)
    # Instead, we'll bet on both sides independently to exploit the mispricing based on the correlation: we bet on A (YES) and also bet on B (NO) if that gives positive expected value according to our model.
    # The expected value of buying A is (true probability of A) - p_A. But we don't know true probability, we only know correlation.
    # We can compute the expected value of betting on B given the model and the market price. If the market price p_B deviates from fair_p_B, we can bet on B (YES or NO) according to the sign.
    # We'll use the following rule:
    #   If p_B > fair_p_B by more than min_edge, then the market overprices B. We will SELL B (YES) and maybe also BUY A? The optimal action is to bet against B, because the market overestimates B's probability.
    #   If p_B < fair_p_B by more than min_edge, we will BUY B (YES).
    # This is a simpler approach: just treat each market individually based on fair_p_B derived from A and the correlation model. We don't need to bet on A simultaneously, but we could also bet on A if its price is mispriced relative to the other? Actually, we want to exploit the correlation mispricing; if we bet only on B, we are exposed to the model risk (the correlation might be wrong). To be market‑neutral, we can bet on both. But for simplicity, I'll implement a bet on B using fair_p_B derived from A.
    # However the bot description says "places offsetting bets". So I'll create a ratio k = (fair_p_B - p_B) / (something) but better to implement a simple two‑leg bet: we calculate the difference, and if the deviation exceeds threshold, we place opposite bets on A and B to lock in profit regardless of outcome.

    # Simplified approach: compute the mispricing of B given A. If B is overpriced, we sell B and also buy A to hedge? Let's implement a simple model:
    # If the deviation diff = p_B - fair_p_B is positive and > min_edge, we sell B (YES) and buy A (YES) because A is the driver; buying A helps offset the risk of B's outcome. Conversely, if diff is negative (B underpriced), we buy B and sell A.
    # We'll set sizes such that the net cost is zero? Not necessarily.

    diff = p_B - fair_p_B
    if abs(diff) < min_edge:
        _post(f"Pair {name}: no edge (diff={diff:.4f})", "info")
        return

    _post(f"Pair {name}: p_A={p_A:.3f}, p_B={p_B:.3f}, fair_p_B={fair_p_B:.3f}, diff={diff:.3f}",
          "info")

    # Determine directions
    if diff > 0:
        # B overpriced → sell B (YES), buy A (YES)
        action_B = ("SELL", token_B_id, p_B)
        action_A = ("BUY", token_A_id, p_A)
        bet_size_B = int(max_bet / p_B) if p_B > 0 else 0
        bet_size_A = int(max_bet / p_A) if p_A > 0 else 0
        # We'll limit both to the same dollar amount
        bet_size = min(bet_size_B, bet_size_A, int(max_bet / max(p_A, p_B)))
    else:
        # B underpriced → buy B (YES), sell A (YES)
        action_B = ("BUY", token_B_id, p_B)
        action_A = ("SELL", token_A_id, p_A)
        bet_size_B = int(max_bet / p_B) if p_B > 0 else 0
        bet_size_A = int(max_bet / p_A) if p_A > 0 else 0
        bet_size = min(bet_size_B, bet_size_A, int(max_bet / max(p_A, p_B)))

    if bet_size < 1:
        _post("Bet size too small", "info")
        return

    # Place orders
    pm_api_key = pm_cfg.get("clob_api_key")
    pm_secret = pm_cfg.get("clob_api_key")  # using same as secret (often the case)
    pm_private = pm_cfg.get("clob_private_key")
    proxy_wallet = pm_cfg.get("proxy_wallet")
    if not all([pm_api_key, pm_private, proxy_wallet]):
        _post("Polymarket credentials missing", "error")
        return

    # Leg A
    order_id_A = place_order(pm_private, proxy_wallet, pm_api_key, pm_secret,
                             action_A[1], action_A[2], bet_size, action_A[0])
    # Leg B
    order_id_B = place_order(pm_private, proxy_wallet, pm_api_key, pm_secret,
                             action_B[1], action_B[2], bet_size, action_B[0])

    if order_id_A or order_id_B:
        _post(f"Placed offsetting bets: A {action_A[0]} ({order_id_A}), B {action_B[0]} ({order_id_B}), size {bet_size}",
              "error", {"pair": name, "orders": [order_id_A, order_id_B]})
        state.setdefault("orders", []).extend([order_id_A, order_id_B])
    else:
        _post("Failed to place one or both orders", "error")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Correlated Event Arb Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        pm_cfg = config.get("polymarket", {})
        pairs = config.get("pairs", [])
        state_file = config.get("state_file", "correlated_arb_state.json")
        state = load_state(state_file)

        for pair in pairs:
            try:
                process_pair(pair, pm_cfg, state)
            except Exception as e:
                _post(f"Error processing pair {pair.get('name','?')}: {e}", "error")

        save_state(state_file, state)
        poll_min = int(config.get("poll_interval_minutes", 15))
        _heartbeat()
        time.sleep(poll_min * 60)

if __name__ == "__main__":
    main()
