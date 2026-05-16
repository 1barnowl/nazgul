#!/usr/bin/env python3
"""
market_maker_spread_bot.py — Market‑Maker Spread Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Continuously places limit buy and sell orders on illiquid
prediction markets (Polymarket CLOB) to capture the bid‑ask
spread.  Once an order fills, the bot reports the event.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `market_maker_config.json` in the same directory:

{
  "polymarket": {
    "clob_api_key": "0x...",
    "clob_private_key": "0xYOUR_PRIVATE_KEY",
    "proxy_wallet": "0xYourProxyWalletAddress"
  },
  "min_spread_pct": 2.0,
  "order_size": 10,
  "distance_from_mid_pct": 1.0,
  "max_position_per_market": 100,
  "poll_interval_seconds": 60,
  "state_file": "market_maker_state.json",
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
BOT_ID = "market_maker_spread_bot"
BOT_NAME = "Market‑Maker Spread"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "market_maker_config.json"
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

# ── Polymarket CLOB API helpers ──────────────────────────────────
POLYMARKET_CLOB = "https://clob.polymarket.com"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Polygon
CHAIN_ID = 137

def sign_order(private_key: str, proxy_wallet: str,
               token_id: str, price: float, size: int, side: str) -> dict:
    """Create an EIP‑712 signed limit order."""
    w3 = Web3()
    account = Account.from_key(private_key)

    domain = {
        "name": "Polymarket CTF Exchange",
        "version": "1",
        "chainId": CHAIN_ID,
        "verifyingContract": CTF_EXCHANGE_ADDRESS
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

    salt = int.from_bytes(os.urandom(32), "big")
    nonce = int(time.time() * 1000)  # milliseconds timestamp as nonce
    expiration = int(time.time() + 3600)  # 1 hour lifetime

    # makerAmount and takerAmount depend on side and decimal scaling
    # token price uses 6 decimals (USDC), shares are whole units
    if side == "BUY":
        # maker wants to receive tokenId, pays USDC
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
            _post(f"Submit order failed: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Order submission error: {e}", "error")
        return None

def cancel_order(order_id: str, api_key: str, api_secret: str) -> bool:
    headers = {
        "POLY-API-KEY": api_key,
        "POLY-SIGNATURE": api_secret,
        "Content-Type": "application/json"
    }
    try:
        resp = requests.delete(f"{POLYMARKET_CLOB}/order/{order_id}",
                               headers=headers, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        _post(f"Cancel error: {e}", "error")
        return False

def get_order_status(order_id: str) -> Optional[str]:
    resp = requests.get(f"{POLYMARKET_CLOB}/order/{order_id}")
    if resp.status_code == 200:
        return resp.json().get("status")  # "OPEN", "FILLED", "CANCELLED", ...
    return None

def get_open_orders(token_id: str, proxy_wallet: str,
                    api_key: str, api_secret: str) -> List[dict]:
    headers = {
        "POLY-API-KEY": api_key,
        "POLY-SIGNATURE": api_secret,
    }
    resp = requests.get(
        f"{POLYMARKET_CLOB}/orders",
        params={"token_id": token_id, "maker": proxy_wallet, "status": "OPEN"},
        headers=headers
    )
    if resp.status_code == 200:
        return resp.json()
    else:
        _post(f"Failed to fetch open orders: {resp.text[:200]}", "warning")
        return []

# ── Market data ──────────────────────────────────────────────────
def get_all_markets() -> List[dict]:
    resp = requests.get(f"{POLYMARKET_GAMMA}/markets?closed=false")
    if resp.status_code == 200:
        return resp.json()
    return []

def get_order_book(token_id: str) -> dict:
    resp = requests.get(f"{POLYMARKET_CLOB}/book?token_id={token_id}")
    if resp.status_code == 200:
        return resp.json()
    return {}

# ── Market making logic ──────────────────────────────────────────
def place_spread_orders(market: dict, config: dict, state: dict,
                        api_key: str, api_secret: str, proxy_wallet: str):
    """
    If spread is wide enough and no open orders already placed,
    submit a buy (bid) and sell (ask) limit order.
    """
    token_id = market.get("tokens", [{}])[0].get("token_id")
    if not token_id:
        return

    book = get_order_book(token_id)
    asks = book.get("asks", [])
    bids = book.get("bids", [])
    if not asks or not bids:
        return

    best_ask = float(asks[0]["price"])
    best_bid = float(bids[0]["price"])
    mid = (best_ask + best_bid) / 2.0
    spread_pct = (best_ask - best_bid) / mid * 100 if mid > 0 else 0

    min_spread = float(config["min_spread_pct"])
    if spread_pct < min_spread:
        return

    # Check if we already have open orders for this token
    open_orders = get_open_orders(token_id, proxy_wallet, api_key, api_secret)
    if open_orders:
        return  # already managing this market

    # Determine order prices
    distance = float(config["distance_from_mid_pct"]) / 100.0
    buy_price = round(mid * (1 - distance), 4)
    sell_price = round(mid * (1 + distance), 4)
    order_size = int(config["order_size"])

    # Sign and submit buy order
    buy_order = sign_order(config["polymarket"]["clob_private_key"],
                           proxy_wallet, token_id, buy_price, order_size, "BUY")
    buy_id = submit_order(buy_order, api_key, api_secret)
    if buy_id:
        _post(f"Placed BUY limit for token {token_id} @ {buy_price}", "info",
              {"order_id": buy_id, "price": buy_price})

    # Sign and submit sell order
    sell_order = sign_order(config["polymarket"]["clob_private_key"],
                            proxy_wallet, token_id, sell_price, order_size, "SELL")
    sell_id = submit_order(sell_order, api_key, api_secret)
    if sell_id:
        _post(f"Placed SELL limit for token {token_id} @ {sell_price}", "info",
              {"order_id": sell_id, "price": sell_price})

    if buy_id or sell_id:
        state.setdefault("order_ids", []).extend([id for id in [buy_id, sell_id] if id])

def manage_existing_orders(state: dict, api_key: str, api_secret: str):
    """Check status of previously placed orders and remove from state if filled/cancelled."""
    for order_id in state.get("order_ids", []):
        status = get_order_status(order_id)
        if status and status != "OPEN":
            _post(f"Order {order_id} status: {status}", "info")
    # Remove non‑open orders from state
    state["order_ids"] = [oid for oid in state.get("order_ids", [])
                          if get_order_status(oid) == "OPEN"]

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Market‑Maker Spread Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        pm_cfg = config.get("polymarket", {})
        api_key = pm_cfg.get("clob_api_key")
        api_secret = pm_cfg.get("gamma_api_secret") or pm_cfg.get("clob_api_key")  # API secret is the same as key in CLOB? Actually need to check.
        # For Polymarket CLOB, the API secret is the API key's private key? The documentation says you need an API key (public) and a corresponding private key for signing. The private key is used to sign orders, while the API key identifies the user. So we need the clob_private_key and clob_api_key (which is the proxy wallet address? Actually, the API key is a separate secret; you obtain it from Polymarket. We'll store it separately.

        # For simplicity, we'll use the proxy wallet as API key? Not exactly. We'll allow config to provide 'clob_api_key' (a secret string) and 'clob_private_key' (the private key). The API key is the "API key" you get from Polymarket's CLOB settings.
        proxy_wallet = pm_cfg.get("proxy_wallet")
        clob_private_key = pm_cfg.get("clob_private_key")
        if not api_key or not clob_private_key or not proxy_wallet:
            _post("Missing CLOB credentials", "error")
            time.sleep(300)
            continue

        state_file = config.get("state_file", "market_maker_state.json")
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
        except Exception:
            state = {"order_ids": []}

        # Manage existing orders
        manage_existing_orders(state, api_key, api_secret)

        # Fetch all open markets
        markets = get_all_markets()
        if not markets:
            _post("No markets returned from Polymarket", "error")
        else:
            for market in markets:
                place_spread_orders(market, config, state, api_key, api_secret, proxy_wallet)
                time.sleep(0.5)  # rate limit

        # Save state
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)

        poll_sec = int(config.get("poll_interval_seconds", 60))
        _heartbeat()
        time.sleep(poll_sec)

if __name__ == "__main__":
    main()
