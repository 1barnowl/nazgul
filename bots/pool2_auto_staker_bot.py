#!/usr/bin/env python3
"""
pool2_auto_staker_bot.py — Pool‑2 Auto‑Staker Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Takes LP tokens from your wallet, stakes them into a
MasterChef‑style farm, harvests the reward token,
swaps them for more LP tokens, and re‑stakes automatically.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `pool2_autostaker_config.json` in the same directory:

{
  "rpc_url": "https://mainnet.infura.io/v3/YOUR_KEY",
  "chain_id": 1,
  "private_key": "0xYOUR_PRIVATE_KEY",
  "dry_run": false,
  "pools": [
    {
      "farm_address": "0xMasterChef...",
      "pid": 0,
      "pair_address": "0xLPtoken...",
      "reward_token": "0xRewardToken...",
      "router_address": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
      "token0_path": ["0xRewardToken...", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "0xToken0..."],
      "token1_path": ["0xToken0...", "0xToken1..."],
      "min_reward": "10.0",
      "slippage": 0.01,
      "gas_limit": 500000,
      "gas_price_multiplier": 1.1
    }
  ],
  "poll_interval_seconds": 3600,
  "state_file": "pool2_autostaker_state.json",
  "heartbeat_interval": 30
}
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
BOT_ID = "pool2_auto_staker_bot"
BOT_NAME = "Pool‑2 Auto‑Staker"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "pool2_autostaker_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(
            f"{HUB}/ingest",
            json={
                "bot_id": BOT_ID,
                "bot_name": BOT_NAME,
                "summary": summary,
                "level": level,
                "payload": payload or {},
            },
            timeout=5,
        )
    except Exception:
        pass

def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(
            f"{HUB}/heartbeat/{BOT_ID}",
            json={"bot_name": BOT_NAME, "status": "online"},
            timeout=3,
        )
    except Exception:
        pass
    _last_hb = time.time()

# ── ABIs (minimal) ───────────────────────────────────────────────
MASTERCHEF_ABI = json.loads(
    '''[
    {"constant":false,"inputs":[{"name":"_pid","type":"uint256"},{"name":"_amount","type":"uint256"}],"name":"deposit","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"constant":false,"inputs":[{"name":"_pid","type":"uint256"},{"name":"_amount","type":"uint256"}],"name":"withdraw","outputs":[],"stateMutability":"nonpayable","type":"function"}
]'''
)  # deposit(pid, amount) often claims pending rewards when amount > 0, and deposit(pid,0) claims rewards only

UNISWAP_V2_ROUTER_ABI = json.loads(
    '''[
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"uint256","name":"amountADesired","type":"uint256"},{"internalType":"uint256","name":"amountBDesired","type":"uint256"},{"internalType":"uint256","name":"amountAMin","type":"uint256"},{"internalType":"uint256","name":"amountBMin","type":"uint256"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"addLiquidity","outputs":[{"internalType":"uint256","name":"amountA","type":"uint256"},{"internalType":"uint256","name":"amountB","type":"uint256"},{"internalType":"uint256","name":"liquidity","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"}
]'''
)

ERC20_ABI = json.loads(
    '''[
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"}
]'''
)

# ── Utility functions ────────────────────────────────────────────
def approve_if_needed(w3, token_addr, spender, account, amount, gas_price, gas_limit, private_key):
    token = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    allowance = token.functions.allowance(account.address, spender).call()
    if allowance >= amount:
        return True
    try:
        tx = token.functions.approve(spender, amount).build_transaction(
            {
                "from": account.address,
                "gas": 100000,
                "gasPrice": gas_price,
                "nonce": w3.eth.get_transaction_count(account.address),
            }
        )
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return receipt.status == 1
    except Exception as e:
        _post(f"Approval error: {e}", "error")
        return False

def swap_tokens(w3, router_addr, path, amount_in, min_out, account, private_key, gas_price, gas_limit):
    router = w3.eth.contract(address=router_addr, abi=UNISWAP_V2_ROUTER_ABI)
    deadline = int(time.time()) + 300
    try:
        tx = router.functions.swapExactTokensForTokens(
            amount_in, min_out, path, account.address, deadline
        ).build_transaction(
            {
                "from": account.address,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "nonce": w3.eth.get_transaction_count(account.address),
            }
        )
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return receipt.status == 1
    except Exception as e:
        _post(f"Swap error: {e}", "error")
        return False

def add_liquidity_and_stake(w3, router_addr, token0, token1, amount0, amount1,
                            min0, min1, farm_addr, pid, account, private_key,
                            gas_price, gas_limit, slippage):
    router = w3.eth.contract(address=router_addr, abi=UNISWAP_V2_ROUTER_ABI)
    deadline = int(time.time()) + 300
    # Add liquidity
    try:
        tx = router.functions.addLiquidity(
            token0, token1, amount0, amount1,
            min0, min1, account.address, deadline
        ).build_transaction({
            "from": account.address,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(account.address),
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

    # Stake obtained LP tokens
    # We assume the LP token is the same as the pair address (pair_address in config)
    # But we can get the LP token address from the farm's poolInfo if needed. For simplicity,
    # we'll rely on the fact that after addLiquidity, the LP token balance of the user will increase.
    # We'll use the pair_address from the pool config as the LP token.
    lp_token_addr = token0  # actually pair_address; we'll pass it separately
    # We'll fetch from the caller context.
    # We'll handle that outside this function.
    # So we skip staking here and do it in the main loop.
    return True

# ── Main worker ──────────────────────────────────────────────────
def process_pool(w3, account, private_key, pool_cfg: dict, dry_run: bool):
    farm_addr = Web3.to_checksum_address(pool_cfg["farm_address"])
    pid = int(pool_cfg["pid"])
    pair_addr = Web3.to_checksum_address(pool_cfg["pair_address"])
    reward_token_addr = Web3.to_checksum_address(pool_cfg["reward_token"])
    router_addr = Web3.to_checksum_address(pool_cfg["router_address"])
    min_reward = float(pool_cfg.get("min_reward", 0.0))
    slippage = float(pool_cfg.get("slippage", 0.01))
    gas_limit = int(pool_cfg.get("gas_limit", 400000))
    gas_mult = float(pool_cfg.get("gas_price_multiplier", 1.1))
    gas_price = int(w3.eth.gas_price * gas_mult)

    # Token contracts
    pair_token = w3.eth.contract(address=pair_addr, abi=ERC20_ABI)
    reward_token = w3.eth.contract(address=reward_token_addr, abi=ERC20_ABI)

    # 1. Stake any existing LP tokens the user holds
    lp_balance = pair_token.functions.balanceOf(account.address).call()
    if lp_balance > 0:
        _post(f"Staking {lp_balance / 10**18:.4f} LP tokens (pool {pid})", "info")
        if not dry_run:
            if not approve_if_needed(w3, pair_addr, farm_addr, account, lp_balance, gas_price, gas_limit, private_key):
                _post("LP approve failed", "error")
                return
            farm = w3.eth.contract(address=farm_addr, abi=MASTERCHEF_ABI)
            try:
                tx = farm.functions.deposit(pid, lp_balance).build_transaction({
                    "from": account.address,
                    "gas": gas_limit,
                    "gasPrice": gas_price,
                    "nonce": w3.eth.get_transaction_count(account.address),
                })
                signed = account.sign_transaction(tx)
                tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                if receipt.status != 1:
                    _post("LP deposit failed", "error")
                else:
                    _post("LP staked successfully", "info")
            except Exception as e:
                _post(f"Deposit error: {e}", "error")

    # 2. Harvest pending rewards via deposit(pid, 0)
    _post(f"Harvesting rewards for pool {pid}", "info")
    if not dry_run:
        farm = w3.eth.contract(address=farm_addr, abi=MASTERCHEF_ABI)
        try:
            tx = farm.functions.deposit(pid, 0).build_transaction({
                "from": account.address,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "nonce": w3.eth.get_transaction_count(account.address),
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status != 1:
                _post("Harvest failed", "error")
                return
        except Exception as e:
            _post(f"Harvest error: {e}", "error")
            return

    # 3. Check reward token balance
    reward_balance = reward_token.functions.balanceOf(account.address).call()
    reward_dec = reward_token.functions.decimals().call()
    reward_human = reward_balance / (10 ** reward_dec)
    _post(f"Reward token balance: {reward_human:.4f} (pool {pid})", "info")

    if reward_balance < int(min_reward * (10 ** reward_dec)):
        _post("Reward below threshold, skipping compound", "info")
        return

    # 4. Compound: swap reward → token0, then half token0 → token1, add liquidity, stake
    # Retrieve token0 and token1 from pair contract (fetch only once per config? we can query)
    pair_contract = w3.eth.contract(address=pair_addr, abi=json.loads('''[
        {"constant":true,"inputs":[],"name":"token0","outputs":[{"name":"","type":"address"}],"type":"function"},
        {"constant":true,"inputs":[],"name":"token1","outputs":[{"name":"","type":"address"}],"type":"function"}
    ]'''))
    token0_addr = pair_contract.functions.token0().call()
    token1_addr = pair_contract.functions.token1().call()
    token0 = w3.eth.contract(address=token0_addr, abi=ERC20_ABI)
    token1 = w3.eth.contract(address=token1_addr, abi=ERC20_ABI)

    # Determine swap paths (config can override)
    token0_path = pool_cfg.get("token0_path")
    token1_path = pool_cfg.get("token1_path")

    # Approve router for reward token
    if not dry_run:
        if not approve_if_needed(w3, reward_token_addr, router_addr, account, reward_balance, gas_price, gas_limit, private_key):
            _post("Reward token approve failed", "error")
            return

    # --- Swap all reward to token0 ---
    # If no custom path, we try direct [reward, token0]
    if not token0_path:
        token0_path = [reward_token_addr, token0_addr]
    else:
        # Ensure addresses are checksummed
        token0_path = [Web3.to_checksum_address(a) for a in token0_path]
        # Make sure it starts with reward_token and ends with token0 (user must have provided correctly)
    router = w3.eth.contract(address=router_addr, abi=UNISWAP_V2_ROUTER_ABI)
    try:
        amounts_out = router.functions.getAmountsOut(reward_balance, token0_path).call()
    except Exception as e:
        _post(f"Swap quote failed for reward->token0: {e}", "error")
        return
    min_out0 = int(amounts_out[-1] * (1 - slippage))

    if not dry_run:
        if not swap_tokens(w3, router_addr, token0_path, reward_balance, min_out0, account, private_key, gas_price, gas_limit):
            _post("Swap reward->token0 failed", "error")
            return

    # --- Now split token0: half to token1 ---
    token0_balance = token0.functions.balanceOf(account.address).call()
    if token0_balance == 0:
        _post("No token0 after swap", "error")
        return

    half0 = token0_balance // 2
    if half0 <= 0:
        _post("Insufficient token0 to split", "error")
        return

    # Determine path for token0 -> token1
    if not token1_path:
        token1_path = [token0_addr, token1_addr]
    else:
        token1_path = [Web3.to_checksum_address(a) for a in token1_path]

    if not dry_run:
        if not approve_if_needed(w3, token0_addr, router_addr, account, half0, gas_price, gas_limit, private_key):
            _post("Token0 approve failed", "error")
            return

    try:
        amounts_out1 = router.functions.getAmountsOut(half0, token1_path).call()
    except Exception as e:
        _post(f"Swap quote failed for token0->token1: {e}", "error")
        return
    min_out1 = int(amounts_out1[-1] * (1 - slippage))

    if not dry_run:
        if not swap_tokens(w3, router_addr, token1_path, half0, min_out1, account, private_key, gas_price, gas_limit):
            _post("Swap token0->token1 failed", "error")
            return

    # --- Add liquidity ---
    bal0 = token0.functions.balanceOf(account.address).call()
    bal1 = token1.functions.balanceOf(account.address).call()
    if bal0 == 0 or bal1 == 0:
        _post("Zero balance for one of the tokens", "error")
        return

    # Approve router for both
    if not dry_run:
        if not approve_if_needed(w3, token0_addr, router_addr, account, bal0, gas_price, gas_limit, private_key):
            return
        if not approve_if_needed(w3, token1_addr, router_addr, account, bal1, gas_price, gas_limit, private_key):
            return

    deadline = int(time.time()) + 300
    min0 = int(bal0 * (1 - slippage))
    min1 = int(bal1 * (1 - slippage))
    if not dry_run:
        try:
            tx = router.functions.addLiquidity(
                token0_addr, token1_addr, bal0, bal1,
                min0, min1, account.address, deadline
            ).build_transaction({
                "from": account.address,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "nonce": w3.eth.get_transaction_count(account.address),
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status != 1:
                _post("Add liquidity failed", "error")
                return
        except Exception as e:
            _post(f"addLiquidity error: {e}", "error")
            return

    # --- Stake new LP tokens ---
    lp_new = pair_token.functions.balanceOf(account.address).call()
    if lp_new > 0 and not dry_run:
        if not approve_if_needed(w3, pair_addr, farm_addr, account, lp_new, gas_price, gas_limit, private_key):
            _post("LP approve for stake failed", "error")
            return
        try:
            tx = farm.functions.deposit(pid, lp_new).build_transaction({
                "from": account.address,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "nonce": w3.eth.get_transaction_count(account.address),
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                _post(f"Compound successful – staked {lp_new / 10**18:.4f} LP tokens", "info")
            else:
                _post("Staking LP after compound failed", "error")
        except Exception as e:
            _post(f"Stake error: {e}", "error")
    else:
        _post("Compound complete (no additional LP tokens to stake)", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Pool‑2 Auto‑Staker Bot online")
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
        if not private_key and not dry_run:
            _post("Private key missing", "error")
            time.sleep(3600)
            continue

        account = None
        if private_key and not dry_run:
            try:
                account = w3.eth.account.from_key(private_key)
            except Exception as e:
                _post(f"Invalid private key: {e}", "error")
                time.sleep(3600)
                continue

        pools = config.get("pools", [])
        for pool_cfg in pools:
            try:
                process_pool(w3, account, private_key, pool_cfg, dry_run)
            except Exception as e:
                _post(f"Error processing pool {pool_cfg.get('pid')}: {e}", "error")

        poll_sec = int(config.get("poll_interval_seconds", 3600))
        _heartbeat()
        time.sleep(poll_sec)

if __name__ == "__main__":
    main()
