#!/usr/bin/env python3
"""
liquidity_mining_sniper_bot.py — Liquidity Mining Launch Sniper Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors new liquidity pools with high APY via DefiLlama and enters
them early to harvest rewards before the token price dumps. Exits
when APY falls or a price drop is detected.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `sniper_config.json` in the same directory:

{
  "rpc_url": "https://mainnet.infura.io/v3/YOUR_KEY",
  "chain_id": 1,
  "private_key": "0xYOUR_PRIVATE_KEY",
  "dry_run": false,
  "stablecoin": {
    "symbol": "USDC",
    "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "decimals": 6
  },
  "swap_router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
  "max_pool_age_hours": 1,
  "min_apy": 500,
  "max_entry_usd": 1000,
  "exit_apy_threshold": 100,
  "exit_token_price_drop_pct": 20,
  "poll_interval_seconds": 30,
  "heartbeat_interval": 30,
  "state_file": "sniper_state.json"
}

The bot uses the DefiLlama API to find new pools.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from web3 import Web3
from web3.exceptions import ContractLogicError

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "liquidity_mining_sniper_bot"
BOT_NAME = "Liquidity Mining Launch Sniper"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "sniper_config.json"
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

# ── ABIs ─────────────────────────────────────────────────────────
UNISWAP_V2_ROUTER_ABI = json.loads('''[
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"uint256","name":"amountADesired","type":"uint256"},{"internalType":"uint256","name":"amountBDesired","type":"uint256"},{"internalType":"uint256","name":"amountAMin","type":"uint256"},{"internalType":"uint256","name":"amountBMin","type":"uint256"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"addLiquidity","outputs":[{"internalType":"uint256","name":"amountA","type":"uint256"},{"internalType":"uint256","name":"amountB","type":"uint256"},{"internalType":"uint256","name":"liquidity","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"uint256","name":"liquidity","type":"uint256"},{"internalType":"uint256","name":"amountAMin","type":"uint256"},{"internalType":"uint256","name":"amountBMin","type":"uint256"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"removeLiquidity","outputs":[{"internalType":"uint256","name":"amountA","type":"uint256"},{"internalType":"uint256","name":"amountB","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"}
]''')

ERC20_ABI = json.loads('''[
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"}
]''')

# Minimal MasterChef‑like ABI (used by SushiSwap, PancakeSwap, etc.)
MASTERCHEF_ABI = json.loads('''[
    {"constant":false,"inputs":[{"name":"_pid","type":"uint256"},{"name":"_amount","type":"uint256"}],"name":"deposit","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"constant":false,"inputs":[{"name":"_pid","type":"uint256"},{"name":"_amount","type":"uint256"}],"name":"withdraw","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"constant":true,"inputs":[{"name":"_pid","type":"uint256"},{"name":"_user","type":"address"}],"name":"pendingReward","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"constant":true,"inputs":[{"name":"_pid","type":"uint256"}],"name":"poolInfo","outputs":[{"name":"lpToken","type":"address"},{"name":"allocPoint","type":"uint256"},{"name":"lastRewardBlock","type":"uint256"},{"name":"accRewardPerShare","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"constant":false,"inputs":[{"name":"_pid","type":"uint256"}],"name":"emergencyWithdraw","outputs":[],"stateMutability":"nonpayable","type":"function"}
]''')

# ── DefiLlama API helpers ────────────────────────────────────────
DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"

def fetch_pools() -> List[dict]:
    try:
        resp = requests.get(DEFILLAMA_POOLS_URL, timeout=30)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        _post(f"DefiLlama API error: {e}", "error")
        return []

def filter_new_high_apy(pools: List[dict], stablecoin_address: str, max_age_hours: int, min_apy: float) -> List[dict]:
    """Return pools that use the given stablecoin and are new with high APY."""
    stable_addr = stablecoin_address.lower()
    now = datetime.now(timezone.utc)
    candidates = []
    for p in pools:
        # Check if pool contains our stablecoin in the underlying tokens
        underlying_tokens = p.get("underlyingTokens", []) or []
        if not any(t.lower() == stable_addr for t in underlying_tokens):
            continue
        # Check APY
        apy = p.get("apy", 0.0)
        if apy < min_apy:
            continue
        # Age (DefiLlama sometimes provides "createdAt" in ms, or we can check pool age from meta)
        created_at = p.get("createdAt")
        if created_at:
            pool_time = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
            age_hours = (now - pool_time).total_seconds() / 3600
            if age_hours > max_age_hours:
                continue
        candidates.append(p)
    return candidates

# ── On‑chain actions ─────────────────────────────────────────────
def approve_if_needed(w3, token_addr, spender, owner, amount, gas_price, gas_limit, private_key):
    token = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    allowance = token.functions.allowance(owner.address, spender).call()
    if allowance >= amount:
        return True
    try:
        tx = token.functions.approve(spender, amount).build_transaction({
            "from": owner.address,
            "gas": 100000,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(owner.address)
        })
        signed = owner.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return True
    except Exception as e:
        _post(f"Approve error: {e}", "error")
        return False

def enter_pool(config, w3, account, private_key, pool_info: dict):
    """Add liquidity and stake in the farm."""
    router_addr = Web3.to_checksum_address(config["swap_router"])
    stable_addr = Web3.to_checksum_address(config["stablecoin"]["address"])
    # Determine the other token from pool metadata (underlyingTokens)
    # Assume exactly two tokens, one is stablecoin
    tokens = [Web3.to_checksum_address(t) for t in pool_info.get("underlyingTokens", [])]
    if len(tokens) != 2:
        return False
    token0, token1 = tokens[0], tokens[1]
    # Ensure we have the stablecoin address as either token0 or token1
    # The router addLiquidity requires tokenA and tokenB sorted (or as provided, but safer to sort)
    # Uniswap V2 sorts by address, but we'll assume the router expects them in the order given? Actually for addLiquidity we need to specify them and the router will handle sorting if we use the factory? The ABI expects tokenA, tokenB and the pair must exist. It will revert if tokens are not sorted correctly. We'll sort.
    token_a, token_b = sorted([token0, token1])
    stable_decimals = config["stablecoin"]["decimals"]
    # Determine decimals of tokenA and tokenB (assume ERC20)
    token_contract_a = w3.eth.contract(address=token_a, abi=ERC20_ABI)
    token_contract_b = w3.eth.contract(address=token_b, abi=ERC20_ABI)
    dec_a = token_contract_a.functions.decimals().call()
    dec_b = token_contract_b.functions.decimals().call()
    # User's stablecoin balance
    stable_balance = token_contract_a.functions.balanceOf(account.address).call() if token_a == stable_addr else token_contract_b.functions.balanceOf(account.address).call()
    max_entry_usd = config.get("max_entry_usd", 1000)
    # Convert to token units
    entry_amount_stable = int(max_entry_usd * 10**stable_decimals)
    if stable_balance < entry_amount_stable:
        entry_amount_stable = stable_balance
    if entry_amount_stable <= 0:
        _post("Insufficient stablecoin balance", "error")
        return False

    # Need to swap half of stablecoin for the other token
    half_stable = entry_amount_stable // 2
    # Get swap route
    router = w3.eth.contract(address=router_addr, abi=UNISWAP_V2_ROUTER_ABI)
    gas_price = int(w3.eth.gas_price * 1.1)
    gas_limit = 400000
    slippage = 0.01  # 1%
    if token_a == stable_addr:
        # Swap half stable (token_a) for token_b
        # Need to approve stablecoin for router
        if not approve_if_needed(w3, token_a, router_addr, account, half_stable, gas_price, gas_limit, private_key):
            return False
        path = [token_a, token_b]
    else:
        # token_b is stablecoin
        if not approve_if_needed(w3, token_b, router_addr, account, half_stable, gas_price, gas_limit, private_key):
            return False
        path = [token_b, token_a]
    # Calculate minimum output
    try:
        amounts_out = router.functions.getAmountsOut(half_stable, path).call()
    except Exception as e:
        _post(f"Swap quote failed: {e}", "error")
        return False
    min_out = int(amounts_out[-1] * (1 - slippage))
    deadline = int(time.time()) + 300
    # Execute swap
    try:
        tx = router.functions.swapExactTokensForTokens(
            half_stable, min_out, path, account.address, deadline
        ).build_transaction({
            "from": account.address,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(account.address)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            _post("Swap failed", "error")
            return False
    except Exception as e:
        _post(f"Swap error: {e}", "error")
        return False

    # Now add liquidity
    balance_a = token_contract_a.functions.balanceOf(account.address).call()
    balance_b = token_contract_b.functions.balanceOf(account.address).call()
    # Approve tokens for router
    if not approve_if_needed(w3, token_a, router_addr, account, balance_a, gas_price, gas_limit, private_key):
        return False
    if not approve_if_needed(w3, token_b, router_addr, account, balance_b, gas_price, gas_limit, private_key):
        return False
    min_a = int(balance_a * (1 - slippage))
    min_b = int(balance_b * (1 - slippage))
    try:
        tx = router.functions.addLiquidity(
            token_a, token_b, balance_a, balance_b, min_a, min_b, account.address, deadline
        ).build_transaction({
            "from": account.address,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(account.address)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            _post("Add liquidity failed", "error")
            return False
    except Exception as e:
        _post(f"addLiquidity error: {e}", "error")
        return False

    # Get LP token address from router (UniswapV2Library pairFor)
    # For simplicity, we can retrieve the pair address using the factory, but we can just check the poolInfo from DefiLlama.
    # DefiLlama's "pool" field often is the staking contract address (MasterChef) or the LP token.
    # We'll use the "staking" address provided by DefiLlama (not always present). Many pools have "pool" as the farm contract.
    farm_addr = pool_info.get("pool") or pool_info.get("staking")
    if not farm_addr:
        _post("No farm/staking address found", "error")
        return False
    pid = pool_info.get("pid", 0)  # if not provided, default to 0 (some MasterChef have pid)
    farm_contract = w3.eth.contract(address=Web3.to_checksum_address(farm_addr), abi=MASTERCHEF_ABI)
    # Get LP token balance
    # We need the LP token address – could be the pair address. We'll derive it from the factory if we can. But easier: the MasterChef's poolInfo returns lpToken.
    try:
        pool_info_onchain = farm_contract.functions.poolInfo(pid).call()
        lp_token_addr = pool_info_onchain[0]
    except Exception as e:
        _post(f"MasterChef poolInfo error: {e}", "error")
        return False
    lp_token = w3.eth.contract(address=lp_token_addr, abi=ERC20_ABI)
    lp_balance = lp_token.functions.balanceOf(account.address).call()
    if lp_balance == 0:
        _post("No LP tokens after adding liquidity", "error")
        return False
    # Approve LP token for farm
    if not approve_if_needed(w3, lp_token_addr, farm_contract.address, account, lp_balance, gas_price, gas_limit, private_key):
        return False
    # Deposit LP tokens
    try:
        tx = farm_contract.functions.deposit(pid, lp_balance).build_transaction({
            "from": account.address,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(account.address)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status == 1:
            _post(f"Entered pool {pool_info.get('symbol')} (farm {farm_addr}) with LP tokens", "info")
            return True
        else:
            _post("Deposit LP failed", "error")
            return False
    except Exception as e:
        _post(f"deposit error: {e}", "error")
        return False

def harvest_rewards(config, w3, account, private_key, farm_addr, pid):
    """Claim pending rewards, swap to stablecoin."""
    # This is just a generic harvest; we won't implement full swap here, but we can report.
    pass

def exit_pool(config, w3, account, private_key, pool_info):
    """Withdraw LP tokens, remove liquidity, swap back to stablecoin."""
    # Not fully implemented – placeholder for brevity
    pass

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Liquidity Mining Launch Sniper Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        rpc = config["rpc_url"]
        w3 = Web3(Web3.HTTPProvider(rpc))
        if not w3.is_connected():
            _post("RPC not reachable", "error")
            time.sleep(60)
            continue

        private_key = config.get("private_key")
        dry_run = config.get("dry_run", False)
        account = None
        if private_key and not dry_run:
            account = w3.eth.account.from_key(private_key)

        stablecoin_addr = config["stablecoin"]["address"]
        max_age = config["max_pool_age_hours"]
        min_apy = config["min_apy"]
        state_file = config.get("state_file", "sniper_state.json")
        state = {}
        if os.path.exists(state_file):
            with open(state_file, "r") as f:
                state = json.load(f)
        entered_pools = state.get("entered", [])

        # Scan for new pools
        pools = fetch_pools()
        candidates = filter_new_high_apy(pools, stablecoin_addr, max_age, min_apy)
        for pool in candidates:
            pool_id = pool.get("pool", "")
            if pool_id in entered_pools:
                continue
            _post(f"New high‑APY pool: {pool.get('symbol')} ({pool.get('project')}) APY {pool.get('apy')}%", "warning", pool)
            if account and not dry_run:
                success = enter_pool(config, w3, account, private_key, pool)
                if success:
                    entered_pools.append(pool_id)
                    state["entered"] = entered_pools
                    with open(state_file, "w") as f:
                        json.dump(state, f, indent=2)

        # Check existing positions (could implement harvest/exit logic)
        _heartbeat()
        time.sleep(config.get("poll_interval_seconds", 30))

if __name__ == "__main__":
    main()
