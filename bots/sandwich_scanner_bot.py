#!/usr/bin/env python3
"""
sandwich_scanner_bot.py — Meme Coin Sandwich Opportunity Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors the Ethereum mempool for large swaps on low‑liquidity
pools (e.g., Uniswap V2 meme coin pairs) and estimates the
profit a sandwich attack could capture.

⚠️  THIS BOT DOES NOT SEND TRANSACTIONS.
══════════════════════════════════════════════
It is a surveillance tool. Real sandwich execution requires
a private node, a custom contract, and Flashbots relay.

SETUP
─────
1. Install dependencies:
      pip install web3 requests

2. Get an Infura or Alchemy WebSocket endpoint and export it:
      export ETH_WSS_URL="wss://mainnet.infura.io/ws/v3/YOUR-ID"

3. Create `sandwich_config.json` (example at the bottom) with
   the meme coin token addresses and their Uniswap V2 pair
   addresses you want to watch.

4. Attach to BotController.
"""

import os
import json
import time
import asyncio
import threading
import requests
from decimal import Decimal
from web3 import Web3
from eth_abi import decode

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "sandwich_scanner_bot"
BOT_NAME = "Meme Coin Sandwich Scanner"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "sandwich_config.json")

HEARTBEAT_INTERVAL = 20
_last_hb = 0.0
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

# ── Configuration ──────────────────────────────────────────────────────────────
WSS_URL = os.getenv("ETH_WSS_URL", "").strip()

# Uniswap V2 Router address (used by most meme coins via Router02)
UNISWAP_V2_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D".lower()

# Known function signatures for swaps on Router02
SWAP_SIGNATURES = {
    "swapExactETHForTokens": "0x7ff36ab5",
    "swapExactTokensForETH": "0x18cbafe5",
    "swapExactTokensForTokens": "0x38ed1739",
}

# Minimum profit estimate in USD to alert
MIN_PROFIT_USD = 20.0

# ── Load token config ──────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        _post("No sandwich_config.json found. Creating default.", "error")
        default = {
            "watch_pairs": [
                # Format: {"token_name": "PEPE", "token_address": "0x...", "pair_address": "0x..."}
            ]
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

# ── Price fetcher (simple) ─────────────────────────────────────────────────────
TOKEN_PRICES_CACHE = {
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": 1800.0,  # WETH
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": 1.0,     # USDC
    "0xdAC17F958D2ee523a2206206994597C13D831ec7": 1.0,     # USDT
}

def get_token_price(address):
    """Return approximate USD price. Extend with Coingecko calls if needed."""
    return TOKEN_PRICES_CACHE.get(address.lower())

# ── Reserve fetcher (on‑chain) ─────────────────────────────────────────────────
# Minimal ABI for Uniswap V2 pair to get reserves
PAIR_ABI = json.loads("""[
    {"constant":true,"inputs":[],"name":"getReserves","outputs":[
        {"internalType":"uint112","name":"_reserve0","type":"uint112"},
        {"internalType":"uint112","name":"_reserve1","type":"uint112"},
        {"internalType":"uint32","name":"_blockTimestampLast","type":"uint32"}
    ],"stateMutability":"view","type":"function"},
    {"constant":true,"inputs":[],"name":"token0","outputs":[
        {"internalType":"address","name":"","type":"address"}
    ],"stateMutability":"view","type":"function"},
    {"constant":true,"inputs":[],"name":"token1","outputs":[
        {"internalType":"address","name":"","type":"address"}
    ],"stateMutability":"view","type":"function"}
]""")

def get_pair_reserves(pair_address, w3):
    contract = w3.eth.contract(address=Web3.to_checksum_address(pair_address), abi=PAIR_ABI)
    try:
        res = contract.functions.getReserves().call()
        token0 = contract.functions.token0().call()
        token1 = contract.functions.token1().call()
        return token0, token1, res[0], res[1]
    except Exception:
        return None, None, 0, 0

# ── Mempool watcher (async) ────────────────────────────────────────────────────
async def watch_mempool():
    if not WSS_URL:
        _post("ETH_WSS_URL not set. Bot idle.", "error")
        return

    w3 = Web3(Web3.WebsocketProvider(WSS_URL))
    if not w3.is_connected():
        _post("Cannot connect to Ethereum node.", "error")
        return

    _post("Mempool scanner connected. Watching for meme coin swap opportunities...", "info")
    pending_filter = w3.eth.filter('pending')

    # Load config once and cache pairs
    cfg = load_config()
    watched_pairs = {entry["pair_address"].lower(): entry for entry in cfg.get("watch_pairs", [])}

    while True:
        try:
            for tx_hash in await asyncio.wait_for(pending_filter.get_new_entries(), timeout=30):
                try:
                    tx = w3.eth.get_transaction(tx_hash)
                except Exception:
                    continue
                if not tx or tx.get('to', '').lower() != UNISWAP_V2_ROUTER:
                    continue
                input_data = tx.input
                if len(input_data) < 10:
                    continue

                func_sig = input_data[:4].hex()
                if func_sig not in SWAP_SIGNATURES.values():
                    continue

                # Decode based on function signature
                # For swapExactETHForTokens (0x7ff36ab5): (uint amountOutMin, address[] path, address to, uint deadline)
                if func_sig == SWAP_SIGNATURES["swapExactETHForTokens"]:
                    try:
                        decoded = decode(['uint256', 'address[]', 'address', 'uint256'], input_data[4:])
                        path = decoded[1]
                        amount_out_min = decoded[0]  # not used
                        # The amount of ETH sent is tx.value
                        amount_in = tx.value
                        token_in_address = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"  # WETH (ETH is wrapped)
                        token_out_address = path[-1].lower()
                    except:
                        continue

                elif func_sig == SWAP_SIGNATURES["swapExactTokensForETH"]:
                    try:
                        decoded = decode(['uint256', 'uint256', 'address[]', 'address', 'uint256'], input_data[4:])
                        amount_in = decoded[0]
                        path = decoded[2]
                        token_in_address = path[0].lower()
                        token_out_address = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
                    except:
                        continue

                elif func_sig == SWAP_SIGNATURES["swapExactTokensForTokens"]:
                    try:
                        decoded = decode(['uint256', 'uint256', 'address[]', 'address', 'uint256'], input_data[4:])
                        amount_in = decoded[0]
                        path = decoded[2]
                        token_in_address = path[0].lower()
                        token_out_address = path[-1].lower()
                    except:
                        continue
                else:
                    continue

                # Check if the token pair involves one of our watched meme coins
                # We look for a pair address that matches token0/token1 combination
                # For simplicity, we compare token addresses directly with known pairs
                pair_found = False
                for pair_addr, info in watched_pairs.items():
                    pair_token = info.get("token_address", "").lower()
                    if token_in_address == pair_token or token_out_address == pair_token:
                        pair_found = True
                        pair_info = info
                        break
                if not pair_found:
                    continue

                # Fetch current reserves of the pair to estimate slippage
                token0, token1, r0, r1 = get_pair_reserves(pair_info["pair_address"], w3)
                if token0 is None:
                    continue

                # Determine which reserve corresponds to tokenIn and tokenOut
                # (we need to know the decimal places for precise calculation, but for estimates we use raw amounts)
                # For an estimate, we can compute the expected amount out using Uniswap V2 formula: x*y=k
                reserve_in = r0 if token_in_address == token0.lower() else r1
                reserve_out = r1 if token_in_address == token0.lower() else r0

                if reserve_in == 0 or reserve_out == 0:
                    continue

                # Price impact (slippage) as a fraction: (amount_in / (reserve_in + amount_in)) roughly
                # More precisely: new price = (reserve_out * reserve_in) / (reserve_in + amount_in)
                # But for an estimate we use: impact = amount_in / (reserve_in + amount_in)
                slippage_pct = float(Decimal(amount_in) / Decimal(reserve_in + amount_in))

                # A sandwich attack profits by capturing this slippage twice (buy before, sell after)
                # Overall profit ~ transaction_value * slippage * 2, minus gas.
                # We estimate USD value of the trade using token prices.
                token_price = get_token_price(token_in_address)
                if not token_price:
                    continue
                # Convert amount_in to human units (assuming 18 decimals; adjust if needed)
                # For simplicity, we treat amount_in in wei, divide by 1e18.
                trade_value_usd = (float(Decimal(amount_in) / Decimal(1e18))) * token_price
                estimated_profit = trade_value_usd * slippage_pct * 2.0

                if estimated_profit < MIN_PROFIT_USD:
                    continue

                payload = {
                    "tx_hash": tx_hash.hex(),
                    "pair": pair_info["token_name"],
                    "trader": tx['from'],
                    "side": "BUY" if func_sig == SWAP_SIGNATURES["swapExactETHForTokens"] else "SELL",
                    "trade_value_usd": round(trade_value_usd, 2),
                    "slippage_est": round(slippage_pct*100, 2),
                    "sandwich_profit_est": round(estimated_profit, 2),
                }

                _post(
                    f"Sandwich opportunity on {pair_info['token_name']}: "
                    f"${trade_value_usd:.0f} swap, ~{slippage_pct*100:.1f}% impact, "
                    f"possible profit ~${estimated_profit:.0f}",
                    "warning" if estimated_profit > 100 else "info",
                    payload,
                )
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            _post(f"Mempool error: {e}", "error")
            await asyncio.sleep(5)

def start_watcher():
    loop = asyncio.new_event_loop()
    loop.run_until_complete(watch_mempool())

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    if not WSS_URL:
        _post("No WebSocket URL for Ethereum node. Bot idle.", "error")
        while True:
            _heartbeat()
            time.sleep(60)

    threading.Thread(target=start_watcher, daemon=True).start()
    _post("Meme Coin Sandwich Scanner running. Watching for large swaps on configured pairs.", "info")

    while True:
        _heartbeat()
        time.sleep(10)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `sandwich_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "watch_pairs": [
    {
      "token_name": "PEPE",
      "token_address": "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
      "pair_address": "0xa43fe16908251ee70ef74718545e4fe6c5ccec9"   (example – replace with real PEPE/WETH pair)
    },
    {
      "token_name": "WIF",
      "token_address": "0x0221bca6f156b168c3b4e9b8d1d2bdb8b2a3c2e1",  (fake – replace)
      "pair_address": "0x..."
    }
  ]
}
"""
