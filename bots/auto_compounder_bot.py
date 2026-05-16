#!/usr/bin/env python3
"""
auto_compounder_bot.py — Auto‑Compounder Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Claims liquidity mining rewards, swaps them into
the LP token pair, and re‑stakes the new LP tokens
to steadily increase your share.  Runs on EVM chains.

Attachable to the Nazgul BotController.

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `auto_compounder_config.json` in the same directory:

{
  "rpc_url": "https://mainnet.infura.io/v3/YOUR_KEY",
  "chain_id": 1,
  "private_key": "0xYOUR_PRIVATE_KEY",
  "staking_contract": "0xStakingContractAddress",
  "reward_token": "0xRewardTokenAddress",
  "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
  "lp_token": "0xLpTokenAddress",
  "token_a": "0xToken0Address",
  "token_b": "0xToken1Address",
  "max_slippage_pct": 2.0,
  "min_reward_to_claim": 0.01,
  "gas_limit": 500000,
  "gas_price_multiplier": 1.1,
  "poll_interval_seconds": 600,
  "heartbeat_interval": 30
}

The staking contract must provide:
  - earned(address account) → uint256 (reward token balance)
  - getReward() (claims rewards)
  - stake(uint256 amount) (stakes LP tokens)
The LP token must be the pair token from the factory.
The router is a UniswapV2‑style contract (0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D on Ethereum mainnet).
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from web3 import Web3
from web3.exceptions import ContractLogicError, TimeExhausted

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "auto_compounder_bot"
BOT_NAME = "Auto‑Compounder"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "auto_compounder_config.json"
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

# ── ABIs (minimal) ───────────────────────────────────────────────
STAKING_ABI = json.loads('''[
    {"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"earned","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[],"name":"getReward","outputs":[],"type":"function"},
    {"constant":false,"inputs":[{"name":"amount","type":"uint256"}],"name":"stake","outputs":[],"type":"function"}
]''')

ROUTER_ABI = json.loads('''[
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"uint256","name":"amountADesired","type":"uint256"},{"internalType":"uint256","name":"amountBDesired","type":"uint256"},{"internalType":"uint256","name":"amountAMin","type":"uint256"},{"internalType":"uint256","name":"amountBMin","type":"uint256"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"addLiquidity","outputs":[{"internalType":"uint256","name":"amountA","type":"uint256"},{"internalType":"uint256","name":"amountB","type":"uint256"},{"internalType":"uint256","name":"liquidity","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}
]''')

ERC20_ABI = json.loads('''[
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"}
]''')

# ── Compound action ─────────────────────────────────────────────
def compound(config: dict) -> Optional[dict]:
    """Perform one full auto‑compound cycle. Return a summary dict or None on failure."""
    rpc = config["rpc_url"]
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        _post("Cannot connect to RPC", "error")
        return None

    account = w3.eth.account.from_key(config["private_key"])
    sender = account.address

    staking = w3.eth.contract(address=config["staking_contract"], abi=STAKING_ABI)
    router = w3.eth.contract(address=config["router"], abi=ROUTER_ABI)
    reward_token = w3.eth.contract(address=config["reward_token"], abi=ERC20_ABI)
    lp_token = w3.eth.contract(address=config["lp_token"], abi=ERC20_ABI)
    token_a = w3.eth.contract(address=config["token_a"], abi=ERC20_ABI)
    token_b = w3.eth.contract(address=config["token_b"], abi=ERC20_ABI)

    # 1. Check earned rewards
    try:
        earned = staking.functions.earned(sender).call()
    except Exception as e:
        _post(f"earned() call failed: {e}", "error")
        return None

    reward_decimals = reward_token.functions.decimals().call()
    min_reward = int(config["min_reward_to_claim"] * 10**reward_decimals)
    if earned < min_reward:
        return {"action": "skip", "earned": earned / 10**reward_decimals, "reason": "below threshold"}

    # 2. Claim rewards
    gas_mult = float(config.get("gas_price_multiplier", 1.1))
    gas_limit = int(config.get("gas_limit", 500000))
    base_fee = w3.eth.gas_price
    gas_price = int(base_fee * gas_mult)

    try:
        tx = staking.functions.getReward().build_transaction({
            "from": sender,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(sender)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            _post(f"Claim reward failed: tx {tx_hash.hex()}", "error")
            return None
    except Exception as e:
        _post(f"Claim reward error: {e}", "error")
        return None

    # 3. Get reward token balance (should be all claimed)
    reward_balance = reward_token.functions.balanceOf(sender).call()

    # 4. Swap half of reward to the other token of the pair
    # We'll swap reward_token -> tokenA if reward_token != tokenA, else to tokenB? Actually simpler: reward_token is typically a single token (e.g., CRV). The LP pair is tokenA/tokenB. Reward token is probably one of them. We need to detect which one it is.
    # Assume reward_token could be either tokenA or tokenB. If it's tokenA, we swap half to tokenB; if it's tokenB, swap half to tokenA.
    # In case reward token is a third token (like a governance token), we would need a more complex path, but we'll assume it's one of the pair.
    reward_addr = config["reward_token"].lower()
    token_a_addr = config["token_a"].lower()
    token_b_addr = config["token_b"].lower()

    if reward_addr == token_a_addr:
        token_from = config["reward_token"]
        token_to = config["token_b"]
    elif reward_addr == token_b_addr:
        token_from = config["reward_token"]
        token_to = config["token_a"]
    else:
        _post("Reward token is neither tokenA nor tokenB – unsupported for simple compound", "error")
        return None

    half = reward_balance // 2
    if half == 0:
        return {"action": "skip", "earned": earned / 10**reward_decimals, "reason": "reward too small to split"}

    # Approve router for reward token
    if not _approve(w3, reward_token, router.address, sender, account, reward_balance, gas_price, gas_limit):
        return None

    # Get min output for swap
    try:
        amounts_out = router.functions.getAmountsOut(half, [token_from, token_to]).call()
    except Exception as e:
        _post(f"getAmountsOut failed: {e}", "error")
        return None
    min_out = int(amounts_out[-1] * (100 - config["max_slippage_pct"]) / 100)
    deadline = int(time.time()) + 300

    # Swap
    try:
        tx = router.functions.swapExactTokensForTokens(
            half, min_out, [token_from, token_to], sender, deadline
        ).build_transaction({
            "from": sender,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(sender)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            _post(f"Swap failed: tx {tx_hash.hex()}", "error")
            return None
    except Exception as e:
        _post(f"Swap error: {e}", "error")
        return None

    # Now we have token_a and token_b balances
    balance_a = token_a.functions.balanceOf(sender).call()
    balance_b = token_b.functions.balanceOf(sender).call()

    if balance_a == 0 or balance_b == 0:
        return {"action": "skip", "reason": "one of the token balances is zero after swap"}

    # Approve router for both tokens
    if not _approve(w3, token_a, router.address, sender, account, balance_a, gas_price, gas_limit):
        return None
    if not _approve(w3, token_b, router.address, sender, account, balance_b, gas_price, gas_limit):
        return None

    # Add liquidity
    amount_a_min = int(balance_a * (100 - config["max_slippage_pct"]) / 100)
    amount_b_min = int(balance_b * (100 - config["max_slippage_pct"]) / 100)
    try:
        tx = router.functions.addLiquidity(
            config["token_a"], config["token_b"],
            balance_a, balance_b,
            amount_a_min, amount_b_min,
            sender, deadline
        ).build_transaction({
            "from": sender,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(sender)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            _post(f"Add liquidity failed: tx {tx_hash.hex()}", "error")
            return None
    except Exception as e:
        _post(f"addLiquidity error: {e}", "error")
        return None

    # 5. Stake LP tokens
    lp_balance = lp_token.functions.balanceOf(sender).call()
    if lp_balance == 0:
        return {"action": "skip", "reason": "no LP tokens received"}

    if not _approve(w3, lp_token, staking.address, sender, account, lp_balance, gas_price, gas_limit):
        return None

    try:
        tx = staking.functions.stake(lp_balance).build_transaction({
            "from": sender,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(sender)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            _post(f"Stake failed: tx {tx_hash.hex()}", "error")
            return None
    except Exception as e:
        _post(f"Stake error: {e}", "error")
        return None

    return {
        "action": "compounded",
        "reward_claimed": earned / 10**reward_decimals,
        "lp_staked": lp_balance / 10**18  # assuming LP token has 18 decimals (usual)
    }

def _approve(w3, token_contract, spender, sender, account, amount, gas_price, gas_limit) -> bool:
    """Approve `amount` if current allowance is insufficient."""
    allowance = token_contract.functions.allowance(sender, spender).call()
    if allowance >= amount:
        return True
    try:
        tx = token_contract.functions.approve(spender, amount).build_transaction({
            "from": sender,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(sender)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return receipt.status == 1
    except Exception as e:
        _post(f"Approve failed: {e}", "error")
        return False

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Auto‑Compounder Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        poll_interval = int(config.get("poll_interval_seconds", 600))
        while True:
            result = compound(config)
            if result:
                if result.get("action") == "skip":
                    _post(f"Compounder skipped: {result.get('reason')}", "info", result)
                else:
                    _post(f"Compounded successfully: claimed {result.get('reward_claimed')}, staked LP", "info", result)
            else:
                _post("Compound cycle failed", "error")
            _heartbeat()
            time.sleep(poll_interval)

if __name__ == "__main__":
    main()
