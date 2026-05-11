#!/usr/bin/env python3
"""
jit_liquidity_scanner.py — JIT Liquidity Opportunity Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors the Ethereum mempool for large UniV3 swaps and
estimates the fee revenue a JIT liquidity position could capture.

⚠️  THIS BOT DOES NOT PROVIDE LIQUIDITY. IT ONLY DETECTS.
══════════════════════════════════════════════════════════════════════
Real JIT execution requires a custom smart contract and Flashbots relay.

SETUP
─────
1. Install dependencies:
      pip install web3 ccxt requests

2. Export WebSocket RPC:
      export ETH_WSS_URL="wss://mainnet.infura.io/ws/v3/YOUR-KEY"

3. Attach to BotController.
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
import ccxt

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "jit_liquidity_scanner_bot"
BOT_NAME = "JIT Liquidity Scanner"

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
MIN_TRADE_VALUE_USD = 10000        # ignore swaps below this
MIN_ESTIMATED_FEE_USD = 20         # ignore if estimated JIT fee < $20

# ── Uniswap V3 Router and SwapRouter02 (mainnet) ─────────────────────────────
UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564".lower()  # SwapRouter
UNISWAP_V3_ROUTER_02 = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45".lower() # Universal Router

# Function signatures
EXACT_INPUT_SIG = "0x414bf389"    # exactInput((bytes,address,uint256,uint256,uint256))
EXACT_OUTPUT_SIG = "0xc04b8d59"   # exactOutput((bytes,address,uint256,uint256,uint256))

# ── CEX price feed (Binance) ─────────────────────────────────────────────────
cex = ccxt.binance({'enableRateLimit': True})
cex.load_markets()

def get_cex_price(symbol):
    try:
        ticker = cex.fetch_ticker(symbol)
        return ticker['last']
    except:
        return None

# ── UniV3 pool data ───────────────────────────────────────────────────────────
# Minimal ABI to get fee and slot0
POOL_ABI = json.loads("""[
    {"constant":true,"inputs":[],"name":"fee","outputs":[{"internalType":"uint24","name":"","type":"uint24"}],"stateMutability":"view","type":"function"},
    {"constant":true,"inputs":[],"name":"slot0","outputs":[
        {"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},
        {"internalType":"int24","name":"tick","type":"int24"},
        {"internalType":"uint16","name":"observationIndex","type":"uint16"},
        {"internalType":"uint16","name":"observationCardinality","type":"uint16"},
        {"internalType":"uint16","name":"observationCardinalityNext","type":"uint16"},
        {"internalType":"uint8","name":"feeProtocol","type":"uint8"},
        {"internalType":"bool","name":"unlocked","type":"bool"}
    ],"stateMutability":"view","type":"function"},
    {"constant":true,"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"constant":true,"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}
]""")

def get_pool_info(w3, pool_address):
    pool_contract = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=POOL_ABI)
    try:
        fee = pool_contract.functions.fee().call()
        slot0 = pool_contract.functions.slot0().call()
        token0 = pool_contract.functions.token0().call()
        token1 = pool_contract.functions.token1().call()
        return int(fee), slot0[1], token0.lower(), token1.lower()
    except Exception:
        return None, None, None, None

# ── Token decimals (cached) ────────────────────────────────────────────────────
ERC20_ABI = json.loads('[{"constant":true,"inputs":[],"name":"decimals","outputs":[{"internalType":"uint8","name":"","type":"uint8"}],"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"symbol","outputs":[{"internalType":"string","name":"","type":"string"}],"stateMutability":"view","type":"function"}]')
decimals_cache = {}

def get_decimals(w3, token_addr):
    if token_addr in decimals_cache:
        return decimals_cache[token_addr]
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
        dec = contract.functions.decimals().call()
        decimals_cache[token_addr] = dec
        return dec
    except:
        return 18

# ── Decode UniV3 path ─────────────────────────────────────────────────────────
# Path is encoded as bytes: tokenAddress(20) + fee(3) + tokenAddress(20) + fee(3)...
def decode_path(path_bytes):
    path = []
    i = 0
    while i < len(path_bytes):
        token = "0x" + path_bytes[i:i+20].hex()
        i += 20
        if i + 3 <= len(path_bytes):
            fee = int.from_bytes(path_bytes[i:i+3], 'big')
            i += 3
            path.append((token, fee))
        else:
            path.append((token, None))
    return path

# ── Compute token price in USD and amount in human units ──────────────────────
def token_value_usd(token_address, amount_raw, w3):
    # Use WETH price and pair if not WETH
    weth = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    decimals_input = get_decimals(w3, token_address)
    amount = Decimal(amount_raw) / Decimal(10 ** decimals_input)

    # If token == WETH, price from CEX
    if token_address.lower() == weth.lower():
        eth_price = get_cex_price("ETH/USDT")
        if eth_price:
            return float(amount) * eth_price, float(amount)
        else:
            return None, float(amount)

    # Else find on-chain WETH pair to get token price in ETH
    # Use Uniswap V2 factory for simplicity (liquidity always available)
    # We'll reuse the pair approach from the cross-chain bot, but here we can do a quick V2 lookup.
    # For brevity, we'll skip and return a warning; you can extend.
    _post(f"Unsupported non-WETH token {token_address}. Cannot price.", "warning")
    return None, float(amount)

# ── Mempool watcher ────────────────────────────────────────────────────────────
async def watch_mempool():
    if not WSS_URL:
        _post("ETH_WSS_URL not set.", "error")
        return
    w3 = Web3(Web3.WebsocketProvider(WSS_URL))
    if not w3.is_connected():
        _post("Cannot connect to Ethereum node.", "error")
        return
    _post("JIT Scanner connected. Watching for large UniV3 swaps...", "info")
    pending_filter = w3.eth.filter('pending')

    while True:
        try:
            for tx_hash in await asyncio.wait_for(pending_filter.get_new_entries(), timeout=30):
                try:
                    tx = w3.eth.get_transaction(tx_hash)
                except:
                    continue
                to_addr = tx['to'].lower() if tx['to'] else ""
                if to_addr not in (UNISWAP_V3_ROUTER, UNISWAP_V3_ROUTER_02):
                    continue

                input_data = tx.input
                if len(input_data) < 10:
                    continue
                func_sig = input_data[:4].hex()

                # Parse exactInput or exactOutput
                try:
                    if func_sig == EXACT_INPUT_SIG:
                        params = decode(['bytes','address','uint256','uint256','uint256'], input_data[4:])
                        path_bytes = params[0]
                        amount_in = params[3]
                        # For JIT we care about the amount being swapped
                    elif func_sig == EXACT_OUTPUT_SIG:
                        params = decode(['bytes','address','uint256','uint256','uint256'], input_data[4:])
                        path_bytes = params[0]
                        amount_out = params[4]  # not needed
                        # exactOutput; amount_in unknown, skip for simplicity
                        continue
                    else:
                        continue
                except:
                    continue

                # Decode the path to get the pool(s) involved
                decoded = decode_path(path_bytes)
                if len(decoded) < 2:
                    continue
                # The first hop is the pool where the swap occurs; we'll get its fee
                token_in, fee_tier = decoded[0]
                token_out = decoded[1][0]
                if fee_tier is None:
                    continue

                # Get the pool address (UniV3 pool is determined by token0, token1, fee, but we don't have it directly from path)
                # We need to know the actual pool contract. We can get it by calling the UniswapV3Factory.
                UNI_V3_FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
                FACTORY_ABI = json.loads('[{"constant":true,"inputs":[{"internalType":"address","name":"","type":"address"},{"internalType":"address","name":"","type":"address"},{"internalType":"uint24","name":"","type":"uint24"}],"name":"getPool","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]')
                factory = w3.eth.contract(address=Web3.to_checksum_address(UNI_V3_FACTORY), abi=FACTORY_ABI)
                try:
                    pool_addr = factory.functions.getPool(
                        Web3.to_checksum_address(token_in),
                        Web3.to_checksum_address(token_out),
                        fee_tier
                    ).call()
                except:
                    continue
                if pool_addr == "0x0000000000000000000000000000000000000000":
                    continue

                # Get pool's fee and current tick
                pool_fee, current_tick, t0, t1 = get_pool_info(w3, pool_addr)
                if pool_fee is None:
                    continue

                # Fee percentage
                fee_pct = pool_fee / 1_000_000

                # Determine trade value in USD (approximate via token_in price)
                token_price_usd, amount_human = token_value_usd(token_in, amount_in, w3)
                if token_price_usd is None:
                    continue
                trade_value_usd = amount_human * token_price_usd

                if trade_value_usd < MIN_TRADE_VALUE_USD:
                    continue

                # Estimated JIT profit = full fees captured by the LP
                estimated_fee_usd = trade_value_usd * fee_pct

                if estimated_fee_usd < MIN_ESTIMATED_FEE_USD:
                    continue

                payload = {
                    "tx_hash": tx_hash.hex(),
                    "pool": pool_addr,
                    "token_in": token_in,
                    "token_out": token_out,
                    "trade_amount": round(amount_human, 6),
                    "trade_value_usd": round(trade_value_usd, 2),
                    "pool_fee_tier": pool_fee,
                    "jit_potential_fee_usd": round(estimated_fee_usd, 2)
                }

                _post(
                    f"JIT opportunity: {trade_value_usd:.0f} USD swap on pool {pool_addr[:10]}... "
                    f"fee tier {pool_fee/10000:.2f}% → potential JIT profit ~${estimated_fee_usd:.2f}",
                    "warning" if estimated_fee_usd > 100 else "info",
                    payload
                )
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            _post(f"Mempool error: {e}", "error")
            await asyncio.sleep(5)

def start_watcher():
    loop = asyncio.new_event_loop()
    loop.run_until_complete(watch_mempool())

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    if not WSS_URL:
        _post("ETH_WSS_URL not set. Bot idle.", "error")
        while True:
            _heartbeat()
            time.sleep(60)

    threading.Thread(target=start_watcher, daemon=True).start()
    _post("JIT Liquidity Scanner running. Watching for swaps worth providing JIT liquidity.", "info")
    while True:
        _heartbeat()
        time.sleep(10)

if __name__ == "__main__":
    main()
