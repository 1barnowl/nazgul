#!/usr/bin/env python3
"""
crypto_mev_bot.py — Mempool Scanner & Sandwich Detector
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors the Ethereum mempool for large DEX swaps and
calculates theoretical sandwich‐attack profit. Alerts are
sent to BotController so you can decide whether to act.

⚠️  THIS BOT DOES NOT EXECUTE FRONT‑RUNNING TRANSACTIONS.
══════════════════════════════════════════════════════════
It is a surveillance/analysis tool only. Real MEV execution
requires a private node, Flashbots relay, and sophisticated
risk management.

SETUP
─────
1. Install dependencies:
      pip install web3 requests eth-abi

2. Get an Infura or Alchemy WebSocket endpoint URL.
   Set it as environment variable:
      export ETH_WSS_URL="wss://mainnet.infura.io/ws/v3/YOUR-PROJECT-ID"

3. Attach to BotController.
"""

import os
import json
import time
import threading
import asyncio
import requests
from datetime import datetime
from decimal import Decimal
from web3 import Web3
from eth_abi import decode

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "crypto_mev_bot"
BOT_NAME = "MEV Mempool Scanner"

HEARTBEAT_INTERVAL = 20
_last_hb = 0.0

# ── Configuration ─────────────────────────────────────────────────────────────
WSS_URL = os.getenv("ETH_WSS_URL", "").strip()

# DEX router addresses we care about (Uniswap V2 & V3, SushiSwap)
ROUTER_ADDRESSES = {
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D".lower(): "Uniswap V2",
    "0xE592427A0AEce92De3Edee1F18E0157C05861564".lower(): "Uniswap V3",
    "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F".lower(): "SushiSwap",
}

# Minimum swap value in USD to consider
MIN_SWAP_VALUE_USD = 50_000

# Quick USD price lookup (using CCXT or on‑chain oracle; here we use a simple
# hardcoded mapping for demonstration – in production you'd fetch live prices)
TOKEN_PRICES = {
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": 1800.0,  # WETH
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": 1.0,     # USDC
    "0xdAC17F958D2ee523a2206206994597C13D831ec7": 1.0,     # USDT
    "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": 67000.0, # WBTC
}

SCAN_INTERVAL = 5  # seconds between heartbeat pings

_last_hb_lock = threading.Lock()

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
    with _last_hb_lock:
        now = time.time()
        if now - _last_hb < HEARTBEAT_INTERVAL:
            return
        _last_hb = now
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

# ── Mempool watcher ───────────────────────────────────────────────────────────
async def watch_mempool():
    if not WSS_URL:
        _post("No ETH_WSS_URL set. Bot cannot watch mempool.", "error")
        return

    w3 = Web3(Web3.WebsocketProvider(WSS_URL))
    if not w3.is_connected():
        _post("Could not connect to Ethereum node.", "error")
        return

    _post("Mempool scanner connected. Watching for large swaps...", "info")
    pending_tx_filter = w3.eth.filter('pending')
    while True:
        try:
            for tx_hash in await asyncio.wait_for(
                pending_tx_filter.get_new_entries(), timeout=30
            ):
                try:
                    tx = w3.eth.get_transaction(tx_hash)
                except Exception:
                    continue
                if not tx or not tx.get('to'):
                    continue
                to_addr = tx['to'].lower()
                router_name = ROUTER_ADDRESSES.get(to_addr)
                if not router_name:
                    continue

                # Decode the transaction input to find the swap path
                # We'll try to parse typical Uniswap V2/V3 function signatures
                input_data = tx.input
                if len(input_data) < 10:
                    continue
                func_sig = input_data[:10].hex()
                # Common swap function signatures (V2: swapExactETHForTokens,
                # swapExactTokensForETH, swapExactTokensForTokens; V3: exactInput, exactOutput)
                # We'll attempt a rough parse to get the first token sold and amount.
                # This is greatly simplified; a real bot would use ABIs and decode fully.
                try:
                    # For demo, we only handle a few well-known methods
                    if func_sig in ["0x7ff36ab5", "0x18cbafe5"]:  # swapExactETHForTokens / swapTokensForExactETH
                        # params vary, so skip deep decoding
                        continue
                    # For V3 exactInput
                    if func_sig == "0x414bf389":
                        # exactInput((bytes path, address recipient, uint256 deadline, uint256 amountIn, uint256 amountOutMinimum))
                        # Decode the struct
                        params = decode(['bytes', 'address', 'uint256', 'uint256', 'uint256'], input_data[4:])
                        path = params[0]
                        amount_in = params[3]
                        # The path encodes token addresses, starting with the token sold
                        token_in_addr = "0x" + path[:40].hex()
                        # Estimate USD value of token sold
                        token_price = TOKEN_PRICES.get(token_in_addr.lower())
                        if token_price is None:
                            continue
                        # For WETH, amount_in is in wei; for USDC/USDT it's 6 decimals
                        decimals = 18 if token_in_addr.lower() == "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2".lower() else 6
                        amount_in_units = Decimal(amount_in) / Decimal(10**decimals)
                        value_usd = float(amount_in_units) * token_price
                        if value_usd < MIN_SWAP_VALUE_USD:
                            continue

                        # Crude sandwich profit estimation (data retrieval)
                        # Typically, sandwich attack profit is ~0.5-1% of swap value for large trades
                        estimated_sandwich_profit = value_usd * 0.005  # 0.5%

                        # Build the alert payload
                        payload = {
                            "tx_hash": tx_hash.hex(),
                            "router": router_name,
                            "from": tx['from'],
                            "gas_price_wei": tx.gasPrice,
                            "token_sold": token_in_addr,
                            "amount_in_units": float(amount_in_units),
                            "estimated_value_usd": value_usd,
                            "theoretical_sandwich_reward": estimated_sandwich_profit,
                        }

                        level = "info"
                        if estimated_sandwich_profit > 1000:
                            level = "error"
                        elif estimated_sandwich_profit > 200:
                            level = "warning"

                        _post(
                            f"{router_name} swap: {value_usd:,.0f} USD → "
                            f"potential sandwich reward ~${estimated_sandwich_profit:.0f}",
                            level,
                            payload,
                        )
                except Exception:
                    # Skip malformed transactions
                    continue
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            _post(f"Mempool error: {e}", "error")
            await asyncio.sleep(5)
        await asyncio.sleep(0.1)  # yield control

def start_mempool_watcher():
    loop = asyncio.new_event_loop()
    loop.run_until_complete(watch_mempool())

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    _post("MEV Mempool Scanner starting — monitoring mempool for sandwichable swaps.", "info")
    if not WSS_URL:
        _post("No WebSocket URL for Ethereum node. Bot idle.", "error")
        while True:
            _heartbeat()
            time.sleep(60)

    # Start mempool watcher in a separate thread
    threading.Thread(target=start_mempool_watcher, daemon=True).start()

    while True:
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
