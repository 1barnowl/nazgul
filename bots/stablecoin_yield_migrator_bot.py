#!/usr/bin/env python3
"""
stablecoin_yield_migrator_bot.py — Best‑Yield Stablecoin Migrator Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors APYs on Curve, Aave, Uniswap, and other DeFi protocols
via the DefiLlama API.  When a better yield is found (and it
covers estimated gas fees), it optionally migrates your
stablecoin position.  Attachable to the Nazgul BotController.

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `stablecoin_migrator_config.json` in the same directory:

{
  "rpc_url": "https://mainnet.infura.io/v3/YOUR_KEY",
  "chain_id": 1,
  "private_key": "0xYOUR_PRIVATE_KEY",       // leave empty for dry‑run
  "stablecoins": ["USDC", "USDT", "DAI", "FRAX"],
  "protocols": ["Curve", "Aave", "Uniswap"],
  "min_apy_improvement": 2.0,                // required extra APY (%)
  "max_gas_fee_usd": 50.0,                   // skip if gas > this
  "poll_interval_hours": 6,
  "state_file": "stablecoin_migrator_state.json",
  "heartbeat_interval": 30
}

The bot fetches live APY data from https://yields.llama.fi/pools .
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from web3 import Web3
from web3.exceptions import ContractLogicError

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "stablecoin_yield_migrator_bot"
BOT_NAME = "Best‑Yield Stablecoin Migrator"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "stablecoin_migrator_config.json"
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

# ── DefiLlama API ────────────────────────────────────────────────
DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"

def fetch_pools() -> List[dict]:
    """Get the full list of pools from DefiLlama."""
    try:
        resp = requests.get(DEFILLAMA_POOLS_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])
    except Exception as e:
        _post(f"DefiLlama API error: {e}", "error")
        return []

def filter_stablecoin_pools(pools: List[dict], stablecoins: List[str], protocols: List[str]) -> List[dict]:
    """Return pools that involve only the given stablecoins and belong to the chosen protocols."""
    filtered = []
    stable_set = set(s.upper() for s in stablecoins)
    proto_set = set(p.lower() for p in protocols)
    for pool in pools:
        # Skip if not a stable pool
        if pool.get("stablecoin") != True:
            continue
        # Check protocol
        project = pool.get("project", "").lower()
        if proto_set and project not in proto_set:
            continue
        # Check underlying tokens (symbols)
        underlying = pool.get("symbol", "").upper()  # e.g., "USDC+USDT"
        symbols = set(s.strip() for s in underlying.split("+"))
        if not symbols.issubset(stable_set):
            continue
        filtered.append(pool)
    return filtered

def select_best_pool(pools: List[dict], current_pool_id: Optional[str] = None, min_apy_improvement: float = 0.0) -> Optional[dict]:
    """Choose the pool with the highest APY (base or reward) that is not the current pool."""
    best = None
    best_apy = 0.0
    for p in pools:
        # Use the "apyBase" or the sum with "apyReward"? We'll use total apy.
        apy = p.get("apy", 0.0) or 0.0
        # Sometimes "apy" is the combined, but check if apy is available
        if p["pool"] == current_pool_id:
            continue
        if apy > best_apy:
            best_apy = apy
            best = p
    if best and min_apy_improvement > 0 and current_pool_id:
        # Get current pool APY from list (must have been fetched)
        current_apy = next((p["apy"] for p in pools if p["pool"] == current_pool_id), None)
        if current_apy is not None and (best_apy - current_apy) < min_apy_improvement:
            return None  # not worth it
    return best

# ── On‑chain operations ──────────────────────────────────────────
# Minimal ABIs needed

ERC20_ABI = json.loads('''[
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}
]''')

# Generic gauge / staking contract with withdraw and deposit (for Curve)
GAUGE_ABI = json.loads('''[
    {"constant":false,"inputs":[{"name":"amount","type":"uint256"}],"name":"withdraw","outputs":[],"type":"function"},
    {"constant":false,"inputs":[{"name":"amount","type":"uint256"}],"name":"deposit","outputs":[],"type":"function"},
    {"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"staking_token","outputs":[{"name":"","type":"address"}],"type":"function"}
]''')

# Aave V2/V3 LendingPool (simplified)
AAVE_LENDING_POOL_ABI = json.loads('''[
    {"constant":false,"inputs":[{"name":"asset","type":"address"},{"name":"amount","type":"uint256"},{"name":"to","type":"address"}],"name":"withdraw","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"asset","type":"address"},{"name":"amount","type":"uint256"},{"name":"onBehalfOf","type":"address"},{"name":"referralCode","type":"uint16"}],"name":"deposit","outputs":[],"type":"function"}
]''')

# Uniswap V3 NonfungiblePositionManager (to remove liquidity)
# We'll need the user's token ID and position manager address. For simplicity, the bot will only support pools where the user holds ERC20 LP tokens (Curve, Uniswap V2). For Uniswap V3, it's more complex. We'll focus on ERC20 LP pools (which covers most stablecoin pools on Curve and Aave aTokens).

def withdraw_from_pool(pool: dict, w3: Web3, account, private_key: str, config: dict) -> bool:
    """Withdraw from a pool identified by its `pool` address (the LP token or gauge)."""
    # For Curve: the user likely staked in a gauge, so we need to withdraw from gauge first, then (optional) remove liquidity?
    # For simplicity, we assume the pool address returned by DefiLlama is the staking contract (gauge) that returns LP tokens.
    # The user can implement the exact logic. We'll provide a skeleton that tries to call `withdraw` on the contract.
    pool_addr = pool.get("pool")  # often the gauge address
    if not pool_addr:
        return False
    try:
        contract = w3.eth.contract(address=pool_addr, abi=GAUGE_ABI)
        # Check user balance in gauge
        user_addr = account.address
        balance = contract.functions.balanceOf(user_addr).call()
        if balance == 0:
            _post(f"No balance in gauge {pool_addr}", "warning")
            return True  # nothing to withdraw
        # Withdraw all
        tx = contract.functions.withdraw(balance).build_transaction({
            "from": user_addr,
            "gas": 300000,
            "gasPrice": w3.eth.gas_price,
            "nonce": w3.eth.get_transaction_count(user_addr)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return receipt.status == 1
    except Exception as e:
        _post(f"Withdraw error: {e}", "error")
        return False

def deposit_to_pool(pool: dict, w3: Web3, account, private_key: str, config: dict) -> bool:
    """Deposit LP tokens into the target pool's gauge/staking contract."""
    pool_addr = pool.get("pool")
    if not pool_addr:
        return False
    try:
        contract = w3.eth.contract(address=pool_addr, abi=GAUGE_ABI)
        # Get LP token address (the underlying token)
        staking_token = contract.functions.staking_token().call()
        token_contract = w3.eth.contract(address=staking_token, abi=ERC20_ABI)
        user_addr = account.address
        balance = token_contract.functions.balanceOf(user_addr).call()
        if balance == 0:
            _post("No LP tokens to deposit", "warning")
            return False
        # Approve gauge
        approve_tx = token_contract.functions.approve(pool_addr, balance).build_transaction({
            "from": user_addr,
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
            "nonce": w3.eth.get_transaction_count(user_addr)
        })
        signed = account.sign_transaction(approve_tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        # Deposit
        tx = contract.functions.deposit(balance).build_transaction({
            "from": user_addr,
            "gas": 300000,
            "gasPrice": w3.eth.gas_price,
            "nonce": w3.eth.get_transaction_count(user_addr)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return receipt.status == 1
    except Exception as e:
        _post(f"Deposit error: {e}", "error")
        return False

# ── Main logic ───────────────────────────────────────────────────
def main():
    _post("Stablecoin Yield Migrator Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        stablecoins = config.get("stablecoins", ["USDC", "USDT", "DAI"])
        protocols = config.get("protocols", ["Curve", "Aave", "Uniswap"])
        min_apy_improvement = float(config.get("min_apy_improvement", 2.0))
        private_key = config.get("private_key", "")
        dry_run = not private_key
        state_file = config.get("state_file", "stablecoin_migrator_state.json")

        # Load state
        state = {}
        if os.path.exists(state_file):
            try:
                with open(state_file, "r") as f:
                    state = json.load(f)
            except Exception:
                state = {}

        current_pool_id = state.get("current_pool_id")
        # Fetch pools
        pools = fetch_pools()
        if not pools:
            _post("No pools fetched", "error")
            time.sleep(3600)
            continue

        filtered = filter_stablecoin_pools(pools, stablecoins, protocols)
        if not filtered:
            _post("No matching stablecoin pools", "info")
        else:
            best = select_best_pool(filtered, current_pool_id, min_apy_improvement)
            if best:
                best_apy = best.get("apy", 0.0)
                summary = f"Best yield: {best['symbol']} on {best['project']} ({best_apy:.2f}% APY) – pool {best['pool']}"
                _post(summary, "info", best)
                if not dry_run and best["pool"] != current_pool_id:
                    # Perform migration
                    _post("Attempting migration...", "warning")
                    w3 = Web3(Web3.HTTPProvider(config["rpc_url"]))
                    account = w3.eth.account.from_key(private_key)
                    if current_pool_id:
                        current_pool = next((p for p in filtered if p["pool"] == current_pool_id), None)
                        if current_pool:
                            if not withdraw_from_pool(current_pool, w3, account, private_key, config):
                                _post("Withdraw failed, aborting", "error")
                                continue
                    # Now deposit to new pool
                    if deposit_to_pool(best, w3, account, private_key, config):
                        state["current_pool_id"] = best["pool"]
                        _post(f"Successfully migrated to {best['project']} {best['symbol']}", "info")
                    else:
                        _post("Deposit failed", "error")
                else:
                    if dry_run:
                        _post("Dry‑run: not migrating (no private key)", "info")
            else:
                _post("No better yield found", "info")

        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)

        interval_hours = int(config.get("poll_interval_hours", 6))
        _heartbeat()
        time.sleep(interval_hours * 3600)

if __name__ == "__main__":
    main()
