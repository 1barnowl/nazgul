#!/usr/bin/env python3
"""
uniswap_v4_hooks_arbitrage_bot.py — Uniswap V4 Arbitrage Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors Uniswap V4 pools (via RPC) for spread inefficiencies
vs. other exchanges, and estimates the profit a custom hook
could capture.  No hooks are deployed; this is a surveillance
tool only.

SETUP
─────
1. Install dependencies:
      pip install web3 requests

2. Set an Ethereum RPC URL (HTTPS is fine for reading):
      export ETH_RPC_URL="https://mainnet.infura.io/v3/YOUR-KEY"

3. Optionally configure a list of V4 pool addresses to watch
   in `v4_arb_config.json`.

4. Attach to BotController.
"""

import os
import json
import time
import requests
from datetime import datetime
from decimal import Decimal
from web3 import Web3

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "uniswap_v4_hooks_arbitrage_bot"
BOT_NAME = "Uniswap V4 Arb Scanner"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "v4_arb_config.json")

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
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME, "status": "online"
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

def _wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Configuration ──────────────────────────────────────────────────────────────
ETH_RPC_URL = os.getenv("ETH_RPC_URL", "")
if not ETH_RPC_URL:
    _post("ETH_RPC_URL not set. Bot idle.", "error")

# Default V4 pool addresses (only examples; you need to find real V4 pools)
# Uniswap V4 is still early; mainnet deployments exist for many token pairs.
# You can add addresses via the config file.
DEFAULT_POOLS = [
    "0x...",  # replace with actual V4 pool addresses
]

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"pools": DEFAULT_POOLS}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

# ── Blockchain connection ──────────────────────────────────────────────────────
w3 = None
if ETH_RPC_URL:
    w3 = Web3(Web3.HTTPProvider(ETH_RPC_URL))
    if not w3.is_connected():
        _post("Cannot connect to Ethereum RPC.", "error")
        w3 = None

# ── Simplified price fetcher (using external APIs) ────────────────────────────
# For real arbitrage detection you need real-time prices from other exchanges.
# Here we use a free aggregator (Coingecko) as a benchmark.
def get_market_price(token_id="ethereum"):
    """Get current price of token in USD from Coingecko."""
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={token_id}&vs_currencies=usd"
        resp = requests.get(url, timeout=10)
        return resp.json()[token_id]["usd"]
    except Exception:
        return None

# ── Uniswap V4 pool data ──────────────────────────────────────────────────────
# Minimal ABI for V4 pool (getSlot0, getHook, swapFee)
V4_POOL_ABI = json.loads("""[
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "getHook",
        "outputs": [{"internalType": "address", "name": "hook", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "swapFee",
        "outputs": [{"internalType": "uint24", "name": "fee", "type": "uint24"}],
        "stateMutability": "view",
        "type": "function"
    }
]""")

def get_pool_info(pool_address):
    """Return {sqrtPrice, tick, hookAddress, swapFee} or None."""
    contract = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=V4_POOL_ABI)
    try:
        slot0 = contract.functions.slot0().call()
        hook = contract.functions.getHook().call()
        fee = contract.functions.swapFee().call()
        return {
            "sqrtPriceX96": slot0[0],
            "tick": slot0[1],
            "hookAddress": hook,
            "swapFee": fee
        }
    except Exception as e:
        _post(f"Error reading pool {pool_address}: {e}", "warning")
        return None

def tick_to_price(tick, decimals0, decimals1):
    """Convert tick to price (token1/token0)."""
    return 1.0001 ** tick * (10 ** decimals0) / (10 ** decimals1)

# ── Arbitrage scan ─────────────────────────────────────────────────────────────
def scan():
    cfg = load_config()
    pools = cfg.get("pools", [])
    if not pools or pools == [""]:
        _post("No V4 pool addresses configured.", "info")
        return

    for pool_addr in pools:
        if not pool_addr.startswith("0x"):
            continue
        info = get_pool_info(pool_addr)
        if not info:
            continue

        # For demonstration, we compare the pool price (token1/token0) with an
        # external price (Coingecko) assuming token0 is WETH and token1 is USDC.
        # This is a massive simplification; you'd need a proper oracle.
        eth_price = get_market_price("ethereum")
        if not eth_price:
            continue
        # Price from pool: token1 per token0 (USDC per ETH)
        pool_price = tick_to_price(info["tick"], 18, 6)  # WETH/USDC

        # Compare
        diff = pool_price - eth_price
        diff_pct = (diff / eth_price) * 100

        payload = {
            "pool": pool_addr,
            "sqrtPriceX96": str(info["sqrtPriceX96"]),
            "tick": info["tick"],
            "hook": info["hookAddress"],
            "swapFee": info["swapFee"],
            "pool_price": round(pool_price, 2),
            "external_price": eth_price,
            "diff_pct": round(diff_pct, 2)
        }

        if abs(diff_pct) > 1.0:
            level = "error" if abs(diff_pct) > 2.0 else "warning"
            _post(f"V4 pool {pool_addr[-8:]}: price ${pool_price:.2f} vs market ${eth_price:.2f} ({diff_pct:+.2f}%)",
                  level, payload)
        else:
            _post(f"V4 pool {pool_addr[-8:]}: price aligned (diff {diff_pct:+.2f}%)", "info", payload)

    _heartbeat()

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    if not ETH_RPC_URL or not w3:
        _post("Ethereum RPC unavailable. Bot idle.", "error")
        while True:
            _heartbeat()
            time.sleep(60)

    _post("Uniswap V4 Arbitrage Scanner online — watching pools for spread inefficiencies.", "info")

    while True:
        scan()
        time.sleep(60)  # 1 minute interval

if __name__ == "__main__":
    main()
