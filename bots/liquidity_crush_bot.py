#!/usr/bin/env python3
"""
liquidity_crush_bot.py — Liquidity Crush Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Finds low‑liquidity prediction markets on Polymarket
where a small buy or sell can drastically move the odds,
then reverses to profit from the reversion.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `liquidity_crush_config.json` in the same directory:

{
  "polymarket": {
    "clob_api_key": "0x...",
    "clob_private_key": "0xPRIVATE_KEY",
    "proxy_wallet": "0xPROXY_WALLET_ADDRESS"
  },
  "trading": {
    "min_liquidity_depth": 50,
    "max_order_size_usd": 20,
    "min_target_profit_pct": 2.0,
    "max_slippage_pct": 1.0,
    "position_timeout_minutes": 10,
    "dry_run": true
  },
  "state_file": "liquidity_crush_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 5
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
BOT_ID = "liquidity_crush_bot"
BOT_NAME = "Liquidity Crush"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "liquidity_crush_config.json"
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
        return {"active_positions": {}}  # token_id -> {entry_order, exit_order, start_time, size, price}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Polymarket API helpers ───────────────────────────────────────
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"

def get_all_markets() -> List[dict]:
    resp = requests.get(f"{POLYMARKET_GAMMA}/markets?closed=false")
    if resp.status_code == 200:
        return resp.json()
    else:
        _post(f"Gamma API error: {resp.status_code}", "error")
        return []

def get_order_book(token_id: str) -> dict:
    resp = requests.get(f"{POLYMARKET_CLOB}/book?token_id={token_id}")
    if resp.status_code == 200:
        return resp.json()
    else:
        return {"asks": [], "bids": []}

def get_my_orders(api_key, proxy_wallet):
    headers = {"POLY-API-KEY": api_key}
    resp = requests.get(f"{POLYMARKET_CLOB}/orders?maker={proxy_wallet}&status=OPEN", headers=headers)
    if resp.status_code == 200:
        return resp.json()
    return []

def cancel_order(order_id: str, api_key: str) -> bool:
    headers = {"POLY-API-KEY": api_key}
    resp = requests.delete(f"{POLYMARKET_CLOB}/order/{order_id}", headers=headers)
    return resp.status_code == 200

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

def submit_order(signed_order: dict, api_key: str) -> Optional[str]:
    headers = {
        "POLY-API-KEY": api_key,
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(f"{POLYMARKET_CLOB}/order", json=signed_order, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("id")
        else:
            _post(f"Order submit error: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Order submission error: {e}", "error")
        return None

# ── Market analysis ──────────────────────────────────────────────
def find_crushable_markets(config: dict, api_key: str, proxy_wallet: str) -> List[dict]:
    """
    Scan all open markets and find those with thin liquidity on one side,
    sufficient to move the price by a profitable amount.
    Returns list of token details.
    """
    markets = get_all_markets()
    if not markets:
        return []

    max_order_size = float(config["trading"]["max_order_size_usd"])
    min_depth = int(config["trading"]["min_liquidity_depth"])
    target_profit_pct = float(config["trading"]["min_target_profit_pct"]) / 100.0
    max_slippage_pct = float(config["trading"]["max_slippage_pct"]) / 100.0

    candidates = []
    for market in markets:
        for token in market.get("tokens", []):
            token_id = token["token_id"]
            book = get_order_book(token_id)
            asks = book.get("asks", [])
            bids = book.get("bids", [])
            if not asks or not bids:
                continue

            # Determine side to crush: we'll look at the ask side (buy to push up) or bid side (sell to push down)
            # Check ask depth: total size available before price moves beyond profitable level
            best_ask = float(asks[0]["price"])
            best_bid = float(bids[0]["price"])
            mid = (best_ask + best_bid) / 2.0

            # Calculate max size we can buy without exceeding max order size, and how much price moves
            total_size = 0
            total_cost = 0
            ask_prices = []
            for ask in asks:
                price = float(ask["price"])
                size = float(ask["size"])
                if total_cost + size * price > max_order_size:
                    # Partial fill
                    remaining = (max_order_size - total_cost) / price
                    if remaining > 0:
                        total_size += remaining
                        total_cost += remaining * price
                        ask_prices.append((price, remaining))
                    break
                total_size += size
                total_cost += size * price
                ask_prices.append((price, size))
                if total_cost >= max_order_size:
                    break

            if total_size < min_depth:
                continue

            # Average buy price
            avg_buy_price = total_cost / total_size if total_size > 0 else best_ask

            # Now simulate a sell order at the new best ask after the buy (which would be the price we paid for the last shares)
            # Conservative: we assume we can sell back at the new best bid? Actually after buying, the new best ask will be higher. The best bid might also move up, but we'll place a limit sell at the new best ask (which we just created by buying).
            # The new best ask after our buy will be the next ask price after our consumption. If we consumed all asks up to price, the new best ask will be the price of the first remaining ask, or if none, we might have cleared the book.
            # We'll estimate the new best ask as the price of the next ask after our size, or the last price we paid if no more asks.
            if len(asks) > 0:
                remaining_asks = [a for a in asks if float(a["price"]) > avg_buy_price or (float(a["price"]) == avg_buy_price and float(a["size"]) > total_size)]
                if remaining_asks:
                    new_best_ask = float(remaining_asks[0]["price"])
                else:
                    new_best_ask = avg_buy_price * 1.02  # assume 2% up if book empty
            else:
                new_best_ask = avg_buy_price * 1.02

            # Profit potential: sell at new best ask minus average buy price, minus estimated slippage
            profit_per_share = new_best_ask - avg_buy_price
            profit_pct = profit_per_share / avg_buy_price

            if profit_pct >= target_profit_pct:
                candidates.append({
                    "token_id": token_id,
                    "market": market.get("question", ""),
                    "side": "BUY",           # we'll buy to push price up
                    "size": int(total_size),
                    "buy_price": avg_buy_price,
                    "expected_sell_price": new_best_ask,
                    "expected_profit_pct": round(profit_pct * 100, 2)
                })

            # Also check for the opposite (sell to push down, then buy back cheaper)
            # Symmetric logic for bids side (omitted for brevity, but would be similar)
            # We'll just do the buy-to-push-up case here.

    return candidates

# ── Trade execution ──────────────────────────────────────────────
def execute_crush(candidate: dict, config: dict, state: dict):
    """
    Place the initial aggressive order, record in state, and place an exit limit order.
    """
    token_id = candidate["token_id"]
    size = candidate["size"]
    buy_price = candidate["buy_price"]
    sell_price = candidate["expected_sell_price"]

    pm_cfg = config["polymarket"]
    api_key = pm_cfg["clob_api_key"]
    private_key = pm_cfg["clob_private_key"]
    proxy_wallet = pm_cfg["proxy_wallet"]
    dry_run = config["trading"].get("dry_run", False)

    # Check if we already have an active position for this token
    if token_id in state.get("active_positions", {}):
        _post(f"Already position in token {token_id}, skipping", "info")
        return

    # Step 1: Place buy order (aggressive, limit at the worst price we'll pay)
    # We need to consume the ask book; we'll place a limit order at the highest price we're willing to pay (which is the average buy price? Actually we should place a buy order at the price of the last ask we intend to eat, to ensure it fills quickly. For simplicity, we'll place a buy order at the current best ask price (the first ask) with size equal to total_size, but if that ask's size is insufficient, the order will partially fill and the rest will be cancelled. To avoid cancellations, we'll place a buy order at the price of the highest ask we're willing to pay (the last price we included). But the CLOB matches by price and time; we can place a limit buy at a price that is equal to the highest price we're willing to pay, and the size = total_size. Then the order will fill up to that price.
    # We'll compute the maximum price among the consumed asks and set that as limit.
    max_price = buy_price * 1.01  # small buffer
    side = "BUY"

    if dry_run:
        _post(f"[DRY] Would crush token {token_id}: buy {size} @ avg {buy_price:.4f}, sell target {sell_price:.4f}", "info")
        return

    signed_buy = sign_order(private_key, proxy_wallet, token_id, max_price, size, side)
    buy_id = submit_order(signed_buy, api_key)
    if not buy_id:
        _post(f"Failed to place crush buy order for token {token_id}", "error")
        return

    _post(f"Crush buy order placed: {buy_id}, size {size}, price limit {max_price:.4f}", "info")

    # Step 2: Immediately place a sell limit order at the expected sell price
    sell_side = "SELL"
    signed_sell = sign_order(private_key, proxy_wallet, token_id, sell_price, size, sell_side)
    sell_id = submit_order(signed_sell, api_key)
    if not sell_id:
        _post(f"Failed to place crush sell order for token {token_id}, cancelling buy", "error")
        cancel_order(buy_id, api_key)
        return

    _post(f"Crush sell order placed: {sell_id}, size {size}, price {sell_price:.4f}", "info")

    # Record in state
    state.setdefault("active_positions", {})[token_id] = {
        "buy_order_id": buy_id,
        "sell_order_id": sell_id,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "size": size,
        "buy_price": buy_price,
        "sell_price": sell_price
    }

# ── Position monitoring ──────────────────────────────────────────
def manage_positions(config: dict, state: dict):
    """Check filled/cancelled orders and log profits."""
    pm_cfg = config["polymarket"]
    api_key = pm_cfg["clob_api_key"]
    timeout_minutes = int(config["trading"]["position_timeout_minutes"])

    for token_id, pos in list(state.get("active_positions", {}).items()):
        # Check if orders still open
        buy_id = pos["buy_order_id"]
        sell_id = pos["sell_order_id"]
        # We can check status via CLOB get order endpoint (simplified: try cancel, if fails it's filled)
        # Better to get order status: /order/{order_id}
        buy_status = None
        sell_status = None
        try:
            resp = requests.get(f"{POLYMARKET_CLOB}/order/{buy_id}", headers={"POLY-API-KEY": api_key})
            if resp.status_code == 200:
                buy_status = resp.json().get("status")
        except:
            pass
        try:
            resp = requests.get(f"{POLYMARKET_CLOB}/order/{sell_id}", headers={"POLY-API-KEY": api_key})
            if resp.status_code == 200:
                sell_status = resp.json().get("status")
        except:
            pass

        # If both filled, profit realized
        if buy_status == "FILLED" and sell_status == "FILLED":
            profit_usd = (pos["sell_price"] - pos["buy_price"]) * pos["size"]
            _post(f"Crush completed for token {token_id}: profit ${profit_usd:.2f}", "error", {
                "token_id": token_id,
                "buy_price": pos["buy_price"],
                "sell_price": pos["sell_price"],
                "size": pos["size"],
                "profit": profit_usd
            })
            del state["active_positions"][token_id]
            continue

        # If timeout reached and still open, cancel both and exit
        entry_time = datetime.fromisoformat(pos["entry_time"])
        if datetime.now(timezone.utc) - entry_time > timedelta(minutes=timeout_minutes):
            _post(f"Position timeout for token {token_id}, cancelling orders", "warning")
            for oid in [buy_id, sell_id]:
                try:
                    cancel_order(oid, api_key)
                except Exception:
                    pass
            # Any partial fills? For simplicity, we assume lost.
            del state["active_positions"][token_id]

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Liquidity Crush Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "liquidity_crush_state.json")
        state = load_state(state_file)

        # Manage existing positions
        manage_positions(config, state)

        # Find new crush opportunities
        candidates = find_crushable_markets(config, config["polymarket"]["clob_api_key"],
                                            config["polymarket"]["proxy_wallet"])
        if candidates:
            _post(f"Found {len(candidates)} crushable markets", "info")
            for cand in candidates[:2]:  # limit number of simultaneous positions
                execute_crush(cand, config, state)

        save_state(state_file, state)
        poll_min = float(config.get("poll_interval_minutes", 5))
        _heartbeat()
        time.sleep(poll_min * 60)

if __name__ == "__main__":
    main()
