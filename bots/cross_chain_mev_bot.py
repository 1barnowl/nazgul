#!/usr/bin/env python3
"""
cross_chain_mev_bot.py — Cross‑Chain MEV Opportunity Scanner (Real Data)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors mempools on Ethereum Mainnet, Arbitrum, and Optimism
for large swaps. Compares real DEX prices (from pool reserves) with
live CEX prices to surface cross‑chain arbitrage opportunities.

⚠️  THIS BOT DOES NOT EXECUTE TRANSACTIONS.
══════════════════════════════════════════════════
Execution would require bridge contracts and private
transaction relays. This detects opportunities only.

SETUP
─────
1. Install dependencies:
      pip install web3 ccxt requests

2. Export WebSocket URLs for each chain:
      export ETH_WSS_URL="wss://mainnet.infura.io/ws/v3/YOUR-KEY"
      export ARB_WSS_URL="wss://arbitrum-mainnet.infura.io/ws/v3/YOUR-KEY"
      export OP_WSS_URL="wss://optimism-mainnet.infura.io/ws/v3/YOUR-KEY"

3. (Optional) Adjust `cross_chain_config.json` if you want to
   add more token/watch combinations.

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
import ccxt

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "cross_chain_mev_bot"
BOT_NAME = "Cross-Chain MEV Scanner"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "cross_chain_config.json")

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
def load_config():
    default = {
        "chains": {
            "ethereum": os.getenv("ETH_WSS_URL", ""),
            "arbitrum": os.getenv("ARB_WSS_URL", ""),
            "optimism": os.getenv("OP_WSS_URL", "")
        },
        "cex": "binance",
        "bridge_cost_pct": 0.3,
        "min_profit_usd": 50,
        "watch_tokens": [
            {
                "symbol": "WETH",
                "mainnet_token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "arb_token": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
                "op_token": "0x4200000000000000000000000000000000000006",
                "cex_symbol": "ETH/USDT",
                "decimals": 18
            },
            {
                "symbol": "ARB",
                "mainnet_token": "0xB50721BCf8d664c30412Cfbc6cf7a15145234ad1",
                "arb_token": "0x912CE59144191C1204E64559FE8253a0e49E6548",
                "op_token": "0x912CE59144191C1204E64559FE8253a0e49E6548",
                "cex_symbol": "ARB/USDT",
                "decimals": 18
            }
        ]
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

CFG = load_config()

# ── Uniswap V2 constants ───────────────────────────────────────────────────────
UNISWAP_V2_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D".lower()
UNISWAP_V2_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"  # same on Mainnet, Arbitrum, Optimism

SWAP_SIGNATURES = {
    "swapExactETHForTokens": "0x7ff36ab5",
    "swapExactTokensForETH": "0x18cbafe5",
    "swapExactTokensForTokens": "0x38ed1739",
}

# ── CEX connection ─────────────────────────────────────────────────────────────
cex_exchange = None
if CFG.get("cex"):
    try:
        exchange_class = getattr(ccxt, CFG["cex"])
        cex_exchange = exchange_class({'enableRateLimit': True})
        cex_exchange.load_markets()
    except Exception as e:
        _post(f"CEX init failed: {e}", "error")

def get_cex_price(symbol):
    if not cex_exchange:
        return None
    try:
        ticker = cex_exchange.fetch_ticker(symbol)
        return ticker['last']
    except Exception:
        return None

# ── On‑chain price from Uniswap V2 reserves ───────────────────────────────────
FACTORY_ABI = json.loads('[{"constant":true,"inputs":[{"internalType":"address","name":"","type":"address"},{"internalType":"address","name":"","type":"address"}],"name":"getPair","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]')
PAIR_ABI = json.loads('[{"constant":true,"inputs":[],"name":"getReserves","outputs":[{"internalType":"uint112","name":"_reserve0","type":"uint112"},{"internalType":"uint112","name":"_reserve1","type":"uint112"},{"internalType":"uint32","name":"_blockTimestampLast","type":"uint32"}],"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]')

def get_pair_address(w3, token_a, token_b):
    """Get the Uniswap V2 pair address for two tokens."""
    factory = w3.eth.contract(address=Web3.to_checksum_address(UNISWAP_V2_FACTORY), abi=FACTORY_ABI)
    try:
        return factory.functions.getPair(
            Web3.to_checksum_address(token_a),
            Web3.to_checksum_address(token_b)
        ).call()
    except Exception:
        return None

def get_pair_reserves(w3, pair_address):
    """Return (token0, token1, reserve0, reserve1) or Nones."""
    if pair_address == "0x0000000000000000000000000000000000000000" or not pair_address:
        return None, None, None, None
    contract = w3.eth.contract(address=Web3.to_checksum_address(pair_address), abi=PAIR_ABI)
    try:
        token0 = contract.functions.token0().call()
        token1 = contract.functions.token1().call()
        r0, r1, _ = contract.functions.getReserves().call()
        return token0.lower(), token1.lower(), r0, r1
    except Exception:
        return None, None, None, None

def dex_price_in_eth(w3, token_address, eth_address):
    """Return the price of 1 token in ETH using the token/WETH pair."""
    pair = get_pair_address(w3, token_address, eth_address)
    if not pair or pair == "0x0000000000000000000000000000000000000000":
        return None
    t0, t1, r0, r1 = get_pair_reserves(w3, pair)
    if t0 is None:
        return None
    # Determine which reserve is the token and which is ETH
    if t0 == token_address.lower():
        token_reserve = r0
        eth_reserve = r1
    else:
        token_reserve = r1
        eth_reserve = r0
    if token_reserve == 0:
        return None
    # Price = (amount of ETH) / (amount of token)  [how much ETH for 1 token]
    return eth_reserve / token_reserve

# ── Cross‑chain opportunity evaluator (real prices) ───────────────────────────
def evaluate_cross_chain(w3, tx_hash, chain_name, token_path, amount_in, decimals):
    """Compare the real DEX price of the token being sold with the CEX price."""
    cfg = load_config()
    bridge_fee = Decimal(str(cfg["bridge_cost_pct"])) / 100
    min_profit = Decimal(str(cfg["min_profit_usd"]))
    cex_symbol = None

    # Identify token being sold (first token in path)
    token_sold = token_path[0].lower()
    token_bought = token_path[-1].lower()  # could be WETH or another token

    # Find matching config entry
    for t in cfg["watch_tokens"]:
        if token_sold in [t.get("mainnet_token","").lower(), t.get("arb_token","").lower(), t.get("op_token","").lower()]:
            cex_symbol = t.get("cex_symbol")
            break
    if not cex_symbol:
        return

    # Determine ETH address for this chain
    if chain_name == "ethereum":
        eth_addr = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    elif chain_name == "arbitrum":
        eth_addr = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
    elif chain_name == "optimism":
        eth_addr = "0x4200000000000000000000000000000000000006"
    else:
        return

    # Get real DEX price (in ETH) of the token sold
    token_price_in_eth = dex_price_in_eth(w3, token_sold, eth_addr)
    if token_price_in_eth is None:
        return

    # Get ETH/USD price from CEX (live)
    eth_usd = get_cex_price("ETH/USDT")
    if eth_usd is None:
        return
    dex_price_usd = token_price_in_eth * eth_usd

    # Get CEX price of the token (e.g. ARB/USDT)
    cex_price = get_cex_price(cex_symbol)
    if cex_price is None:
        return

    # Spread
    price_diff_pct = (cex_price - dex_price_usd) / dex_price_usd * 100
    if abs(price_diff_pct) < 0.5:
        return

    # Determine if cross‑chain bridging needed (simplified: if chain != mainnet and token is native to another chain)
    is_cross_chain = False
    # If token is on L2 but CEX price is mainnet equivalent, bridging would be required in reality.
    # We'll assume cross‑chain if chain is not mainnet and token has a mainnet representation.
    for t in cfg["watch_tokens"]:
        if token_sold in [t.get("arb_token","").lower(), t.get("op_token","").lower()]:
            is_cross_chain = True
            break

    potential_profit_pct = abs(price_diff_pct) - (bridge_fee * 100 if is_cross_chain else 0)
    if potential_profit_pct <= 0:
        return

    amount_in_human = Decimal(amount_in) / Decimal(10 ** decimals)
    trade_value_usd = float(amount_in_human) * dex_price_usd
    profit_usd = trade_value_usd * potential_profit_pct / 100

    if profit_usd < float(min_profit):
        return

    payload = {
        "tx_hash": tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash,
        "chain": chain_name,
        "token": token_sold,
        "dex_price_usd": round(dex_price_usd, 6),
        "cex_symbol": cex_symbol,
        "cex_price": round(cex_price, 6),
        "spread_pct": round(price_diff_pct, 2),
        "trade_value_usd": round(trade_value_usd, 2),
        "cross_chain": is_cross_chain,
        "est_profit_usd": round(profit_usd, 2)
    }

    level = "warning" if profit_usd > 200 else "info"
    _post(
        f"{chain_name.upper()}: {cex_symbol} DEX→CEX spread {price_diff_pct:+.2f}%, "
        f"est. profit ${profit_usd:.0f} (bridge {is_cross_chain})",
        level,
        payload
    )

# ── Mempool watcher for one chain ─────────────────────────────────────────────
async def watch_chain(chain_name, wss_url):
    if not wss_url:
        return
    w3 = Web3(Web3.WebsocketProvider(wss_url))
    if not w3.is_connected():
        _post(f"{chain_name}: connection failed", "error")
        return
    _post(f"{chain_name} mempool watcher started.", "info")
    pending_filter = w3.eth.filter('pending')

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

                # Decode swap parameters
                try:
                    if func_sig == SWAP_SIGNATURES["swapExactETHForTokens"]:
                        params = decode(['uint256','address[]','address','uint256'], input_data[4:])
                        path = params[1]
                        amount_in = tx.value
                    elif func_sig == SWAP_SIGNATURES["swapExactTokensForETH"]:
                        params = decode(['uint256','uint256','address[]','address','uint256'], input_data[4:])
                        amount_in = params[0]
                        path = params[2]
                    elif func_sig == SWAP_SIGNATURES["swapExactTokensForTokens"]:
                        params = decode(['uint256','uint256','address[]','address','uint256'], input_data[4:])
                        amount_in = params[0]
                        path = params[2]
                except Exception:
                    continue

                # Determine token decimals from config
                token_sold = path[0].lower()
                decimals = 18
                for t in CFG["watch_tokens"]:
                    if token_sold in [
                        t.get("mainnet_token","").lower(),
                        t.get("arb_token","").lower(),
                        t.get("op_token","").lower()
                    ]:
                        decimals = t.get("decimals", 18)
                        break

                evaluate_cross_chain(w3, tx.hash, chain_name, path, amount_in, decimals)
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            _post(f"{chain_name} mempool error: {e}", "error")
            await asyncio.sleep(5)

# ── Main async runner ──────────────────────────────────────────────────────────
def start_watchers():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    tasks = []
    for name, url in CFG.get("chains", {}).items():
        if url:
            tasks.append(watch_chain(name, url))
    if tasks:
        loop.run_until_complete(asyncio.gather(*tasks))

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    _post("Cross-Chain MEV Scanner starting — using live DEX reserves and CEX prices.", "info")
    threading.Thread(target=start_watchers, daemon=True).start()
    while True:
        _heartbeat()
        time.sleep(10)

if __name__ == "__main__":
    main()
